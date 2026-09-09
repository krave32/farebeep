"""Per-chat conversational memory, DB-backed (the `chat_state` table).

Why not in-memory dicts: the process restarts on every deploy (and Railway
may run several replicas), so RAM context silently dies mid-conversation.
Every read/write here goes through the Shared Ledger Postgres, so a chat's
thread survives restarts and works from any instance.

One row per phone with three JSON slots:
  last_fare     - the last single-fare quote (bare BOOK/TRACK reuse it)
  last_fares    - the last ranked list (bare "1, 2 or 3" picks use it)
  pending_fare  - a partial fare we asked a follow-up about (a bare city
                  answer completes it)
  agent_history - the Groq agent's rolling [{role, content}] memory

Writes COMMIT immediately: each message turn runs in its own short-lived
session, so a later turn (possibly on another replica) must see them.
"""
import logging
from typing import Optional

from FareBeep.models import ChatState, utcnow

logger = logging.getLogger("farebeep.chatstate")


def _row(db, phone: str) -> ChatState:
    """Fetch the phone's row, creating it on first write (reads never
    create rows - a chat that only says "hi" leaves no state)."""
    row = db.query(ChatState).filter(ChatState.phone == phone).first()
    if row is None:
        row = ChatState(phone=phone)
        db.add(row)
        db.flush()
    return row


def _read(db, phone: str, field: str):
    row = db.query(ChatState).filter(ChatState.phone == phone).first()
    return getattr(row, field) if row is not None else None


def _write(db, phone: str, field: str, value) -> None:
    row = _row(db, phone)
    setattr(row, field, value)
    row.updated_at = utcnow()
    db.commit()


def get_last_fare(db, phone: str) -> Optional[dict]:
    return _read(db, phone, "last_fare")


def set_last_fare(db, phone: str, ctx: dict) -> None:
    _write(db, phone, "last_fare", ctx)


def clear_last_fare(db, phone: str) -> None:
    _write(db, phone, "last_fare", None)


def get_last_fares(db, phone: str) -> Optional[dict]:
    return _read(db, phone, "last_fares")


def set_last_fares(db, phone: str, ctx: dict) -> None:
    _write(db, phone, "last_fares", ctx)


def clear_last_fares(db, phone: str) -> None:
    _write(db, phone, "last_fares", None)


def get_pending_fare(db, phone: str) -> Optional[dict]:
    return _read(db, phone, "pending_fare")


def set_pending_fare(db, phone: str, ctx: dict) -> None:
    _write(db, phone, "pending_fare", ctx)


def clear_pending_fare(db, phone: str) -> None:
    _write(db, phone, "pending_fare", None)


def get_pending_requote(db, phone: str) -> Optional[dict]:
    return _read(db, phone, "pending_requote")


def set_pending_requote(db, phone: str, ctx: dict) -> None:
    _write(db, phone, "pending_requote", ctx)


def clear_pending_requote(db, phone: str) -> None:
    _write(db, phone, "pending_requote", None)


MAX_AGENT_HISTORY = 12   # rolling window: the last ~6 turns


def get_agent_history(db, phone: str) -> list:
    """The Groq agent's rolling memory: [{role, content}...] (may be empty)."""
    return _read(db, phone, "agent_history") or []


def append_agent_history(db, phone: str, user_text: str,
                         assistant_text: str) -> None:
    """Record one turn, keeping only the trailing window (replica-safe:
    a single read-modify-write per turn, same as every other slot)."""
    hist = get_agent_history(db, phone)
    hist = hist + [{"role": "user", "content": user_text},
                   {"role": "assistant", "content": assistant_text}]
    _write(db, phone, "agent_history", hist[-MAX_AGENT_HISTORY:])


def clear_agent_history(db, phone: str) -> None:
    """Wipe the Groq agent's rolling memory (greeting reset: the new
    session must not resurrect the old thread conversationally). The
    User row (name, identity) is untouched - only chat memory goes."""
    _write(db, phone, "agent_history", [])