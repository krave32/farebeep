"""FareBeep API - WhatsApp conversational layer (Meta Cloud + Twilio test).

Endpoints:
  GET  /webhook/meta    - Meta handshake (hub.challenge echo after
                           hub.verify_token check)
  POST /webhook/meta    - Meta message receiver. X-Hub-Signature-256 is
                           verified with HMAC-SHA256 over the RAW body using
                           META_APP_SECRET (the requirement: `use hmac to
                           verify X-Hub-Signature-256`).
  POST /webhook/twilio  - Twilio WhatsApp Sandbox receiver (test channel).
                           X-Twilio-Signature is verified with the account
                           auth token; the reply goes out via the REST API
                           in a background task.
  GET/POST /tools/search - ElevenLabs server tool: ledger-first fare lookup
                           (ledger hit wins; Travels247 live on miss, UPSERTed).
  POST /tools/reserve   - ElevenLabs server tool: live re-verify + 10-minute
                           booking_session + Paystack link (payment FIRST -
                           the Travels247 PNR is only issued in /webhook/paystack
                           after charge.success).
  POST /webhook/paystack- Paystack transaction events (settles the
                           10-minute booking loop; issues the Travels247 PNR on
                           success when a booking_token + travellers exist).
  GET  /health          - liveness.
  GET  /admin/ops       - ops snapshot (X-Admin-Token; closed without
                          ADMIN_TOKEN): inbound dispatch health, dead
                          letters, receipts, beeps, bookings, agent stats
  POST /admin/ops/dead-letters/{id}/replay
                        - re-dispatch one dead letter from its payload

Run:  uvicorn FareBeep.main:app --port 8000
"""
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import uuid

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func

from FareBeep import brain, cards, chatstate
from FareBeep.config import (ADMIN_TOKEN, APP_BASE_URL, CONSENT_VERSION,
                             MESSAGING_PROVIDER, META_APP_SECRET,
                             META_VERIFY_TOKEN, REQUOTE_TOLERANCE_NGN,
                             TRAVELS247_EMAIL, TRAVELS247_PASSWORD,
                             ELEVENLABS_TOOL_SECRET, GROQ_API_KEY, GUIDED_MODE)
from FareBeep.database import SessionLocal, init_db
from FareBeep.iata import city_name, resolve_iata
from FareBeep.models import (BookingSession, DeliveryReceipt,
                             ProcessedMessage, Subscription, User, utcnow)
from FareBeep.notifier import MetaWhatsapp, get_notifier
from FareBeep.payments import verify_paystack_signature
from FareBeep.search import LedgerOnlyEngine, LedgerSearch
from FareBeep.travels247 import (Travels247Client, Travels247Error,
                                     pick_cheapest)
from FareBeep.transactions import BookingService, PaystackError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("farebeep.main")

app = FastAPI(title="FareBeep - Transactional Utility")

notifier = get_notifier()

# Which brain answered, this process. Ops-visible via GET /admin/ops so a
# silently-degrading Groq key (fallback storms) shows up instead of hiding
# in per-request logs. Per-process counters reset on restart - direction,
# not billing.
_agent_stats = {"groq_ok": 0, "groq_fail": 0, "guided_turns": 0}

# Per-chat conversational memory lives in the `chat_state` table (see
# FareBeep/chatstate.py) - NOT in RAM, so it survives deploys/restarts and
# works from any replica. Last quoted fare, last ranked list, and any
# pending follow-up question are read/written there on every turn.


@app.on_event("startup")
def _startup():
    """Create the schema on first run (safe in both SQLite + Supabase modes)
    then verify the connection - prints the mission banner:
    '✅ Connected to Supabase Shared Ledger'. Finally, recover any inbound
    messages orphaned by a crash-after-ack (claimed, 200 sent, never
    processed) - normally zero, so boot stays fast."""
    from FareBeep.database import verify_connection
    from FareBeep.models import Base
    init_db(Base)
    verify_connection()
    try:
        recovered = recover_orphaned_inbound()
        if recovered:
            logger.warning("Startup recovered %d orphaned message(s)",
                           recovered)
    except Exception as e:
        logger.error("Startup recovery failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Meta Cloud API Webhook - HANDSHAKE
# ---------------------------------------------------------------------------
@app.get("/webhook/meta")
async def meta_verify(request: Request):
    """GET verification: echo hub.challenge when hub.verify_token matches."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        logger.info("Meta webhook subscribed (handshake OK)")
        return PlainTextResponse(challenge, media_type="text/plain")
    logger.warning("Meta handshake rejected (mode=%r token=%r)", mode, token)
    return Response(status_code=403)


# ---------------------------------------------------------------------------
# Meta Cloud API Webhook - RECEIVER
# ---------------------------------------------------------------------------
def _verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    """X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(META_APP_SECRET, raw_body).

    The HMAC is computed over the RAW request body - never over a parsed /
    re-serialized version.
    """
    if not signature_header or not META_APP_SECRET:
        return False
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Meta inbound bridge - classify ALL messages, save-before-ack, FIFO per phone.
# a. HMAC verified above (unchanged).
# b. Every entry/change/message in the payload is classified (not just [0]).
# c. New wamids are inserted into processed_messages BEFORE the 200 ack
#    (ON CONFLICT DO NOTHING semantics via IntegrityError catch - portable
#    across Supabase Postgres and SQLite). Duplicates are dropped here.
# d. The 200 ack goes out immediately - no typing bubble, no brain, no
#    network calls in the request thread (Meta 20s deadline).
# e. Newly-claimed messages are processed in ONE background task, grouped
#    by phone in timestamp (FIFO) order, through the EXISTING concierge
#    pipeline (_handle_incoming_message / _tap_*). Booking/payment paths
#    are untouched - this bridge only changes HOW messages reach them.
# f. FAILED rows are reclaimable: when Meta redelivers the same wamid
#    (its standard retry), a row stuck at failed with attempts left is
#    re-queued instead of dup-dropped - bounded by MAX_INBOUND_ATTEMPTS.
#    done/queued/exhausted rows stay dropped (never double-book).
# g. Outbound status callbacks (sent/delivered/read/failed) are UPSERTed
#    into delivery_receipts for ops visibility. Failed receipts NEVER
#    trigger an automatic resend here - booking/payment messages must
#    not be blind-retried.
# ---------------------------------------------------------------------------

MAX_INBOUND_ATTEMPTS = 3

# Ownership lease: a worker dispatches ONLY rows it holds a live lease
# on. Web batches, the startup sweep and the periodic sweep all take
# rows through acquire_queued_messages (one atomic UPDATE ... WHERE the
# lease is free-or-expired ... RETURNING), so two workers - threads in
# this process or separate processes/replicas - can never hold the same
# row at once. The sweep additionally requires row AGE, the lease
# requires OWNERSHIP: a slow-but-live turn holds its lease, so recovery
# can never steal it no matter how long it runs.
LEASE_SECONDS = 600


def _lease_owner(prefix: str) -> str:
    """Ops-visible lease holder id: role + pid + thread + random."""
    return (f"{prefix}:{os.getpid()}:{threading.get_ident()}:"
            f"{uuid.uuid4().hex[:8]}")


def acquire_queued_messages(owner: str, phone: str = None,
                            stale_minutes: int = 0,
                            lease_seconds: int = LEASE_SECONDS,
                            limit: int = None) -> list:
    """Atomically lease queued rows. ONE UPDATE statement takes every
    matching row whose lease is free (NULL) or expired and stamps it
    with (owner, deadline) - the database serializes concurrent
    acquirers, so the returned sets are always disjoint. Returns
    [{message_id, payload, created_at, phone}]. Never raises (empty on
    DB error - a missed sweep is safer than a crashed webhook)."""
    from datetime import timedelta
    from sqlalchemy import and_, or_, update
    now = utcnow()
    conds = [
        ProcessedMessage.status == "queued",
        or_(ProcessedMessage.lease_expires_at.is_(None),
            ProcessedMessage.lease_expires_at < now),
    ]
    if phone is not None:
        conds.append(ProcessedMessage.phone == phone)
    if stale_minutes:
        conds.append(ProcessedMessage.created_at
                     < now - timedelta(minutes=stale_minutes))
    if limit:
        # Oldest claims first (bounds one batch without starving old
        # rows). Portable form: Postgres has no UPDATE..ORDER BY..LIMIT,
        # so cap via a subselect both engines accept.
        from sqlalchemy import select
        conds.append(ProcessedMessage.message_id.in_(
            select(ProcessedMessage.message_id)
            .where(and_(*conds))
            .order_by(ProcessedMessage.created_at)
            .limit(limit)
            .scalar_subquery()))
    stmt = (update(ProcessedMessage).where(and_(*conds))
            .values(lease_owner=owner,
                    lease_expires_at=now + timedelta(seconds=lease_seconds))
            .returning(ProcessedMessage.message_id,
                       ProcessedMessage.payload,
                       ProcessedMessage.created_at,
                       ProcessedMessage.phone))
    db = SessionLocal()
    try:
        rows = db.execute(stmt).all()
        db.commit()
        return [{"message_id": r[0], "payload": r[1],
                 "created_at": r[2], "phone": r[3]} for r in rows]
    except Exception as e:
        logger.warning("Lease acquire failed (%s): %s", owner, e)
        try:
            db.rollback()
        except Exception:
            pass
        return []
    finally:
        db.close()


def _snapshot_message(m) -> dict:
    """JSON-safe snapshot of a classified message, stored at claim time so
    a crash-after-ack is recoverable: the payload carries everything
    _dispatch_single needs (no re-fetch, no Meta redelivery required)."""
    mtype = (m.message_type.value
             if hasattr(m.message_type, "value") else str(m.message_type))
    return {
        "message_id": m.message_id or "",
        "from_number": m.from_number or "",
        "message_type": mtype,
        "timestamp": m.timestamp or "",
        "text": m.text,
        "button_id": m.button_id,
        "button_title": m.button_title,
        "list_id": m.list_id,
        "list_title": m.list_title,
        "flow_token": m.flow_token,
        "flow_data": m.flow_data,
        "raw": m.raw if isinstance(m.raw, dict) else {},
    }


def _restore_message(snap: dict):
    """Rebuild an InboundMessage from a stored snapshot (startup recovery)."""
    from FareBeep.whatsapp.router import InboundMessage, MessageType
    try:
        mtype = MessageType((snap or {}).get("message_type", "unknown"))
    except ValueError:
        mtype = MessageType.UNKNOWN
    return InboundMessage(
        message_id=(snap or {}).get("message_id", ""),
        from_number=(snap or {}).get("from_number", ""),
        message_type=mtype,
        timestamp=(snap or {}).get("timestamp", ""),
        raw=(snap or {}).get("raw", {}) or {},
        text=(snap or {}).get("text"),
        button_id=(snap or {}).get("button_id"),
        button_title=(snap or {}).get("button_title"),
        list_id=(snap or {}).get("list_id"),
        list_title=(snap or {}).get("list_title"),
        flow_token=(snap or {}).get("flow_token"),
        flow_data=(snap or {}).get("flow_data"),
    )


def recover_orphaned_inbound(db=None, stale_minutes: int = 0) -> int:
    """Re-dispatch rows stuck at queued, rebuilt from stored payloads.

    Two callers:
      - startup (_startup, stale_minutes=0): at boot no worker is alive,
        so EVERY queued row is a crash-after-ack orphan - replay all.
      - worker sweep (run_cycles, stale_minutes=5): periodic safety net
        that needs NO restart and NO Meta redelivery. Only rows queued
        longer than the cutoff are orphans - fresh rows may still have a
        live background task working on them, so they are left alone.

    ORDER RULE (shared with _process_inbound_batch): sender timestamp
    first (user-intent order - one payload can arrive wire-shuffled),
    claim time (created_at) breaks ties and orders separate requests.
    See _arrival_key.

    Attempts accounting continues, so a poison message still
    dead-letters instead of looping forever. Rows with NO stored
    payload (claimed by the pre-recovery build) can never be replayed:
    stale ones are parked as exhausted/failed with an explicit error
    instead of cycling. Returns the count re-dispatched. Never raises.
    """
    own = db is None
    db = db or SessionLocal()
    try:
        # Lease, don't just read: rows whose lease is live belong to a
        # running worker (possibly on another thread/process) and are
        # NOT orphans, however old they look. Only free-or-expired rows
        # are taken - atomically, so a concurrent batch and sweep split
        # the work instead of doubling it.
        taken = acquire_queued_messages(
            _lease_owner("sweep" if stale_minutes else "startup"),
            stale_minutes=stale_minutes)
        legacy = [t for t in taken
                  if not (t["payload"] or {}).get("message_id")]
        if legacy:
            db.query(ProcessedMessage).filter(
                ProcessedMessage.message_id.in_(
                    [t["message_id"] for t in legacy])).update(
                {"status": "failed",
                 "attempts": MAX_INBOUND_ATTEMPTS,
                 "last_error": ("no stored payload (claimed before crash "
                                "recovery shipped) - ask the customer to "
                                "resend"),
                 "processed_at": utcnow(),
                 "lease_owner": None,
                 "lease_expires_at": None},
                synchronize_session=False)
            db.commit()
        replayable = [t for t in taken
                      if (t["payload"] or {}).get("message_id")]
        if not replayable:
            return 0
        logger.warning("Recovery: re-dispatching %d orphaned inbound "
                       "message(s)", len(replayable))
        grouped: dict = {}
        order: list = []
        for t in replayable:
            snap = t["payload"] or {}
            phone = snap.get("from_number") or t["phone"] or ""
            if not phone:
                continue
            if phone not in grouped:
                grouped[phone] = []
                order.append(phone)
            grouped[phone].append(t)
        for phone in order:
            with _phone_lock(phone):
                batch = sorted(
                    grouped[phone],
                    key=lambda t: _arrival_key(t["payload"], t["created_at"]))
                for t in batch:
                    m = _restore_message(t["payload"])
                    try:
                        _dispatch_single(m)
                        _mark_processed(m.message_id, "done")
                    except Exception as e:
                        logger.error("Recovery handling failed (%s): %s",
                                     phone, e)
                        _mark_processed(m.message_id, "failed",
                                        error=str(e))
        return len(replayable)
    except Exception as e:
        logger.error("Recovery sweep failed (orphans stay queued): %s", e)
        return 0
    finally:
        if own:
            try:
                db.close()
            except Exception:
                pass


# Per-phone batch locks: two webhook POSTs for the same customer (or a
# startup recovery racing a live request) must never interleave turns -
# each phone's batch runs alone. Single-process scope: it serializes the
# threadpool BackgroundTasks on this replica. Cross-replica races need
# DB-level serialization (Railway runs one replica by default - flag it
# before scaling web replicas).
_PHONE_LOCKS: dict = {}
_PHONE_LOCKS_GUARD = threading.Lock()


def _phone_lock(phone: str) -> threading.Lock:
    with _PHONE_LOCKS_GUARD:
        lock = _PHONE_LOCKS.get(phone)
        if lock is None:
            lock = threading.Lock()
            _PHONE_LOCKS[phone] = lock
        return lock
def _classify_all_entries(payload: dict) -> list:
    """Classify every message in every entry. Legacy template quick-reply
    taps (messages[].button.payload - no 'interactive' wrapper) bypass the
    router, so they are re-attached here as BUTTON_REPLY (same tap ids the
    old single-message path fed to cards.translate_tap)."""
    from FareBeep.whatsapp.router import (InboundMessage, MessageType,
                                          classify_message)
    classified: list = []
    seen_ids: set = set()
    for entry in payload.get("entry") or []:
        try:
            batch = classify_message(entry)
        except Exception as e:
            logger.warning("classify_message failed on entry: %s", e)
            batch = []
        for m in batch:
            classified.append(m)
            if m.message_id:
                seen_ids.add(m.message_id)
    # Legacy sweep: template button payloads the router drops (it returns
    # None for unknown shapes). Preserves the old BOOK/pick/alert/beep tap
    # behaviour for those messages.
    for entry in payload.get("entry") or []:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id", "")
                if mid and mid in seen_ids:
                    continue
                tap_id = (msg.get("button") or {}).get("payload") or ""
                if tap_id and msg.get("from"):
                    classified.append(InboundMessage(
                        message_id=mid,
                        from_number=msg.get("from", ""),
                        message_type=MessageType.BUTTON_REPLY,
                        timestamp=msg.get("timestamp", ""),
                        raw=msg,
                        button_id=tap_id,
                        button_title=tap_id,
                    ))
                    if mid:
                        seen_ids.add(mid)
    return classified


def _claim_inbound_messages(messages: list) -> list:
    """Save-before-ack: insert new wamids as queued. Duplicates (PK clash)
    are dropped - EXCEPT failed rows with attempts left, which Meta's own
    redelivery reclaims (status back to queued, attempts preserved). That
    is the retry transport: durable across restarts, bounded by
    MAX_INBOUND_ATTEMPTS, and it never re-runs a done turn (no double-book).
    Messages WITHOUT an id (old tests, malformed retries) cannot dedupe -
    they always pass through, preserving legacy behaviour."""
    from sqlalchemy.exc import IntegrityError
    claimed = []
    db = SessionLocal()
    try:
        for m in messages:
            mid = (m.message_id or "").strip()
            if not mid:
                claimed.append(m)
                continue
            try:
                db.add(ProcessedMessage(
                    message_id=mid,
                    phone=m.from_number or None,
                    message_type=(m.message_type.value
                                  if hasattr(m.message_type, "value")
                                  else str(m.message_type)),
                    status="queued",
                    payload=_snapshot_message(m),
                ))
                db.commit()
                claimed.append(m)
            except IntegrityError:
                db.rollback()
                row = db.query(ProcessedMessage).filter_by(
                    message_id=mid).first()
                if (row is not None and row.status == "failed"
                        and (row.attempts or 0) < MAX_INBOUND_ATTEMPTS):
                    row.status = "queued"
                    row.lease_owner = None     # next acquirer leases it
                    row.lease_expires_at = None
                    db.commit()
                    claimed.append(m)
                    logger.info("Reclaim: retrying failed wamid %s "
                                "(attempt %s)", mid, (row.attempts or 0) + 1)
                else:
                    logger.info("Dedup: dropping repeat wamid %s", mid)
            except Exception as e:
                db.rollback()
                logger.warning("Claim failed for %s: %s", mid, e)
    finally:
        db.close()
    return claimed


def _mark_processed(message_id: str, status: str, error: str = None) -> None:
    """Flip a claimed row to done/failed. Failed bumps attempts and keeps
    the truncated error - the row stays reclaimable until attempts run
    out, then it is permanently dropped (ops-visible via last_error)."""
    if not (message_id or "").strip():
        return
    db = SessionLocal()
    try:
        row = db.query(ProcessedMessage).filter_by(
            message_id=message_id).first()
        if row is not None:
            row.status = status
            row.lease_owner = None       # release ownership with the outcome
            row.lease_expires_at = None
            if status == "failed":
                row.attempts = (row.attempts or 0) + 1
                if error:
                    row.last_error = str(error)[:500]
                if row.attempts >= MAX_INBOUND_ATTEMPTS:
                    logger.error(
                        "Dead-letter: wamid %s from %s failed %d times - "
                        "retained in processed_messages, needs a human "
                        "(last error: %s)",
                        message_id, row.phone, row.attempts,
                        (row.last_error or "")[:200])
            if status in ("done", "failed"):
                row.processed_at = utcnow()
            db.commit()
    except Exception as e:
        logger.warning("Mark %s=%s failed: %s", message_id, status, e)
    finally:
        db.close()


def _dispatch_single(msg) -> None:
    """One classified message through the EXISTING pipeline. Typing bubble
    lives here (background) so the webhook thread never blocks on network."""
    from FareBeep.whatsapp.router import MessageType
    phone = msg.from_number or ""
    if not phone:
        return
    try:
        MetaWhatsapp().send_typing_indicator(phone)
    except Exception as e:
        logger.warning("Typing bubble failed (%s): %s", phone, e)
    if msg.message_type == MessageType.TEXT:
        if (msg.text or "").strip():
            _handle_incoming_message(phone, msg.text)
        return
    if msg.message_type == MessageType.UNKNOWN:
        # Legacy tolerance: typeless payloads (old tests, early Meta
        # shape) carry text.body with no "type" field. The old
        # single-message path read that body directly - do the same
        # rather than dropping a real customer message.
        legacy = ((msg.raw or {}).get("text") or {}).get("body", "")
        if isinstance(legacy, str) and legacy.strip():
            _handle_incoming_message(phone, legacy)
        return
    if msg.message_type in (MessageType.BUTTON_REPLY,
                            MessageType.LIST_REPLY):
        tap_id = msg.button_id or msg.list_id or ""
        if not tap_id:
            return
        tap = cards.translate_tap(tap_id)
        if tap is None:
            logger.warning("Meta tap ignored: unknown button id %r", tap_id)
            return
        if tap[0] == "alert":
            _tap_alert_by_phone(phone, tap[1])
            return
        if tap[0] == "beep":
            _tap_beep_by_phone(phone, tap[1])
            return
        _handle_incoming_message(
            phone, str(tap[1]) if tap[0] == "pick" else "BOOK")
        return
    logger.info("Inbound %s from %s needs no concierge turn - ignored",
                msg.message_type, phone)


def _arrival_key(payload: dict, created) -> tuple:
    """Sort key for one customer's queued rows: sender timestamp first,
    claim time second. Timestamp = the order the human typed them (Meta
    can deliver one payload wire-shuffled); created_at = our arrival
    record, which orders separate requests and breaks timestamp ties.
    NOTE the limit this implies: only ARRIVED (claimed) messages can be
    ordered - a message Meta hasn't delivered yet cannot be sequenced,
    so a late arrival with an early timestamp still runs late."""
    try:
        ts = int(str((payload or {}).get("timestamp") or ""))
        has_ts = 0
    except (ValueError, TypeError):
        ts, has_ts = 0, 1
    created_key = (created.isoformat() if hasattr(created, "isoformat")
                   else str(created or ""))
    return (has_ts, ts, created_key)


def _process_inbound_batch(messages: list) -> None:
    """Background worker: SAVED order per phone, then mark each row
    done/failed. The messages list is only the trigger - the work order
    comes from the DB (queued rows for the phone, see _arrival_key), so
    separate webhook POSTs for one customer still run deterministically,
    never in thread-scheduling order. In-memory copies of already-stored
    rows are skipped (a racing batch may have taken them); id-less
    messages run after the recorded ones. One bad message never kills
    the batch (booking safety: exceptions are contained per message).

    Crash-mid-turn honesty: a turn that dies AFTER writing a booking
    but BEFORE done WILL re-run on recovery/redelivery and can leave a
    second PENDING session (new payment_ref). Money still moves only via
    Paystack per unique ref, settle is idempotent, and the worker expiry
    sweep clears stale pendings - but exactly-once booking is NOT
    claimed here (see test_booking_crash_mid_turn_documents_duplicates).
    """
    grouped: dict = {}
    order: list = []
    for m in messages:
        phone = m.from_number or ""
        if not phone:
            continue
        if phone not in grouped:
            grouped[phone] = []
            order.append(phone)
        grouped[phone].append(m)
    for phone in order:
        # Per-phone lock (this process) + atomic lease (every process):
        # two batches for one customer can neither interleave nor take
        # each other's rows. Different customers still run parallel.
        with _phone_lock(phone):
            taken = acquire_queued_messages(_lease_owner("web"), phone=phone)
            work = []
            for t in sorted(taken,
                            key=lambda t: _arrival_key(t["payload"],
                                                       t["created_at"])):
                snap = t["payload"] or {}
                if snap.get("message_id"):
                    work.append(_restore_message(snap))
                # Payload-less rows are the sweep's job (parked, not
                # replayed) - never dispatched from here.
            for m in grouped[phone]:
                if not (m.message_id or "").strip():
                    work.append(m)  # id-less: no arrival record, run as-is
            for m in work:
                try:
                    _dispatch_single(m)
                    _mark_processed(m.message_id, "done")
                except Exception as e:
                    logger.error("Inbound handling failed (%s): %s",
                                 phone, e)
                    _mark_processed(m.message_id, "failed", error=str(e))


def _record_statuses(payload: dict) -> int:
    """Persist Meta outbound status callbacks (sent/delivered/read/failed)
    into delivery_receipts (upsert per wamid). Returns the count recorded.
    A failed receipt is ops-visible and loudly logged - but NEVER resent
    from here: booking/payment texts must not be blind-retried."""
    from FareBeep.models import DeliveryReceipt
    recorded = 0
    db = SessionLocal()
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for st in value.get("statuses", []):
                    if not isinstance(st, dict):
                        continue
                    mid = st.get("id", "")
                    if not mid:
                        continue
                    status = st.get("status", "")
                    phone = st.get("recipient_id")
                    try:
                        row = db.query(DeliveryReceipt).filter_by(
                            message_id=mid).first()
                        if row is None:
                            db.add(DeliveryReceipt(
                                message_id=mid, phone=phone, status=status))
                        else:
                            row.status = status
                            if phone:
                                row.phone = phone
                        db.commit()
                        recorded += 1
                        if status == "failed":
                            logger.warning(
                                "Outbound %s to %s FAILED "
                                "(see delivery_receipts - no auto-resend)",
                                mid, phone)
                    except Exception as e:
                        db.rollback()
                        logger.warning("Receipt upsert failed for %s: %s",
                                       mid, e)
    finally:
        db.close()
    return recorded


@app.post("/webhook/meta")
async def meta_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()

    if not _verify_meta_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Meta webhook REJECTED: bad X-Hub-Signature-256")
        return Response(status_code=403)

    payload = await request.json()
    # Status callbacks ride the same webhook - record first so a
    # statuses-only payload still tracks delivery before the 200 ack.
    _record_statuses(payload)
    claimed = _claim_inbound_messages(_classify_all_entries(payload))

    # Ack Meta immediately (20s deadline); the batch runs off-thread.
    # NOTE: plain "200 OK" (not {"status":"ok"}) keeps the existing
    # webhook contract + tests green - the status code is the ack.
    if claimed:
        background.add_task(_process_inbound_batch, claimed)
    return Response(content="200 OK", media_type="text/plain")


# Standalone greetings: the WHOLE message is the greeting, nothing else.
# (A route question containing "hi" - "hi, lagos to abuja" - is not this.)
_SESSION_GREETINGS = frozenset({
    "hi", "hello", "hey", "yo", "good morning", "good afternoon",
    "good evening", "hi there", "hello there", "hey there",
})


def _is_session_greeting(text: str) -> bool:
    """True when the message is just a greeting (punctuation tolerated)."""
    return (text or "").strip().lower().rstrip("!.").strip() \
        in _SESSION_GREETINGS


# Honesty prefix when the smart brain is down and the deterministic one
# answers instead: one line, no jargon, then normal handling continues.
RESTING_NOTE = ("Our smart brain is resting right now, so I'm running on "
                "simple mode - I can still check fares and set beeps if "
                "you send route + date plainly, e.g. 'Lagos to Abuja "
                "tomorrow'.\n\n")


_MANAGE_LIST = frozenset({
    "beeps", "my beeps", "my alerts", "alerts", "watches", "my watches",
    "show beeps", "show alerts", "list beeps", "list alerts",
})
_MANAGE_VERBS = ("PAUSE", "RESUME", "CANCEL", "EDIT")


def _list_beeps(db, user: User) -> None:
    """Numbered beep list (numbers match PAUSE/RESUME/CANCEL/EDIT)."""
    from FareBeep.alerts import format_watch, list_watches
    subs = list_watches(db, user.user_id)
    if not subs:
        _say(user.phone,
             "You have no price beeps yet. Send a route like 'Lagos to "
             "Abuja tomorrow' and I'll watch it - or TRACK it with a "
             "target price.",
             user.name)
        return
    lines = "\n".join(format_watch(s, i) for i, s in enumerate(subs, start=1))
    _say(user.phone,
         f"🔔 Your beeps ({len(subs)}):\n{lines}\n\n"
         f"Reply PAUSE n, RESUME n, CANCEL n, or EDIT n 70000.",
         user.name)


def _parse_edit_price(rest: str):
    """Last number in the text is the new target ('70k', '70,000');
    the word DROP (and nothing numeric) clears it to drop-watch."""
    import re
    if re.search(r"\bdrop\b", rest or "", re.IGNORECASE):
        return "drop"
    nums = re.findall(r"(\d[\d,]*)\s*(k)?\b", rest or "", re.IGNORECASE)
    if not nums:
        return None
    digits, kilo = nums[-1]
    try:
        value = int(digits.replace(",", ""))
    except ValueError:
        return None
    return float(value * 1000 if kilo else value)


def _handle_manage(db, user: User, text: str) -> bool:
    """Deterministic beep management. True = turn handled (agent and
    brain must not run). Bare PAUSE/RESUME apply to a lone watch;
    bare CANCEL/EDIT always ask first (destructive/ambiguous)."""
    norm = (text or "").strip()
    flat = norm.lower().rstrip("?!.")
    upper = norm.upper().rstrip("?!.")
    if flat in _MANAGE_LIST:
        _list_beeps(db, user)
        return True
    verb = None
    for v in _MANAGE_VERBS:
        if upper == v or upper.startswith(v + " "):
            verb = v
            break
    if verb is None:
        return False
    rest = norm[len(verb):].strip()
    from FareBeep.alerts import (WatchAmbiguous, format_watch, list_watches,
                                 match_watch)
    subs = list_watches(db, user.user_id)
    if not subs:
        _say(user.phone,
             "You have no price beeps to change. Send a route like "
             "'Lagos to Abuja tomorrow' and I'll watch it.",
             user.name)
        return True
    if not rest:
        if verb in ("PAUSE", "RESUME") and len(subs) == 1:
            sub, n = subs[0], 1
        else:
            _list_beeps(db, user)
            return True
    else:
        try:
            found = match_watch(subs, rest)
        except WatchAmbiguous:
            _say(user.phone,
                 "Which one did you mean?\n" +
                 "\n".join(format_watch(s, i)
                            for i, s in enumerate(subs, start=1)) +
                 f"\n\nReply {verb} with its number.",
                 user.name)
            return True
        sub, n = found
        if sub is None:
            if verb == "EDIT" and len(subs) == 1:
                sub, n = subs[0], 1
            else:
                _say(user.phone,
                     "I couldn't find that watch. " +
                     "\n".join(format_watch(s, i)
                                for i, s in enumerate(subs, start=1)) +
                     f"\n\nReply {verb} with its number.",
                     user.name)
                return True
    if verb == "PAUSE":
        sub.paused = True
        db.commit()
        _say(user.phone,
             f"⏸ Paused {format_watch(sub, n)} - settings kept, silent "
             f"until you RESUME it.",
             user.name)
    elif verb == "RESUME":
        sub.paused = False
        db.commit()
        _say(user.phone, f"▶ Resumed {format_watch(sub, n)}.", user.name)
    elif verb == "CANCEL":
        db.delete(sub)
        db.commit()
        _say(user.phone,
             f"❌ Cancelled {city_name(sub.origin)} -> "
             f"{city_name(sub.destination)} - watch deleted.",
             user.name)
    elif verb == "EDIT":
        price = _parse_edit_price(rest)
        if price is None:
            _say(user.phone,
                 f"To change {format_watch(sub, n)}, reply like: EDIT {n} "
                 f"70000 - or EDIT {n} DROP to watch any genuine drop.",
                 user.name)
            return True
        from FareBeep.alerts import SubscriptionMonitor
        new_target = None if price == "drop" else price
        SubscriptionMonitor(db).subscribe(
            user.user_id, sub.origin, sub.destination,
            target_price=new_target, target_date=sub.target_date)
        if new_target is None:
            _say(user.phone,
                 f"✏️ Updated {format_watch(sub, n)}: now watching any "
                 f"genuine drop.",
                 user.name)
        else:
            _say(user.phone,
                 f"✏️ Updated {format_watch(sub, n)}: now beeping at "
                 f"₦{new_target:,.0f} or lower.",
                 user.name)
    return True


# Whole-message stop requests: handled deterministically BEFORE the agent
# (the model must never improvise deletions - NDPA data promise).
_STOP_PHRASES = frozenset({
    "stop", "unsubscribe", "stop all", "stop alerts", "cancel all alerts",
    "cancel my alerts", "remove all alerts", "delete my data",
    "delete everything", "opt out", "optout", "stop beeps",
})

# Phrases that stop everything even buried in a sentence ("please stop
# messaging me"). Each REQUIRES an object word - a bare "stop" inside
# other words ("stopover in Abuja", "nonstop flight") must never match.
_STOP_CONTAINS = (
    r"\bstop\s+(all\s+)?(alerts|beeps|messages|messaging|notifications)\b",
    r"\bunsubscribe\b",
    r"\bcancel\s+(all\s+|my\s+)?(alerts|beeps)\b",
    r"\bdelete\s+(my|all|everything)\b",
    r"\bopt\s?out\b",
)


def _is_stop_request(text: str) -> bool:
    """Stop/unsubscribe/erase request: whole message, or a stop-phrase
    with an explicit object (punctuation tolerated). Negated phrasing
    ("don't cancel my alerts") is NEVER a stop - it falls through to
    the agent/brain for nuanced handling."""
    import re
    flat = (text or "").strip().lower().rstrip("!.").strip()
    if flat in _STOP_PHRASES:
        return True
    if _looks_negated(flat):
        return False
    return any(re.search(pat, flat) for pat in _STOP_CONTAINS)


def _looks_negated(text: str) -> bool:
    """Hedged/negated phrasing that must never trigger deletion."""
    import re
    return bool(re.search(r"\b(don'?t|do not|never mind|not really)\b",
                          text or ""))


def _handle_stop(db, user: User) -> None:
    """Delete every subscription AND all chat-scoped state, then confirm.
    Money records (booking_sessions) are untouched - refunds stay provable."""
    from FareBeep.alerts import SubscriptionMonitor
    removed = SubscriptionMonitor(db).unsubscribe(user.user_id)
    chatstate.clear_pending_fare(db, user.phone)
    chatstate.clear_pending_requote(db, user.phone)
    chatstate.clear_last_fare(db, user.phone)
    chatstate.clear_last_fares(db, user.phone)
    chatstate.clear_agent_history(db, user.phone)
    if removed:
        _say(user.phone,
             f"🔕 Stopped - all {removed} price alert(s) removed and your "
             f"chat data deleted.",
             user.name)
    else:
        _say(user.phone,
             "You have no active price alerts - nothing to stop. "
             "Your chat data is cleared anyway.",
             user.name)


# Watch helpers (list/match/format) live in alerts.py next to the
# Subscription model logic - imported lazily like the rest of main's
# alerts usage. _tap_alert keeps its own index+context resolution.


def _handle_incoming_message(phone: str, text: str) -> None:
    """PASS 2 - CONCIERGE LOGIC: intent -> ask / search / act -> reply."""
    db = SessionLocal()
    try:
        try:
            user = _get_or_create_user(db, phone)

            # GREETING RESET: sessions never end on their own, so a
            # standalone hello starts a whole new one. Without this,
            # stale quotes, ranked picks and pending follow-ups hijack
            # the NEXT route ("hi" -> "Abuja" would answer a dead
            # question from the old thread) - and the agent's rolling
            # memory would resurrect it conversationally anyway ("still
            # on Abuja->PHC?"). So EVERYTHING chat-scoped goes: quotes,
            # picks, pending items AND agent history. The User row (name,
            # identity) survives - only the conversation restarts.
            if _is_session_greeting(text):
                chatstate.clear_pending_fare(db, user.phone)
                chatstate.clear_pending_requote(db, user.phone)
                chatstate.clear_last_fare(db, user.phone)
                chatstate.clear_last_fares(db, user.phone)
                chatstate.clear_agent_history(db, user.phone)

            # PICK GATE (before the brain): a "1", "2" or "3" reply right
            # after a ranked fare list selects and locks THAT fare. Narrow:
            # only when an active list exists AND the message is the number
            # (or "number 2" / "the 2nd one"). Without a list, a bare number
            # falls through to the brain as a DATE - never intercepted.
            pick = _try_pick(db, text, phone)
            if pick is not None:
                if pick == "out_of_range":
                    n = len((chatstate.get_last_fares(db, phone) or {})
                            .get("fares", []))
                    _say(phone, f"I only showed {n} option"
                         + ("s" if n != 1 else "")
                         + ". Reply 1-" + str(n) + ", or ask me for a new search.",
                         user.name)
                elif pick == "unclear":
                    ctx = chatstate.get_last_fares(db, phone) or {}
                    msg = brain.compose_unclear_pick_reply(
                        text, ctx.get("fares") or [], user_name=user.name)
                    _say(user.phone, msg, user.name, humanized=True)
                else:
                    _reply_booking(db, user, brain.Intent(intent="book"),
                                   picked_fare=pick)
                return

            # REQUOTE GATE: after "the price moved to ₦X - still lock it?",
            # a bare yes/book answer proceeds with the already-live-quoted
            # fare; a no/cancel politely aborts. Anything naming a city or
            # date is a fresh request - it falls through to the brain.
            if _try_requote_answer(db, user, text):
                return

            # CONTINUATION: the user answered our follow-up question with a
            # bare city ("abuja" after "where will you be flying from?") -
            # fill the missing piece and search, like a person would.
            if _fill_pending_fare(db, user, text):
                return

            # DATE CONFIRMATION answer ("5 June" after "did you mean 5
            # June or 6 May?"): the pending route is complete, only the
            # date was missing - fill it and search. A city mention or
            # an ambiguous answer falls through for fresh handling.
            if _fill_pending_date(db, user, text):
                return

            # AMBIGUOUS DATE GUARD (before the agent, like STOP/MANAGE):
            # "05/06" reads two ways and must never reach search, booking
            # - or the agent's tools - as a guessed date. Parsed locally
            # (free, deterministic) so online/offline behave identically.
            if _try_date_confirm(db, user, text):
                return

            # GROQ AGENT: when configured (and not in guided mode), the
            # LangChain agent owns the turn end-to-end (tools included)
            # and its reply goes out verbatim. On ANY agent failure
            # (quota, outage, timeout) we fall through to the
            # deterministic brain below instead of going silent - the
            # user always gets an answer.
            #
            # Just before it: STOP / MANAGE whole-message commands own
            # their turns (the agent must never improvise deletions or
            # beep edits - a wrong guess here deletes real user data).
            # They sit AFTER pick/requote/pending so a financial answer
            # ("cancel" to a price question) always wins over commands.
            guided = GUIDED_MODE
            if _is_stop_request(text):
                _handle_stop(db, user)
                return
            if _handle_manage(db, user, text):
                return
            if GROQ_API_KEY and not guided:
                from FareBeep import agent as fare_agent
                try:
                    _say(phone, fare_agent.agent_reply(db, user, text),
                         user.name, humanized=True)
                    _agent_stats["groq_ok"] += 1
                    return
                except Exception as e:
                    _agent_stats["groq_fail"] += 1
                    logger.warning("Groq agent failed (%s) - guided "
                                   "fallback: %s", phone, e)
                    guided = True
            _agent_stats["guided_turns"] += 1

            intent = brain.parse_intent(text, force_local=guided)
            logger.info("Intent=%s payload=%s phone=%s",
                        intent.intent, intent.as_dict(), phone)

            if intent.name and not user.name:
                user.name = intent.name
                db.commit()
                logger.info("Captured name %s for %s", intent.name, phone)

            if intent.intent == "fare":
                if intent.has_route and _needs_date_confirm(intent):
                    _ask_date_confirm(db, user, intent)
                elif intent.has_route:
                    chatstate.clear_pending_fare(db, user.phone)  # superseded by a full route
                    _reply_fare(db, user, intent)
                elif intent.destination_iata and intent.date and not intent.origin_iata:
                    # "Find me a flight to Abj for next tuesday" - destination
                    # + date, no origin: search from the default hub (Lagos).
                    # BUT when the destination IS the hub (Lagos), the hub
                    # cannot also be the origin - that would be Lagos->Lagos.
                    # Ask where they're flying from instead.
                    if intent.destination_iata == "LOS":
                        _ask_missing_info(db, user, intent)
                    else:
                        _reply_fare(db, user, intent)
                elif intent.is_partial:
                    _ask_missing_info(db, user, intent)
                else:
                    _say(user.phone, _help_text(), user.name)
            elif intent.intent == "book":
                # BOOK after a fare quote: the route/date the user discussed
                # live in the per-chat context (chat_state), so a bare "BOOK"
                # books exactly what was quoted - never silently today.
                if intent.has_route or chatstate.get_last_fare(db, user.phone) is not None:
                    if intent.has_route and _needs_date_confirm(intent):
                        _ask_date_confirm(db, user, intent)
                    else:
                        _reply_booking(db, user, intent)
                elif intent.is_partial:
                    _ask_missing_info(db, user, intent)
                else:
                    _say(user.phone,
                         "To book: send your route, e.g. 'BOOK Lagos to "
                         "Abuja tomorrow' - or just say BOOK right after a "
                         "fare quote I sent you.",
                         user.name)
            elif intent.intent == "subscribe":
                if intent.has_route and _needs_date_confirm(intent):
                    _ask_date_confirm(db, user, intent)
                else:
                    _reply_subscribe(db, user, intent)
            elif intent.intent == "unsubscribe":
                if _looks_negated(text):
                    # "don't cancel my alerts" parsed as unsubscribe: the
                    # user is reassuring, not erasing. Confirm, delete nothing.
                    _say(user.phone,
                         "Understood - your price alerts stay exactly as "
                         "they are. Anything else I can do?",
                         user.name)
                else:
                    _reply_unsubscribe(db, user)
            elif intent.intent in ("status", "track"):
                _reply_status_ack(user, intent)
            else:
                msg = _help_text()
                if guided:
                    msg = RESTING_NOTE + msg
                _say(user.phone, msg, user.name)
        except Exception as e:
            # never let a single turn crash the webhook thread
            logger.error("Message handling failed (%s): %s", phone, e)
    finally:
        db.close()


def _say(phone: str, msg: str, user_name: str = None,
         humanized: bool = False) -> None:
    """Send a reply. By default Gemini runs a personality pass first so the
    bot sounds human; on AI failure the template goes out verbatim. When the
    message is ALREADY AI-narrated (the ranked fare list) pass humanized=True
    so it is sent verbatim - no second personality pass to garble the prices."""
    if not msg:
        notifier.send_text(phone, "")
        return
    if humanized:
        notifier.send_text(phone, msg)
        return
    notifier.send_text(phone, brain.compose_reply(msg, user_name=user_name))


_YES_WORDS = {"yes", "yeah", "yep", "y", "sure", "ok", "okay", "alright",
              "book", "lock", "proceed", "confirm", "continue", "go", "ahead"}
_NO_WORDS = {"no", "nope", "nah", "cancel", "forget", "never", "stop"}


def _try_requote_answer(db, user: User, text: str) -> bool:
    """Answer to the price-move question ("the price moved to ₦X - still
    lock it?"). YES/BOOK -> book the already re-quoted fare; NO/CANCEL ->
    politely abort. Returns True when handled. A message naming a city or
    a date is a fresh request, not an answer - it falls through."""
    pending = chatstate.get_pending_requote(db, user.phone)
    if not pending:
        return False
    if brain.single_city(text) is not None or brain.has_date(text):
        return False
    words = re.findall(r"[a-z]+", text.lower())
    if not words:
        return False
    if all(w in _YES_WORDS for w in words):
        chatstate.clear_pending_requote(db, user.phone)
        _reply_requoted_booking(db, user, pending)
        return True
    if all(w in _NO_WORDS for w in words):
        chatstate.clear_pending_requote(db, user.phone)
        _say(user.phone,
             "No problem - that quote is gone anyway. Ask me for a fresh "
             "search and I'll check the latest price.",
             user.name)
        return True
    return False


def _reply_requoted_booking(db, user: User, pending: dict) -> None:
    """Book the fare that was LIVE re-quoted before the price-move question
    (no re-fetch - it is seconds old). Surge is still refused."""
    fare = pending.get("fare") or {}
    origin_iata = pending.get("origin_iata")
    destination_iata = pending.get("destination_iata")
    flight_date = pending.get("flight_date")
    if not (origin_iata and destination_iata and flight_date and fare):
        _say(user.phone,
             "That quote has expired - ask me for a fresh search and I'll "
             "check the latest price.",
             user.name)
        return
    if fare.get("above_guardrail"):
        _say(user.phone,
             "That fare is at a surge price right now - I can't lock it "
             "safely. Try again in a bit, or TRACK it and I'll Beep you "
             "when it normalises.",
             user.name)
        return
    _create_and_send_booking(db, user, origin_iata, destination_iata,
                             flight_date, fare,
                             flight_iata=fare.get("flight_number"))


def _fill_pending_fare(db, user: User, text: str) -> bool:
    """Continue a partial fare conversation: we asked a follow-up (e.g.
    "where will you be flying from?"), and the user answered with a bare
    city. Fill the missing slot and run the search. Returns True when
    handled. Only fires for a clean single-city answer with no new date -
    anything richer is a fresh request for the brain."""
    pending = chatstate.get_pending_fare(db, user.phone)
    if not pending:
        return False
    iata = brain.single_city(text)
    if iata is None or brain.has_date(text):
        return False
    origin_iata = pending.get("origin_iata")
    destination_iata = pending.get("destination_iata")
    date_ = pending.get("date")
    if not origin_iata and destination_iata:
        origin_iata = iata
    elif not destination_iata and origin_iata:
        destination_iata = iata
    else:
        return False
    if not (origin_iata and destination_iata and date_):
        return False
    chatstate.clear_pending_fare(db, user.phone)
    intent = brain.Intent(intent="fare",
                          origin=city_name(origin_iata),
                          destination=city_name(destination_iata),
                          date=date_)
    logger.info("Filled pending fare: %s -> %s on %s (from '%s')",
                origin_iata, destination_iata, date_, text)
    _reply_fare(db, user, intent)
    return True


def _fill_pending_date(db, user: User, text: str) -> bool:
    """Mirror of _fill_pending_fare for the date slot: we asked 'did you
    mean X or Y?' and the user answered with a plain date. Fill it and
    run the search. Returns True when handled. Declines when the pending
    route is incomplete, when the text names a city (a fresh request for
    the brain), when no date parses, or when the answer is itself
    ambiguous (falls through so the confirm gate re-asks - the loop can
    never book a guessed date)."""
    pending = chatstate.get_pending_fare(db, user.phone)
    if not pending:
        return False
    origin_iata = pending.get("origin_iata")
    destination_iata = pending.get("destination_iata")
    if not (origin_iata and destination_iata) or pending.get("date"):
        return False
    if brain._local_route(text):
        return False
    day = brain._local_date(text)
    if not day:
        return False
    from FareBeep.dates import ambiguous_date_hint
    if ambiguous_date_hint(text) is not None:
        return False
    chatstate.clear_pending_fare(db, user.phone)
    intent = brain.Intent(intent="fare",
                          origin=city_name(origin_iata),
                          destination=city_name(destination_iata),
                          date=day)
    logger.info("Filled pending date: %s -> %s on %s (from '%s')",
                origin_iata, destination_iata, day, text)
    _reply_fare(db, user, intent)
    return True


def _try_date_confirm(db, user: User, text: str) -> bool:
    """Pre-agent ambiguous-date guard: locally parse this turn (no AI
    cost); when it names a complete route with a two-way date, ask which
    reading and stop - the agent's tools never see the guess. Returns
    True when handled."""
    try:
        intent = brain.parse_intent(text, force_local=True)
    except Exception:
        return False
    if intent is None or intent.intent not in ("fare", "book", "subscribe"):
        return False
    if not intent.has_route or not _needs_date_confirm(intent):
        return False
    _ask_date_confirm(db, user, intent)
    return True


def _ask_date_confirm(db, user: User, intent: brain.Intent) -> None:
    """Ambiguous date text on a complete route: remember the route
    (dateless pending) and ask which reading - never search or book a
    guessed date. The plain-date answer completes via _fill_pending_date."""
    chatstate.set_pending_fare(db, user.phone, {
        "origin_iata": intent.origin_iata,
        "destination_iata": intent.destination_iata,
        "date": None,
    })
    _say(user.phone,
         f"{intent.date_hint} Reply with the date plainly, e.g. '5 June'.",
         user.name)


def _needs_date_confirm(intent: brain.Intent) -> bool:
    """True when the turn carries an unresolved date ambiguity: the date
    slot is empty BECAUSE the text read two ways (not because no date
    was given). Money-adjacent branches gate on this before acting."""
    return bool(intent.date_hint and not intent.date)


def _ask_missing_info(db, user: User, intent: brain.Intent) -> None:
    """Pass 2 follow-up: the extraction was partial - ask for the missing
    pieces like a human agent would, never show a blank error. For fare
    partials we remember what we already know (chat_state) so the user's
    next bare-city answer completes the search (_fill_pending_fare)."""
    if intent.intent == "fare":
        chatstate.set_pending_fare(db, user.phone, {
            "origin_iata": intent.origin_iata,
            "destination_iata": intent.destination_iata,
            "date": intent.date,
        })
    dest = city_name(intent.destination_iata) if intent.destination_iata else None
    origin = city_name(intent.origin_iata) if intent.origin_iata else None
    if dest and not origin and not intent.date:
        msg = (f"I'd love to help you get to {dest}! ✈️ Where will you be "
               f"flying from, and what date are you looking at?")
    elif dest and not origin:
        msg = f"I'd love to help you get to {dest}! ✈️ Where will you be flying from?"
    elif origin and not dest:
        msg = f"Great - from {origin}! 🎉 Where are you headed?"
    else:
        msg = ("I'd love to help! ✈️ Tell me where you're going, where you're "
               "flying from, and what date you have in mind.")
    _say(user.phone, msg, user.name)


def _get_or_create_user(db, phone: str) -> User:
    user = db.query(User).filter(User.phone == phone).first()
    if user is None:
        user = User(phone=phone, first_seen_at=utcnow())
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _try_pick(db, text: str, phone: str):
    """Interpret a reply as a ranked-list pick. Returns the picked fare dict,
    "out_of_range" when the number exceeds the list, "unclear" when the reply
    looks like a pick but no option resolves, or None to fall through to the
    brain (no active list, or not a pick-shaped message)."""
    ctx = chatstate.get_last_fares(db, phone)
    if not ctx or not ctx.get("fares"):
        return None
    m = _PICK_RE.search(text.strip())
    if m:
        n = int(m.group(1))
        if n > len(ctx["fares"]):
            return "out_of_range"
        return ctx["fares"][n - 1]
    # Natural-language pick ("the second one", "Air Peace", "the 7am flight",
    # "the cheapest") resolved by the brain. Gated on pick signals so an
    # unrelated message never costs an AI call.
    if _looks_like_pick(text, ctx["fares"]):
        idx = brain.resolve_pick(text, ctx["fares"])
        if idx is None:
            # A QUESTION about the list ("is that the cheapest?", "are they
            # the same airline?") is NOT a pick - let the brain answer it
            # instead of forcing a 1-2-3 redirect.
            if _QUESTION_RE.search(text):
                return None
            return "unclear"
        return ctx["fares"][idx - 1]
    return None


# A bare "2", "number 2", "option 2", "pick 2", "the 2nd one", "#2".
# Only reached when an active ranked list exists (see _try_pick).
_PICK_RE = re.compile(
    r"^(?:the\s+)?(?:number|option|pick|choose|select)?\s*#?\s*"
    r"([1-9])\s*(?:st|nd|rd|th)?\s*(?:one)?\s*$",
    re.IGNORECASE)

# Cheap gate before the AI pick resolver: only messages that could plausibly
# be picking (ordinals, "one", "cheapest", an am/pm time, ...) spend an AI
# call. Everything else falls through to the brain untouched.
_PICK_SIGNAL_RE = re.compile(
    r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|last|"
    r"one|option|cheapest|cheaper|cheap|best|deal|pick|choose|select)\b"
    r"|\b\d(?:st|nd|rd|th)\b|\b\d{1,2}\s*(?:am|pm)\b", re.IGNORECASE)

_QUESTION_RE = re.compile(
    r"\?|\b(is|are|was|were|do|does|did|can|could|what|which|how|when|"
    r"where)\b", re.IGNORECASE)


def _looks_like_pick(text: str, fares: list) -> bool:
    """True when the message could be choosing a listed fare - so we try the
    pick resolver; False means it is unrelated and should go to the brain."""
    if _PICK_SIGNAL_RE.search(text):
        return True
    flat = text.lower()
    upper = text.upper()
    for f in fares:
        airline = (f.get("airline") or "").lower()
        if airline and any(t in flat for t in airline.split() if len(t) >= 3):
            return True
        fnum = (f.get("flight_number") or "").upper()
        if fnum and fnum in upper:
            return True
        dep = f.get("departs_at")
        if dep and re.match(r"\d{1,2}:\d{2}", str(dep)) and str(dep)[:5] in flat:
            return True
    return False


def _reply_fare(db, user: User, intent: brain.Intent) -> None:
    # A fresh search supersedes any open price-move question.
    chatstate.clear_pending_requote(db, user.phone)
    # Pass 2: destination-only with a date -> search from the user's default
    # hub (Lagos - Nigeria's main base). The fare reply names both cities,
    # so any wrong assumption is visible and correctable.
    origin_iata = intent.origin_iata or "LOS"
    search = LedgerSearch(db)
    fares, surge_fares = search.search_list(origin_iata,
                                            intent.destination_iata,
                                            intent.date, limit=3)
    if not fares:
        if surge_fares:
            # Everything is above the guardrail - flag it, don't fake "no fares".
            surge_price = min(f["price"] for f in surge_fares)
            _say(user.phone,
                 f"Flights are at a surge rate on "
                 f"{city_name(origin_iata)} -> "
                 f"{city_name(intent.destination_iata)} right now. 🚨 Prices "
                 f"are unusually high (from ₦{surge_price:,.0f}) - I won't "
                 f"sell you that. Try again in a bit, or TRACK and I'll Beep "
                 f"you when it normalises.",
                 user.name)
            return
        _say(user.phone,
             f"No fare found {city_name(origin_iata)} -> "
             f"{city_name(intent.destination_iata)} for "
             f"{intent.date or 'that date'}.",
             user.name)
        return

    if len(fares) == 1:
        # Thin route: one result - keep the classic single-fare reply.
        fare = fares[0]
        chatstate.set_last_fare(db, user.phone, {
            "origin_iata": origin_iata,
            "destination_iata": intent.destination_iata,
            "flight_date": fare["flight_date"],
            "price": fare["price"],
            "airline": fare["airline"],
        })
        _say(user.phone,
             f"Fare {city_name(origin_iata)} -> "
             f"{city_name(intent.destination_iata)} {fare['flight_date']}:\n"
             f"₦{fare['price']:,.0f} via {fare['airline']} "
             f"({_fresh_label(fare)})\n"
             f"Verify: {fare['verify_link']}\n"
             f"Reply BOOK - I'll re-confirm the live price before you "
             f"pay - or TRACK to get a Beep when it drops.",
             user.name)
        _send_fare_cards(user, [fare], origin_iata, intent.destination_iata)
        return

    # Ranked list - one option per airline, NARRATED by the brain so it reads
    # like a person presenting choices, not a menu. The numbered options stay
    # pickable (reply 1, 2 or 3 - or just say the airline).
    chatstate.set_last_fares(db, user.phone, {
        "origin_iata": origin_iata,
        "destination_iata": intent.destination_iata,
        "flight_date": fares[0]["flight_date"],
        "fares": fares,
    })
    origin = city_name(origin_iata)
    destination = city_name(intent.destination_iata)
    msg = brain.compose_ranked_reply(
        fares, origin, destination, fares[0]["flight_date"],
        user_name=user.name)
    _say(user.phone, msg, user.name, humanized=True)
    _send_fare_cards(user, fares, origin_iata, intent.destination_iata)


def _fresh_label(fare: dict) -> str:
    """'checked just now' for live results, 'cached ~N min ago' for
    ledger rows - so a quoted fare never reads fresher than it is."""
    from FareBeep.search import fare_freshness
    return fare_freshness(fare) or "price shown as found"


def _booking_total(airline_price: float) -> float:
    from FareBeep.payments import calculate_final_price
    return calculate_final_price(airline_price)["total_amount"]


def _reply_booking(db, user: User, intent: brain.Intent,
                   picked_fare: dict = None) -> None:
    """THE LIVE HANDSHAKE - what happens when a user replies BOOK.

    1. FORCE REFRESH: the Shared Ledger is ignored; SerpApi is queried
       LIVE so the seat exists at the quoted price right now.
    2. Session: a booking_session row is saved with expires_at = now +
       13m by default (10m once the payment method is card).
    3. The WhatsApp/TG call: the Paystack TEST link + the held-total
    message (our 10-minute hold, never a supplier fare lock), with the
    10-minute expiry stated up front.

    picked_fare: when set, the user picked "1, 2 or 3" from a ranked list -
    the route/date come from the list context and the SELECTED flight is
    re-verified live (by flight number) so the price is fresh, never the
    stale list price.

    Date resolution: the intent's own date wins; otherwise the date of the
    LAST fare quote in this chat (so "BOOK" right after a quote books that
    flight). With neither, the bot ASKS - it never silently books today.
    """
    ctx = chatstate.get_last_fare(db, user.phone) or {}
    list_ctx = chatstate.get_last_fares(db, user.phone) or {}
    if picked_fare is not None:
        origin_iata = list_ctx.get("origin_iata")
        destination_iata = list_ctx.get("destination_iata")
        flight_date = list_ctx.get("flight_date")
        if not (origin_iata and destination_iata and flight_date):
            _say(user.phone,
                 "I've lost that fare list - ask me for the fares again "
                 "and I'll re-run the search.",
                 user.name)
            return
    else:
        origin_iata = intent.origin_iata or ctx.get("origin_iata")
        destination_iata = intent.destination_iata or ctx.get("destination_iata")
        flight_date = intent.date or ctx.get("flight_date")
        if not (origin_iata and destination_iata):
            _say(user.phone,
                 "To book: send your route, e.g. BOOK Lagos to Abuja tomorrow "
                 "(or BOOK right after a fare quote I just sent you).",
                 user.name)
            return
        if not flight_date:
            _say(user.phone,
                 "Which date? E.g. BOOK Lagos to Abuja on the 31st - I won't "
                 "book a flight for today without you saying so.",
                 user.name)
            return

    search = LedgerSearch(db)
    expected_price = None
    if picked_fare is not None:
        # Force-refresh the ranked list live and re-locate the picked flight
        # by its number (prices move - never book the stale list price).
        fares, _ = search.search_list(origin_iata, destination_iata,
                                      flight_date, limit=6)
        picked_no = picked_fare.get("flight_number")
        if picked_no:
            fare = next((f for f in fares
                         if f.get("flight_number") == picked_no), None)
        else:
            fare = next((f for f in fares
                         if f.get("airline") == picked_fare.get("airline")
                         and f.get("departs_at") == picked_fare.get("departs_at")),
                        None)
        if fare is None:
            _say(user.phone,
                 "That option is no longer available at that price - the "
                 "seat or fare may have moved. Reply 1, 2 or 3 again, or "
                 "ask me for a fresh search.",
                 user.name)
            return
        expected_price = picked_fare.get("price")
        if fare.get("above_guardrail"):
            _say(user.phone,
                 "That fare is at a surge price right now - I can't lock it "
                 "safely. Try again in a bit, or TRACK it and I'll Beep you "
                 "when it normalises.",
                 user.name)
            return
    else:
        fare = search.search(origin_iata, destination_iata, flight_date,
                             force_refresh=True)
        if fare is None:
            _say(user.phone,
                 "That seat/route is no longer available right now - ask me for "
                 "the latest fare again and I'll re-check live.",
                 user.name)
            return
        if fare.get("above_guardrail"):
            _say(user.phone,
                 "That fare is at a surge price right now - I can't lock it "
                 "safely. Try again in a bit, or TRACK it and I'll Beep you "
                 "when it normalises.",
                 user.name)
            return
        expected_price = ctx.get("price") if ctx else None

    # THE PRICE-MOVE HANDCHECK: the live re-quote may differ from what the
    # user was shown. Within the tolerance (the natural volatility buffer)
    # book silently; beyond it, ASK first - never take a silent price jump.
    if (expected_price is not None
            and abs(fare["price"] - expected_price) > REQUOTE_TOLERANCE_NGN):
        chatstate.clear_last_fares(db, user.phone)   # superseded by the question
        chatstate.set_pending_requote(db, user.phone, {
            "origin_iata": origin_iata,
            "destination_iata": destination_iata,
            "flight_date": fare["flight_date"],
            "fare": fare,
        })
        _say(user.phone,
             f"The fare moved from ₦{expected_price:,.0f} to "
             f"₦{fare['price']:,.0f} while you were deciding - the airline "
             f"repriced it. Still go ahead at the new price? Reply YES or "
             f"BOOK - or NO to cancel.",
             user.name)
        return

    _create_and_send_booking(db, user, origin_iata, destination_iata,
                             fare["flight_date"], fare,
                             flight_iata=fare.get("flight_number")
                             or intent.flight)

    if picked_fare is not None:
        # A pick is consumed by booking: a stray "2" later must not rebook.
        chatstate.clear_last_fares(db, user.phone)


def _create_and_send_booking(db, user: User, origin_iata: str,
                             destination_iata: str, flight_date: str,
                             fare: dict, flight_iata: str = None) -> None:
    """Create the 10-minute booking session and send the held-total
    message with the confirmation-page link. Shared by the direct BOOK flow
    and the re-quoted "yes" flow."""
    bookings = BookingService(db)
    try:
        result = bookings.create_booking(
            user.user_id, origin_iata, destination_iata,
            flight_date, fare["price"],
            flight_iata=flight_iata,
            email=user.email or f"{user.phone.replace('+', '')}@farebeep.ng",
            airline=fare.get("airline"),
            source="serpapi")
    except Exception as e:
        logger.error("Booking creation failed: %s", e)
        _say(user.phone, "Payment link could not be created. Try again in a minute.", user.name)
        return

    session = result["session"]
    expires = result["expires_at"].strftime("%H:%M")
    origin = city_name(origin_iata)
    destination = city_name(destination_iata)
    # The user goes to the booking confirmation page (NOT the raw Paystack
    # URL): it reconfirms the price breakdown + captures NDPA consent before
    # redirecting to payment. Every booking flows through this page.
    book_url = f"{APP_BASE_URL}/book/{session.id}"
    _say(user.phone,
         f"🔒 Total held for 10 minutes - our promise, not the airline's: "
         f"no supplier holds seats for us, so if the fare moves again "
         f"before you pay, tell us and we'll make it right.\n"
         f"{origin} -> {destination} on {flight_date}\n"
         f"Airline price (re-checked live just now): "
         f"₦{fare['price']:,.0f}\n"
         f"Markup + fees: ₦{session.markup + session.processing_fee:,.0f}\n"
         f"TOTAL:         ₦{result['total_amount']:,.0f}\n\n"
         f"Confirm & pay here (valid until {expires} today):\n"
         f"{book_url}\n\n"
         f"⚠️ PAYSTACK TEST MODE - use the test card, no real money leaves "
         f"your account.\n"
         f"Miss the window? Your payment is auto-refunded - no questions.",
         user.name)


def _reply_status_ack(user: User, intent: brain.Intent) -> None:
    flight = intent.flight or "your flight"
    _say(user.phone,
         f"Status watch for {flight} is attached to a paid booking. "
         f"We'll text you via template 3h before departure if anything changes.",
         user.name)


def _reply_subscribe(db, user: User, intent: brain.Intent) -> None:
    """Set a fare-drop subscription (target price optional).

    Like BOOK: a bare TRACK right after a fare quote arms the alert on the
    ROUTE + DATE the user was just shown (never a made-up route). The route
    lives in the single-fare context OR the active ranked list - both count
    as "the route we were just discussing"."""
    from FareBeep.alerts import SubscriptionMonitor
    ctx = chatstate.get_last_fare(db, user.phone) or {}
    list_ctx = chatstate.get_last_fares(db, user.phone) or {}
    origin_iata = (intent.origin_iata or ctx.get("origin_iata")
                   or list_ctx.get("origin_iata"))
    destination_iata = (intent.destination_iata or ctx.get("destination_iata")
                        or list_ctx.get("destination_iata"))
    if not (origin_iata and destination_iata):
        _say(user.phone,
             "To set a price alert, send your route, e.g. 'TRACK Lagos to "
             "Abuja below 80k' - or just say TRACK right after a fare quote.",
             user.name)
        return
    target_date = (intent.date or ctx.get("flight_date")
                   or list_ctx.get("flight_date"))

    monitor = SubscriptionMonitor(db)
    monitor.subscribe(user.user_id, origin_iata, destination_iata,
                      target_price=intent.target_price, target_date=target_date)

    origin = city_name(origin_iata)
    destination = city_name(destination_iata)
    if intent.target_price is not None:
        msg = (f"✅ Beep armed: {origin} -> {destination}\n"
               f"We'll text you the moment it hits ₦{intent.target_price:,.0f} or lower.")
        from FareBeep.alerts import target_realism_note
        note = target_realism_note(db, origin_iata, destination_iata,
                                   intent.target_price)
        if note:
            msg += f"\n\n{note}"
    else:
        msg = (f"✅ Beep armed: {origin} -> {destination}\n"
               f"We'll text you when the fare drops by 10% or more.")
    _say(user.phone, msg, user.name)


def _tap_beep_by_phone(phone: str, sub_id: int) -> None:
    """Background task for a beep template's "Book now" tap: re-present
    that subscription's route fresh (never books blind). Own session,
    like _handle_incoming_message - never the request's."""
    db = SessionLocal()
    try:
        try:
            user = _get_or_create_user(db, phone)
            _tap_beep(db, user, sub_id)
        except Exception as e:
            logger.error("Beep-tap handling failed (%s): %s", phone, e)
    finally:
        db.close()


def _tap_beep(db, user: User, sub_id: int) -> None:
    """"Book now" from a price-drop Beep: look up the subscription (it
    must belong to THIS user - foreign ids are ignored silently) and
    run a fresh fare search for its route+date. The user then picks
    and books from live numbers, exactly like a typed search."""
    from FareBeep.models import Subscription
    sub = db.query(Subscription).filter_by(id=sub_id).first()
    if sub is None:
        _say(user.phone,
             "That alert has expired - send me your route and I'll "
             "check fresh fares for you.",
             user.name)
        return
    if sub.user_id != user.user_id:
        logger.warning("Beep tap rejected: sub %s not owned by %s",
                       sub_id, user.phone)
        return
    intent = brain.Intent(intent="fare",
                          origin=city_name(sub.origin),
                          destination=city_name(sub.destination),
                          date=sub.target_date)
    logger.info("Beep tap: fresh search %s->%s for %s",
                sub.origin, sub.destination, user.phone)
    _reply_fare(db, user, intent)


def _tap_alert_by_phone(phone: str, idx: int) -> None:
    """Background task for a card's "Set alert" tap: subscribe from the
    fare the user was just shown (ranked list first, single quote next).
    Own session, like _handle_incoming_message - never the request's."""
    db = SessionLocal()
    try:
        try:
            user = _get_or_create_user(db, phone)
            _tap_alert(db, user, idx)
        except Exception as e:
            logger.error("Alert-tap handling failed (%s): %s", phone, e)
    finally:
        db.close()


def _tap_alert(db, user: User, idx: int) -> None:
    """Subscribe the tapped fare's route+date, target = its price ("beep
    me below this"). idx is 1-based like the cards and the pick gate
    (alert:1 = first fare); 0 = single-fare card backed by last_fare.
    Mirrors _reply_subscribe's confirm wording."""
    from FareBeep.alerts import SubscriptionMonitor
    ctx = chatstate.get_last_fares(db, user.phone) or {}
    fares = ctx.get("fares") or []
    fare = fares[idx - 1] if 1 <= idx <= len(fares) else None
    origin_iata = ctx.get("origin_iata")
    destination_iata = ctx.get("destination_iata")
    if fare is None:
        # Single-fare card (alert:0): the quote lives in last_fare.
        single = chatstate.get_last_fare(db, user.phone) or {}
        if not single.get("price"):
            _say(user.phone,
                 "That fare list has expired - ask me for the fares again "
                 "and I'll re-run the search.",
                 user.name)
            return
        fare, origin_iata, destination_iata = (
            single, single.get("origin_iata"), single.get("destination_iata"))
    if not (origin_iata and destination_iata):
        _say(user.phone,
             "That fare list has expired - ask me for the fares again "
             "and I'll re-run the search.",
             user.name)
        return
    monitor = SubscriptionMonitor(db)
    monitor.subscribe(user.user_id, origin_iata, destination_iata,
                      target_price=fare.get("price"),
                      target_date=fare.get("flight_date"))
    _say(user.phone,
         f"✅ Beep armed: {city_name(origin_iata)} -> "
         f"{city_name(destination_iata)}\n"
         f"We'll text you the moment it hits "
         f"₦{fare.get('price', 0):,.0f} or lower.",
         user.name)


def _send_fare_cards(user: User, fares: list, origin_iata: str,
                     destination_iata: str) -> None:
    """Follow a fare text with tappable flight cards (Meta channel only -
    other channels keep the text they already got). One card per fare,
    logo on top, price under, Book + Set-alert buttons. idx is 1-based
    so taps reuse the "reply 1, 2, 3" pick gate; a lone fare uses the
    bare BOOK / last_fare context instead."""
    if not hasattr(notifier, "send_interactive_card"):
        return  # non-Meta channel (Telegram/Twilio) - keep the text reply
    single = len(fares) == 1
    for i, fare in enumerate(fares[:3], start=1):
        idx = 0 if single else i
        notifier.send_interactive_card(
            user.phone,
            body=cards.card_body(fare, origin_iata, destination_iata),
            buttons=cards.card_buttons(fare, idx),
            image_url=cards.logo_for(fare.get("airline")),
            footer="FareBeep \u2022 verified just now")


def _reply_unsubscribe(db, user: User) -> None:
    """Remove all subscriptions for the user (NDPA-style data removal)."""
    from FareBeep.alerts import SubscriptionMonitor
    removed = SubscriptionMonitor(db).unsubscribe(user.user_id)
    if removed:
        _say(user.phone,
             f"🔕 {removed} price alert(s) removed. Your route data is deleted.",
             user.name)
    else:
        _say(user.phone, "You have no active price alerts.", user.name)


def _help_text() -> str:
    return ("Beep! 🎫 FareBeep is your fast Nigerian flight concierge. "
            "Just tell me where and when, e.g. 'Lagos to Abuja tomorrow'\n"
            "      'BOOK Lagos to Abuja' (reserve in 10 mins)\n"
            "      'TRACK Lagos to Abuja below 80k' (price alert)\n"
            "      'Track P47123' (flight status)")


# ---------------------------------------------------------------------------
# Twilio WhatsApp Sandbox Webhook - TEST CHANNEL
# ---------------------------------------------------------------------------
def _verify_twilio_signature(raw_url: str, form: dict, signature: str) -> bool:
    """Compare X-Twilio-Signature (base64 HMAC-SHA1 over the URL + POST
    params, keyed by the account auth token) the way Twilio ships it."""
    if not signature or not MESSAGING_PROVIDER.lower() == "twilio":
        return False
    from twilio.request_validator import RequestValidator
    try:
        validator = RequestValidator(os.getenv("TWILIO_AUTH_TOKEN") or "")
        return validator.validate(raw_url, form, signature)
    except Exception as e:
        logger.warning("Twilio signature validation error: %s", e)
        return False


@app.post("/webhook/twilio")
async def twilio_webhook(request: Request, background: BackgroundTasks):
    """Twilio sandbox receiver (form-encoded, x-twilio-signature verified).

    Replies are sent by the background task THROUGH the REST API, so this
    handler always answers Twilio with an empty TwiML ack instantly.
    """
    form = dict(await request.form())
    sig = request.headers.get("X-Twilio-Signature", "")
    if not _verify_twilio_signature(str(request.url), form, sig):
        logger.warning("Twilio webhook REJECTED: bad X-Twilio-Signature")
        return Response(status_code=403)

    phone = str(form.get("From", "")).removeprefix("whatsapp:")
    text = str(form.get("Body", ""))
    if text and phone:
        background.add_task(_handle_incoming_message, phone, text)
    return Response(content="<?xml version='1.0' encoding='UTF-8'?><Response></Response>",
                    media_type="application/xml")


@app.get("/webhook/twilio")
async def twilio_verify(request: Request):
    """Twilio sandbox webhook settings may fire a GET before the POST."""
    return PlainTextResponse("FareBeep Twilio webhook is live")


# ---------------------------------------------------------------------------
# Telegram Bot API Webhook - FASTEST TEST CHANNEL
# ---------------------------------------------------------------------------
def _verify_telegram_secret(secret: str) -> bool:
    """X-Telegram-Bot-Api-Secret-Token set via setWebhook(secret_token)."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET") or ""
    return bool(expected) and hmac.compare_digest(expected, secret)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request, background: BackgroundTasks):
    """Telegram Bot API receiver (JSON, secret-token header verified).

    The chat_id IS the user identity (stored where a phone number would
    live on the WhatsApp channels), so the whole conversational pipeline
    is reused unchanged.
    """
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not _verify_telegram_secret(secret):
        logger.warning("Telegram webhook REJECTED: bad secret token")
        return Response(status_code=403)

    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = str(message.get("text") or "")
    if text and chat_id:
        # typing bubble runs as a background task (FIFO, before the reply
        # task): a dead Telegram API must never delay the 200 ack
        background.add_task(_telegram_typing, chat_id)
        background.add_task(_handle_incoming_message, chat_id, text)
    return {"ok": True}


def _telegram_typing(chat_id: str) -> None:
    from FareBeep.notifier import TelegramBot
    TelegramBot().send_action(chat_id)  # typing… (best-effort)


@app.get("/webhook/telegram")
async def telegram_verify(request: Request):
    return PlainTextResponse("FareBeep Telegram webhook is live")


# ---------------------------------------------------------------------------
# Admin ops - dead-letter visibility + replay (X-Admin-Token guarded)
# ---------------------------------------------------------------------------
def _admin_ok(request: Request) -> bool:
    """Shared-secret gate. ADMIN_TOKEN unset = the admin surface stays
    closed; 404 (not 403) so the endpoint's existence isn't advertised."""
    expected = ADMIN_TOKEN or ""
    if not expected:
        return False
    provided = request.headers.get("X-Admin-Token", "")
    return bool(provided) and hmac.compare_digest(expected, provided)


@app.get("/admin/ops")
def admin_ops(request: Request):
    """One ops snapshot: inbound dispatch health, dead letters, outbound
    receipts, beeps, bookings, and which brain answered. Read-only."""
    if not _admin_ok(request):
        return Response(status_code=404)
    db = SessionLocal()
    try:
        inbound = dict(db.query(
            ProcessedMessage.status, func.count(ProcessedMessage.message_id)
        ).group_by(ProcessedMessage.status).all())
        from datetime import timedelta
        stale_queued = db.query(ProcessedMessage).filter(
            ProcessedMessage.status == "queued",
            ProcessedMessage.created_at < utcnow() - timedelta(minutes=5)
        ).count()
        dead_rows = db.query(ProcessedMessage).filter(
            ProcessedMessage.status == "failed"
        ).order_by(ProcessedMessage.created_at.desc()).limit(20).all()
        dead = [{
            "message_id": r.message_id,
            "phone": r.phone,
            "attempts": r.attempts,
            "last_error": (r.last_error or "")[:300],
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "replayable": bool((r.payload or {}).get("message_id")),
        } for r in dead_rows]
        receipts = dict(db.query(
            DeliveryReceipt.status, func.count(DeliveryReceipt.message_id)
        ).group_by(DeliveryReceipt.status).all())
        subs = db.query(Subscription).all()
        bookings = db.query(BookingSession.status,
                            func.count(BookingSession.id)
                            ).group_by(BookingSession.status).all()
        return {
            "inbound": {"by_status": inbound, "stale_queued_5m": stale_queued},
            "dead_letters_recent": dead,
            "delivery_receipts": {"by_status": receipts},
            "beeps": {
                "total": len(subs),
                "active": sum(1 for s in subs if not s.paused),
                "paused": sum(1 for s in subs if s.paused),
            },
            "bookings": {str(getattr(k, "value", k)): v for k, v in bookings},
            "agent_this_process": dict(_agent_stats),
        }
    finally:
        db.close()


@app.post("/admin/ops/dead-letters/{message_id}/replay")
def admin_replay_dead_letter(message_id: str, request: Request):
    """Re-dispatch one dead-lettered inbound from its stored payload,
    through the exact recovery path (per-phone lock, single turn).
    Synchronous on purpose: the caller sees the new terminal state."""
    if not _admin_ok(request):
        return Response(status_code=404)
    db = SessionLocal()
    try:
        row = db.query(ProcessedMessage).filter(
            ProcessedMessage.message_id == message_id).first()
        if row is None or row.status != "failed":
            return JSONResponse({"replayed": False,
                                 "reason": "not a dead letter"}, status_code=404)
        payload = row.payload or {}
        if not payload.get("message_id"):
            return JSONResponse({"replayed": False,
                                 "reason": "no stored payload (pre-recovery row)"})
        m = _restore_message(payload)
        phone = m.from_number or row.phone or ""
        if not phone:
            return JSONResponse({"replayed": False,
                                 "reason": "no phone on payload"})
        with _phone_lock(phone):
            try:
                _dispatch_single(m)
                _mark_processed(m.message_id, "done")
                return {"replayed": True, "message_id": m.message_id}
            except Exception as e:
                _mark_processed(m.message_id, "failed", error=f"replay: {e}")
                return JSONResponse({"replayed": False,
                                     "reason": str(e)[:300]})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ElevenLabs Server Tools - the conversational agent's fare API
# ---------------------------------------------------------------------------
async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_dict(value) -> dict:
    """Accept a dict or a JSON string (ElevenLabs declares objects to its
    model as type string, so callers send '{"adults":1,...}' as text).
    Anything else -> {} (callers apply their own defaults)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _verify_tool_auth(request: Request) -> bool:
    """Shared-secret check for ElevenLabs webhook-tool calls.

    In the ElevenLabs dashboard each tool gets a custom auth header
    (X-FareBeep-Tool-Secret: <ELEVENLABS_TOOL_SECRET>). When the secret
    is unset the tools stay open (local dev only - production must set
    it, otherwise anyone on the internet can trigger bookings).
    """
    if not ELEVENLABS_TOOL_SECRET:
        logger.warning("ELEVENLABS_TOOL_SECRET unset - /tools/* open")
        return True
    return hmac.compare_digest(
        request.headers.get("X-FareBeep-Tool-Secret", ""),
        ELEVENLABS_TOOL_SECRET)


def _tool_fare_summary(fare: dict, source: str, origin: str,
                       destination: str, flight_date: str) -> dict:
    """Human-readable fare summary for the AI to present (Airline, Time,
    Price), plus the machine fields it needs to book (booking_token)."""
    airline = fare.get("airline_name") or fare.get("airline") or "Airline"
    legs = ""
    if fare.get("departure_time") or fare.get("arrival_time"):
        legs = (f", {fare.get('departure_code') or origin} "
                f"{fare.get('departure_time') or ''}->"
                f"{fare.get('arrival_code') or destination} "
                f"{fare.get('arrival_time') or ''}")
    flight = f" {fare['flight_no']}" if fare.get("flight_no") else ""
    price = fare["price"]
    return {
        "found": True,
        "source": source,
        "summary": (f"{airline}{flight}, {origin}->{destination} on "
                    f"{flight_date}{legs}, NGN {price:,.0f}"),
        "airline": airline,
        "flight_no": fare.get("flight_no"),
        "departs_at": fare.get("departure_time"),
        "arrives_at": fare.get("arrival_time"),
        "price_ngn": price,
        "currency": fare.get("currency") or "NGN",
        "booking_token": (fare.get("booking_token")
                          if source == "247travels" else None),
        "origin": origin,
        "destination": destination,
        "flight_date": flight_date,
        "note": ("Prices are unusually high right now."
                 if fare.get("above_guardrail") else None),
    }


@app.api_route("/tools/search", methods=["GET", "POST"])
async def tools_search(request: Request):
    """ElevenLabs server tool: ledger-first fare lookup.

    Args (query or JSON): origin, destination (city or IATA), flight_date
    (YYYY-MM-DD), adults (default 1), phone (optional, log attribution).
    A ledger hit (<500ms, free) wins;
    on a miss Travels247 is queried live (search_mode=external) and the
    cheapest offer is UPSERTed into the ledger for the community.
    """
    if not _verify_tool_auth(request):
        logger.warning("/tools/search REJECTED: bad tool secret")
        return JSONResponse(status_code=403, content={
            "found": False, "error": "forbidden"})
    args = dict(request.query_params) if request.method == "GET" \
        else await _json_body(request)
    origin = resolve_iata(args.get("origin") or "")
    destination = resolve_iata(args.get("destination") or "")
    flight_date = str(args.get("flight_date") or "")[:10]
    adults = _as_int(args.get("adults"), 1)
    adults = max(1, adults)  # supplier rejects 0/negative passenger counts
    caller = str(args.get("phone") or "")
    if not origin or not destination or not flight_date:
        return JSONResponse(status_code=422, content={
            "found": False,
            "error": "origin, destination and flight_date (YYYY-MM-DD) "
                     "are required"})
    db = SessionLocal()
    try:
        ledger = LedgerSearch(db, live=LedgerOnlyEngine())
        hit = ledger.search(origin, destination, flight_date, verify=False)
        if hit is not None:
            logger.info("tools/search ledger hit %s->%s %s (caller=%s)",
                        origin, destination, flight_date, caller)
            return _tool_fare_summary(hit, "ledger", origin, destination,
                                      flight_date)
        sky = Travels247Client()
        try:
            offers = await sky.search_offers(origin, destination,
                                             flight_date, adults=adults)
        finally:
            await sky.close()
        best = pick_cheapest(offers)
        if best is None:
            return {"found": False, "source": "247travels",
                    "error": f"no live offers for {origin}->{destination} "
                             f"on {flight_date}"}
        # The ledger keeps the price (no public URL exists for a Travels247
        # offer, so verify_link stays empty; the booking_token travels in
        # this response, and /tools/reserve mints a fresh one anyway).
        # Surge-guarded, no 90s hold on the voice path (reserve re-verifies).
        ledger._verify_and_upsert(origin, destination, flight_date, {
            "price": best["price"], "currency": best["currency"],
            "airline": best["airline_name"], "verify_link": None},
            verify=False)
        return _tool_fare_summary(best, "247travels", origin, destination,
                                  flight_date)
    finally:
        db.close()


@app.post("/tools/reserve")
async def tools_reserve(request: Request):
    """ElevenLabs server tool: price-lock + Paystack link (payment FIRST).

    Travels247 creates a LIVE PNR on every /reserve call, so FareBeep NEVER
    calls Travels247 reserve here - it re-verifies the price live, opens the
    10-minute booking_session (13 for bank transfer), and returns the
    Paystack link. The PNR is issued in /webhook/paystack after
    charge.success. Body: {phone, booking_token? | origin+destination+
    flight_date, passengers?, travellers?, payment_method?}.
    """
    if not _verify_tool_auth(request):
        logger.warning("/tools/reserve REJECTED: bad tool secret")
        return JSONResponse(status_code=403, content={
            "locked": False, "error": "forbidden"})
    body = await _json_body(request)
    phone = str(body.get("phone") or "").strip()
    if not phone:
        return JSONResponse(status_code=422, content={
            "locked": False, "error": "phone is required"})
    pax = _as_dict(body.get("passengers"))
    adults = _as_int(pax.get("adults"), 1)
    adults = max(1, adults)  # supplier rejects 0/negative passenger counts
    children = _as_int(pax.get("children"), 0)
    infants = _as_int(pax.get("infants"), 0)
    db = SessionLocal()
    try:
        user = _get_or_create_user(db, phone)
        sky = Travels247Client()
        try:
            if body.get("booking_token"):
                origin = resolve_iata(body.get("origin") or "")
                destination = resolve_iata(body.get("destination") or "")
                flight_date = str(body.get("flight_date") or "")[:10]
                airline = body.get("airline")
                if not origin or not destination or not flight_date:
                    return JSONResponse(status_code=422, content={
                        "locked": False,
                        "error": "token bookings still need origin, "
                                 "destination and flight_date for the lock"})
                priced = await sky.verify_price(
                    body["booking_token"], adults=adults,
                    children=children, infants=infants)
            else:
                origin = resolve_iata(body.get("origin") or "")
                destination = resolve_iata(body.get("destination") or "")
                flight_date = str(body.get("flight_date") or "")[:10]
                if not origin or not destination or not flight_date:
                    return JSONResponse(status_code=422, content={
                        "locked": False,
                        "error": "booking_token or origin+destination+"
                                 "flight_date is required"})
                offers = await sky.search_offers(
                    origin, destination, flight_date, adults=adults,
                    children=children, infants=infants)
                best = pick_cheapest(offers)
                if best is None:
                    return {"locked": False, "source": "247travels",
                            "error": f"no live offers for "
                                     f"{origin}->{destination} on {flight_date}"}
                priced = await sky.verify_price(
                    best["booking_token"], adults=adults,
                    children=children, infants=infants)
                airline = best["airline_name"]
        except Travels247Error as e:
            logger.warning("tools/reserve Travels247 failed: %s", e)
            return JSONResponse(status_code=502, content={
                "locked": False, "error": f"live price check failed: {e}"})
        finally:
            await sky.close()
        bookings = BookingService(db)
        try:
            result = bookings.create_booking(
                user.user_id, origin, destination, flight_date,
                priced["verified_price"], airline=airline, source="247travels",
                payment_method=body.get("payment_method"))
        except PaystackError as e:
            logger.warning("tools/reserve Paystack link failed: %s", e)
            return JSONResponse(status_code=502, content={
                "locked": False, "error": f"payment link failed: {e}"})
        session = result["session"]
        details = dict(session.flight_details or {})
        details.update({
            "booking_token": priced["booking_token"],
            "passengers": {"adults": adults, "children": children,
                           "infants": infants},
            "travellers": _as_dict(body.get("travellers")),
        })
        session.flight_details = details
        db.commit()
        total = result["total_amount"]
        link = result["payment_link"]
        return {
            "locked": True,
            "payment_link": link,
            "total_amount": total,
            "payment_ref": session.payment_ref,
            "expires_at": result["expires_at"].isoformat(),
            "summary": (f"Locked {airline or 'flight'} {origin}->"
                        f"{destination} {flight_date} at NGN {total:,.0f}. "
                        f"Pay within 10 minutes: {link}"),
        }
    finally:
        db.close()


async def _maybe_issue_247travels_ticket(session,
                                      fallback_pnr: str) -> tuple:
    """Paid webhook: turn the stored Travels247 booking_token into a real PNR.

    Runs only when Travels247 creds are configured AND the session carries a
    booking_token + travellers (stored by /tools/reserve). Anything
    missing, or any Travels247 failure -> (fallback_pnr, provisional note):
    the booking stays PAID and the mock-PNR path is preserved.
    """
    provisional = " (provisional)"
    pending_note = ("Your e-ticket is issued once the airline confirms "
                    "availability.")
    details = session.flight_details or {}
    token, travellers = details.get("booking_token"), details.get("travellers")
    if not (TRAVELS247_EMAIL and TRAVELS247_PASSWORD):
        return fallback_pnr, pending_note, provisional
    if not token or not travellers:
        logger.info("Travels247 ticketing skipped for %s (no token/travellers)",
                    session.payment_ref)
        return fallback_pnr, pending_note, provisional
    sky = Travels247Client()
    try:
        pax = details.get("passengers") or {}
        priced = await sky.verify_price(
            token, adults=_as_int(pax.get("adults"), 1),
            children=_as_int(pax.get("children"), 0),
            infants=_as_int(pax.get("infants"), 0))
        res = await sky.reserve(
            priced["booking_token"], travellers,
            adults=_as_int(pax.get("adults"), 1),
            children=_as_int(pax.get("children"), 0),
            infants=_as_int(pax.get("infants"), 0))
        logger.info("TRAVELS247 TICKET %s -> PNR %s",
                    session.payment_ref, res["pnr"])
        deadline = (f" Ticket deadline: {res['ticket_deadline']}."
                    if res.get("ticket_deadline") else "")
        return res["pnr"], f"E-ticket issued.{deadline}", ""
    except Travels247Error as e:
        logger.error("Travels247 ticketing failed for %s: %s - keeping "
                     "provisional PNR", session.payment_ref, e)
        return fallback_pnr, pending_note, provisional
    finally:
        await sky.close()


# ---------------------------------------------------------------------------
# Paystack webhook - the other half of the settlement loop
# ---------------------------------------------------------------------------
def _notify_admin(text: str) -> None:
    """Best-effort admin alert (Refund Required etc.). Goes to
    ADMIN_ALERT_PHONE if configured, else the log (a real ops channel can
    be wired without touching the loop)."""
    from FareBeep.config import ADMIN_ALERT_PHONE
    if ADMIN_ALERT_PHONE:
        try:
            notifier.send_text(ADMIN_ALERT_PHONE, text)
            logger.info("Admin alert sent to %s", ADMIN_ALERT_PHONE)
            return
        except Exception as e:
            logger.error("Admin alert send failed: %s", e)
    logger.warning("ADMIN ALERT (no recipient configured): %s", text)


def _notify_session_user(session, text: str) -> None:
    """Send an outbound message to the session's owner (WhatsApp number or
    Telegram chat_id - whatever the user's phone column holds)."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == session.user_id).first()
    finally:
        db.close()
    if user is None:
        logger.warning("Session %s has no user row - message not sent: %s",
                       session.payment_ref, text)
        return
    notifier.send_text(user.phone, text)


@app.post("/webhook/paystack")
async def paystack_webhook(request: Request):
    """Paystack event receiver - X-Paystack-Signature (HMAC-SHA512) verified
    over the RAW body. On charge.success the booking is gated by expires_at:

      now() <= expires_at  -> status = paid, mock Ticket Issued (PNR FB-XXXX)
      now() >  expires_at  -> status = expired, Refund Required admin alert
                              + the user is told a refund/price-match is coming
    """
    raw = await request.body()
    if not verify_paystack_signature(
            raw, request.headers.get("x-paystack-signature", "")):
        logger.warning("Paystack webhook REJECTED: bad signature")
        return Response(status_code=403)

    payload = await request.json()
    event = payload.get("event", "")
    data = payload.get("data") or {}
    reference = str(data.get("reference", ""))
    status = str(data.get("status", ""))

    db = SessionLocal()
    try:
        bookings = BookingService(db)
        outcome = bookings.settle_payment(reference, status)
        logger.info("Paystack %s -> %s", reference, outcome["outcome"])

        session = outcome.get("session")
        if outcome["outcome"] == "paid":
            pnr, ticket_note, provisional = await _maybe_issue_247travels_ticket(
                session, outcome.get("pnr") or "FB-????")
            _notify_session_user(
                session,
                f"✅ BOOKING CONFIRMED - payment received!\n"
                f"PNR: {pnr}{provisional}\n"
                f"{city_name(session.origin)} -> {city_name(session.destination)} "
                f"{session.flight_date}\n"
                f"Paid: ₦{session.total_price:,.0f}\n"
                f"{ticket_note} Safe travels!")
        elif outcome["outcome"] == "refund_required":
            _notify_session_user(
                session,
                "Payment was successful, but the 10-minute window closed. "
                "Your refund is being processed automatically - no "
                "questions.")
            # THE REFUND PROMISE: the airline was never ticketed, so the
            # money goes back via the Paystack API - not just an alert.
            refunded = False
            try:
                from FareBeep.payments import refund_paystack_transaction
                refund_paystack_transaction(reference)
                refunded = True
            except Exception as e:
                logger.error("Auto-refund failed for %s: %s", reference, e)
            if refunded:
                tail = "No action needed - Paystack refund issued."
            else:
                tail = ("MANUAL REFUND REQUIRED - the Paystack refund call "
                        "failed.")
            _notify_admin(
                f"{'AUTO-REFUNDED' if refunded else 'REFUND FAILED'}: "
                f"{reference} was paid after its 10-minute lock expired "
                f"({session.expires_at}); the airline API was NOT called. "
                f"{tail}")
        return {"ok": True, "outcome": outcome["outcome"]}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    # Vendor contract drift probe: when the fare source's payload changes a
    # pinned field name, `providers.probe_contract` reports it here so churn
    # shows up as a health signal before users see a broken quote.
    from FareBeep import providers
    probe = providers.tiqwa_probe()
    if probe is None:
        return {"status": "ok", "service": "FareBeep"}
    return {"status": "ok" if probe["ok"] else "degraded",
            "service": "FareBeep", "fare_provider_probe": probe}


# ---------------------------------------------------------------------------
# Booking confirmation page - the ONLY place a user proceeds to payment.
# The WhatsApp bot sends a link here instead of a raw Paystack URL. The page
# reconfirms the quoted price (breakdown) and captures NDPA consent (version
# + timestamp + phone) BEFORE the Paystack redirect. All bookings flow
# through this page, so consent is always captured - no text-parsing needed.
# ---------------------------------------------------------------------------
_CONSENT_TEXT = (
    "By proceeding, you agree that FareBeep may collect and process your "
    "details (and, for ticket purchases, passenger/travel document data) "
    "for the purpose of booking, pricing and confirming your flight, "
    "sending you fare alerts, and contacting you about your booking. "
    "We do not share your data with third parties for their own marketing. "
    "You can stop alerts anytime by replying STOP. "
    "This page confirms your INTENT to purchase the flight shown at the "
    "quoted total - the price is locked for 10 minutes, and your ticket is "
    "issued after payment is confirmed by our payment partner. "
    "This is our current data notice (version {version}).")


def _as_naive(dt):
    """Normalise a datetime for safe comparison.

    Postgres returns timestamptz columns as offset-NAIVE datetimes, while
    models.utcnow() is offset-aware - comparing the two raises TypeError.
    Strip tzinfo from both sides before comparing (all values are UTC).
    """
    return dt.replace(tzinfo=None) if dt is not None else None


def _is_expired(session: BookingSession) -> bool:
    return _as_naive(session.expires_at) < _as_naive(utcnow())


def _booking_not_found() -> HTMLResponse:
    return HTMLResponse("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Booking</title></head>
<body style="font-family:system-ui,sans-serif;background:#0b1220;color:#e8eef7;
display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#131c2e;border:1px solid #26324a;border-radius:16px;
padding:2.5rem;max-width:26rem;text-align:center">
<h1>Booking not found</h1>
<p style="color:#9fb0c9">This link is invalid or already used. Ask the bot
for a fresh fare and booking in WhatsApp.</p>
</div></body></html>""")


def _booking_closed(origin: str = "", destination: str = "") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Window closed</title></head>
<body style="font-family:system-ui,sans-serif;background:#0b1220;color:#e8eef7;
display:grid;place-items:center;min-height:100vh;margin:0">
<div style="background:#131c2e;border:1px solid #26324a;border-radius:16px;
padding:2.5rem;max-width:26rem;text-align:center">
<h1>Price window closed</h1>
<p style="color:#9fb0c9">The 10-minute price lock for
{origin} &rarr; {destination} has expired. Send a new message in WhatsApp
(e.g. &ldquo;BOOK Lagos to Abuja tomorrow&rdquo;) and I&rsquo;ll re-check
the latest fare for you.</p>
</div></body></html>""")


def _booking_page(session: BookingSession) -> HTMLResponse:
    origin = city_name(session.origin)
    destination = city_name(session.destination)
    airline = (session.flight_details or {}).get("airline") or "—"
    expires = session.expires_at.strftime("%H:%M")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Confirm booking</title>
<style>
 body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif;
        background:#0b1220; color:#e8eef7; display:grid; place-items:center;
        min-height:100vh; margin:0; }}
 .card {{ background:#131c2e; border:1px solid #26324a; border-radius:16px;
         padding:2rem; max-width:26rem; width:100%; box-sizing:border-box; }}
 h1 {{ font-size:1.25rem; margin:0 0 .25rem; }}
 .route {{ color:#9fb0c9; margin:0 0 1.25rem; }}
 .row {{ display:flex; justify-content:space-between; padding:.35rem 0;
         border-bottom:1px solid #1f2a3f; color:#9fb0c9; }}
 .row.total {{ border-bottom:none; color:#e8eef7; font-weight:700;
               font-size:1.1rem; }}
 .locked {{ font-size:.8rem; color:#6ee7a0; margin:1rem 0; }}
 .notice {{ background:#101a2c; border:1px solid #26324a; border-radius:10px;
           padding:.9rem; font-size:.8rem; color:#9fb0c9; line-height:1.5;
           margin:1rem 0; }}
 button {{ width:100%; background:#22c55e; color:#06210f; border:0;
          border-radius:10px; padding:.85rem; font-size:1rem; font-weight:700;
          cursor:pointer; }}
</style></head>
<body>
<div class="card">
  <h1>Confirm your booking</h1>
  <p class="route">{origin} &rarr; {destination} &middot; {session.flight_date} &middot; {airline}</p>

  <div class="row"><span>Airline price</span><span>&#8358;{session.airline_price:,.0f}</span></div>
  <div class="row"><span>Markup + fees</span><span>&#8358;{session.markup + session.processing_fee:,.0f}</span></div>
  <div class="row total"><span>Total to pay</span><span>&#8358;{session.total_price:,.0f}</span></div>

  <p class="locked">Price locked, valid until {expires} today. Payments after
  the window are auto-refunded.</p>

  <div class="notice">{_CONSENT_TEXT.format(version=CONSENT_VERSION)}</div>

  <form method="post" action="/book/{session.id}/confirm">
    <button type="submit">I agree &amp; Proceed to Payment</button>
  </form>
</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/book/{session_id}")
def booking_page(session_id: uuid.UUID):
    db = SessionLocal()
    try:
        session = db.get(BookingSession, session_id)
        if session is None:
            return _booking_not_found()
        if _is_expired(session):
            return _booking_closed(session.origin, session.destination)
        return _booking_page(session)
    finally:
        db.close()


@app.post("/book/{session_id}/confirm")
def booking_confirm(session_id: uuid.UUID):
    """Record NDPA consent for the session's user, then send them to Paystack."""
    db = SessionLocal()
    try:
        session = db.get(BookingSession, session_id)
        if session is None:
            return _booking_not_found()
        if _is_expired(session):
            return _booking_closed(session.origin, session.destination)
        user = db.get(User, session.user_id)
        if user is not None:
            user.consent_at = utcnow()
            user.consent_text_version = CONSENT_VERSION
            db.commit()
            logger.info("Consent recorded v%s for user %s (booking %s)",
                        CONSENT_VERSION, session.user_id, session.payment_ref)
        return RedirectResponse(session.callback_url or "/payment/status",
                                status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Payment status page - where Paystack sends the user after checkout
# (PAYSTACK_CALLBACK_URL). The actual settlement happens in the webhook;
# this page is just a friendly confirmation screen.
# ---------------------------------------------------------------------------
@app.get("/payment/status")
def payment_status(reference: str = ""):
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareBeep - Payment</title>
<style>
  body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif;
         background: #0b1220; color: #e8eef7; display: grid; place-items: center;
         min-height: 100vh; margin: 0; }}
  .card {{ background: #131c2e; border: 1px solid #26324a; border-radius: 16px;
          padding: 2.5rem; max-width: 26rem; text-align: center; }}
  .check {{ font-size: 3rem; }}
  h1 {{ font-size: 1.25rem; margin: 0.75rem 0 0.5rem; }}
  p {{ color: #9fb0c9; margin: 0.25rem 0; line-height: 1.5; }}
  .ref {{ color: #64748b; font-size: 0.85rem; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <div class="check">✅</div>
  <h1>Payment received</h1>
  <p>Your booking is being confirmed.<br>We&#39;ll send your ticket and PNR to your WhatsApp shortly.</p>
  <p class="ref">Reference: {reference or "—"}</p>
</div>
</body>
</html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Landing website - the FareBeep home page (served from FareBeep/web/).
# Assets are static files; "/" hands out the single-page site.
# ---------------------------------------------------------------------------
WEB_DIR = Path(__file__).resolve().parent / "web"

import mimetypes

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")

app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/", include_in_schema=False)
def landing():
    return FileResponse(WEB_DIR / "index.html")
