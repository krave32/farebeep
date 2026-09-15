"""Read-only: look for a specific phone (the bot's own number) and
list every user in the DB with their subscriptions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from FareBeep.database import SessionLocal  # noqa: E402
from FareBeep.models import User, Subscription  # noqa: E402

s = SessionLocal()
try:
    print("=== all users ===")
    for u in s.query(User).all():
        subs = s.query(Subscription).filter(Subscription.user_id == u.user_id).all()
        print(f"  {u.phone} -> " + ("; ".join(
            f"{x.origin}->{x.destination} target={x.target_price} "
            f"date={x.target_date} paused={x.paused}" for x in subs)
            or "(no subs)"))
    total = s.query(Subscription).count()
    print("total subscriptions:", total)
finally:
    s.close()
