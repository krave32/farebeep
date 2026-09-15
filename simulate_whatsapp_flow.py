"""simulate_whatsapp_flow.py - controlled WhatsApp flow rehearsals.

Two rehearsal scripts:

1. Core journey (default): hello -> search -> alert. NO booking and NO
   payment steps by construction: the scripted turns never contain
   book/pay language (hard-aborted if they do), and the dry run asserts
   zero booking_sessions afterwards.

2. Booking audit (--with-booking, dry-run only): the core journey PLUS
   BOOK -> 10-minute hold -> Paystack charge.success webhook ->
   settlement -> confirmation. All money and supplier APIs are stubbed
   (Paystack init, signature verification); the run asserts the full
   book -> pay -> confirm spine end-to-end offline.

Modes:
  --dry-run (default) - fully offline: in-process TestClient, throwaway
      SQLite file, stubbed notifier + fare search. No network, no costs,
      no customer contact. This is what CI / this machine runs.
  --live --to <number> --i-am-sure - posts HMAC-signed Meta payloads to a
      RUNNING server (--base-url, default http://127.0.0.1:8000). The
      number MUST be in the allowlist (--allowlist file or TEST_ALLOWLIST
      env, comma-separated). Anything else aborts before sending. Live
      mode never sends booking turns.

Usage:
    python simulate_whatsapp_flow.py
    python simulate_whatsapp_flow.py --with-booking
    python simulate_whatsapp_flow.py --live --to +2348010000009 --i-am-sure
    TEST_ALLOWLIST=+2348010000009 python simulate_whatsapp_flow.py --live \\
        --to +2348010000009 --i-am-sure --base-url https://<staging>/...
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import tempfile

TEST_NUMBER = "+2348010000009"  # dry-run identity (never a real customer)

TURNS = [
    ("hello", "hi"),
    ("search", "Lagos to Abuja tomorrow"),
    ("alert", "TRACK"),
]

# Payment-adjacent language: the core script must never send these.
FORBIDDEN = ("book", "pay", "card", "pin", "otp", "transfer", "account")


def _check_no_payments():
    for name, text in TURNS:
        low = text.lower()
        if any(w in low for w in FORBIDDEN):
            raise SystemExit(
                f"REFUSED: turn {name!r} contains payment-adjacent language")


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body,
                                hashlib.sha256).hexdigest()


def _payload(phone: str, mid: str, text: str, ts: int) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [
        {"id": mid, "from": phone, "timestamp": str(ts),
         "type": "text", "text": {"body": text}}]}}]}]}


def dry_run(with_booking: bool = False) -> int:
    _check_no_payments()
    from unittest import mock

    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from FareBeep import main, transactions
    from FareBeep.models import Base, BookingSession, Subscription, User

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    sent = []
    paystack_refs = []

    class _FakeSearch:
        def __init__(self, db):
            pass

        def _fare(self, origin, destination, date_):
            return {"source": "stub", "flight_date": date_,
                    "price": 85000.0, "airline": "Rano Air",
                    "flight_number": "RN 303", "departs_at": "06:00",
                    "verify_link": "https://example.com/sim",
                    "above_guardrail": False}

        def search_list(self, origin, destination, date_, limit=3):
            return [self._fare(origin, destination, date_)], []

        def search(self, origin, destination, date_, force_refresh=False,
                   **kw):
            return self._fare(origin, destination, date_)

    def _fake_paystack_init(reference, total, email):
        # captures the real reference the settlement engine minted so the
        # simulated charge.success targets the actual booking session
        paystack_refs.append((reference, total))
        return {
            "access_code": f"SIM-{reference}",
            "authorization_url": f"https://sim.paystack.test/pay/{reference}",
        }

    with mock.patch.object(main, "META_APP_SECRET", "sim-secret"), \
         mock.patch.object(main, "SessionLocal", factory), \
         mock.patch.object(main.brain, "GEMINI_API_KEY", None), \
         mock.patch.object(main, "GROQ_API_KEY", None), \
         mock.patch.object(main, "LedgerSearch", _FakeSearch), \
         mock.patch.object(transactions, "initialize_paystack_payment",
                           _fake_paystack_init), \
         mock.patch.object(main, "verify_paystack_signature",
                           lambda raw, sig: True), \
         mock.patch.object(main, "MetaWhatsapp",
                           lambda *a, **k: type("W", (), {
                               "send_typing_indicator": staticmethod(
                                   lambda to: True)})()), \
         mock.patch.object(main, "notifier", type("N", (), {
             "send_text": staticmethod(
                 lambda to, body: sent.append((to, body)) or True)})()):
        client = TestClient(main.app)
        for i, (name, text) in enumerate(TURNS):
            body = json.dumps(
                _payload(TEST_NUMBER, f"sim-{i}", text, 100 + i)).encode()
            r = client.post(
                "/webhook/meta", content=body,
                headers={"X-Hub-Signature-256": _sign(body, "sim-secret")})
            assert r.status_code == 200, (name, r.status_code)
            print(f"[{name}] -> 200 OK")

        if with_booking:
            # the booking audit: BOOK rides on the quoted fare in
            # chat_state (route + date), then payment settles in-window
            body = json.dumps(_payload(
                TEST_NUMBER, "sim-book", "BOOK", 900)).encode()
            r = client.post(
                "/webhook/meta", content=body,
                headers={"X-Hub-Signature-256": _sign(body, "sim-secret")})
            assert r.status_code == 200
            print("[book] -> 200 OK")

            assert paystack_refs, "BOOK did not create a payment session"
            reference, total = paystack_refs[-1]
            body = json.dumps({
                "event": "charge.success",
                "data": {"reference": reference, "status": "success"},
            }).encode()
            r = client.post(
                "/webhook/paystack", content=body,
                headers={"X-Paystack-Signature": "sim"})  # stubbed verifier
            assert r.status_code == 200, r.text
            print(f"[paystack charge.success] -> 200 ({r.json().get('outcome')})")

    db = factory()
    try:
        user = db.query(User).filter_by(phone=TEST_NUMBER).first()
        subs = db.query(Subscription).filter_by(
            user_id=user.user_id).all() if user else []
        bookings = db.query(BookingSession).all()
    finally:
        db.close()
    print(f"user row: {'yes' if user else 'NO'}")
    print(f"beeps armed for test number: {len(subs)}")
    for s in subs:
        print(f"  {s.origin}->{s.destination} target={s.target_price}")
    print(f"outbound texts captured: {len(sent)}")

    assert user is not None, "hello turn did not register the user"
    assert len(subs) >= 1, "TRACK turn did not arm a beep"

    if not with_booking:
        print(f"booking_sessions: {len(bookings)} (must be 0)")
        assert len(bookings) == 0, \
            "a booking was created - script invariant broken"
        print("DRY-RUN PASS: hello -> search -> alert, no payments touched")
        return 0

    # ---- booking audit assertions ------------------------------------
    assert len(bookings) == 1, f"expected 1 booking, got {len(bookings)}"
    b = bookings[0]
    print(f"booking: {b.payment_ref} status={b.status} "
          f"total={b.total_price:,.0f} (airline {b.airline_price:,.0f})")
    assert b.status == "paid", f"booking did not settle: {b.status}"
    assert b.total_price > b.airline_price, \
        "settlement engine margin (markup+fee) missing from the total"
    assert paystack_refs[-1][0] == b.payment_ref, \
        "settle hit a different reference than the session minted"
    joined = "\n".join(body for _, body in sent)
    assert "Total held for 10 minutes" in joined, \
        "hold-honesty wording missing from the booking message"
    assert "BOOKING CONFIRMED" in joined, "no confirmation after payment"
    assert f"PNR: FB-" in joined, "confirmation missing the PNR"
    assert f"/book/{b.id}" in joined, \
        "the booking link is not the confirmation page (consent flow)"
    print("BOOKING-AUDIT PASS: BOOK -> 10m hold -> Paystack success -> "
          "settled -> confirmed, fully offline")


def live(args) -> int:
    _check_no_payments()
    allow = set()
    if args.allowlist and os.path.exists(args.allowlist):
        with open(args.allowlist) as f:
            allow |= {l.strip() for l in f if l.strip()}
    allow |= {n.strip() for n in os.getenv("TEST_ALLOWLIST", "").split(",")
              if n.strip()}
    if not args.i_am_sure:
        raise SystemExit("REFUSED: live mode needs --i-am-sure")
    if args.to not in allow:
        raise SystemExit(
            f"REFUSED: {args.to} is not in the test-number allowlist "
            f"({len(allow)} entr(y/ies) configured)")
    import httpx
    from FareBeep.config import META_APP_SECRET
    if not META_APP_SECRET:
        raise SystemExit("REFUSED: META_APP_SECRET not configured")
    print(f"LIVE rehearsal -> {args.to} via {args.base_url}")
    print("Operator: watch the test phone. Expected: greeting, fare, "
          "beep confirmation. No payment link should EVER arrive.")
    for i, (name, text) in enumerate(TURNS):
        body = json.dumps(_payload(args.to, f"live-{i}", text,
                                   1000 + i)).encode()
        r = httpx.post(args.base_url.rstrip("/") + "/webhook/meta",
                       content=body,
                       headers={"X-Hub-Signature-256": _sign(
                           body, META_APP_SECRET)},
                       timeout=30)
        print(f"[{name}] {text!r} -> HTTP {r.status_code}")
        if r.status_code != 200:
            raise SystemExit(f"FAILED at turn {name}: HTTP {r.status_code}")
    print("LIVE POSTS ACCEPTED - verify on the test phone, then confirm "
          "no booking_sessions exist for the test number.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Controlled WhatsApp flow rehearsals "
                    "(core journey + optional booking audit)")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--with-booking", action="store_true",
                    help="dry-run only: also drive BOOK -> pay -> confirm "
                         "with stubbed Paystack")
    ap.add_argument("--to", default=os.getenv("TEST_WHATSAPP_NUMBER", ""))
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--allowlist", default="")
    ap.add_argument("--i-am-sure", action="store_true")
    args = ap.parse_args()
    if args.live and args.with_booking:
        raise SystemExit("REFUSED: --with-booking is dry-run only - live "
                         "rehearsals never send booking turns")
    return live(args) if args.live else dry_run(with_booking=args.with_booking)


if __name__ == "__main__":
    sys.exit(main())
