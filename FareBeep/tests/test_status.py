"""STATUS MONITOR - 3-hour watch window + proactive template push."""
import uuid
from datetime import timedelta

import pytest

from FareBeep.models import StatusWatch, User, utcnow
from FareBeep.status import StatusService


def _id():
    return uuid.uuid4()


class FakeAviation:
    def __init__(self, results):
        self.results = results          # list of status strings per call
        self.calls = 0

    def flight_status(self, flight_iata, flight_date):
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


class FakeNotifier:
    def __init__(self):
        self.templates = []

    def send_template(self, to, name, body_parameters=None):
        self.templates.append((to, name, body_parameters))
        return True


def make_user(db):
    u = User(phone="+2348012345678")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_service(db, api_results, clock, notifier=None):
    api = FakeAviation(api_results)
    svc = StatusService(
        db, api=api, notifier=notifier or FakeNotifier(),
        watch_lead_hours=3, template_name="flight_status",
        clock=clock)
    return svc, api


def test_watch_window_opens_3h_before_departure(db):
    svc = StatusService(db, api=FakeAviation(["scheduled"]),
                        watch_lead_hours=3, clock=lambda: utcnow())
    dep = utcnow() + timedelta(hours=8)
    watch = svc.create_watch(_id(), None, "P47123", "2026-08-20", dep)
    assert watch.watch_starts_at == dep - timedelta(hours=3)


def test_not_tracked_until_window_opens(db):
    """Hour 5 before departure: the watch is not yet due -> no API spend."""
    dep = utcnow() + timedelta(hours=5)
    svc, api = make_service(db, ["scheduled"], lambda: utcnow())
    watch = svc.create_watch(_id(), _id(), "P47123", "2026-08-20", dep)

    n = svc.run_watch_cycle()
    assert n == 0
    assert api.calls == 0
    assert watch.initiated is False


def test_delayed_change_sends_proactive_template(db):
    """At T-2h the aviationstack status changes to delayed -> template pushed."""
    user = make_user(db)
    dep = utcnow() + timedelta(hours=2)
    notifier = FakeNotifier()
    svc, api = make_service(db, ["delayed"], lambda: utcnow(), notifier=notifier)
    watch = svc.create_watch(_id(), user.user_id, "P47123",
                             "2026-08-20", dep)

    # move the clock into the window (T-2h30m, window opened at T-3h)
    svc.clock = lambda: dep - timedelta(hours=2, minutes=30)
    n = svc.run_watch_cycle()

    assert n == 1
    assert notifier.templates == [
        ("+2348012345678", "flight_status",
         ["P47123", "DELAYED"])
    ]


def test_no_template_when_status_unchanged(db):
    user = make_user(db)
    dep = utcnow() + timedelta(hours=2)
    notifier = FakeNotifier()
    svc, api = make_service(db, ["scheduled", "scheduled"],
                            lambda: utcnow(), notifier=notifier)
    svc.create_watch(_id(), user.user_id, "P47123", "2026-08-20", dep)

    svc.clock = lambda: dep - timedelta(hours=2, minutes=30)
    svc.run_watch_cycle()
    svc.run_watch_cycle()

    assert notifier.templates == []


def test_already_departed_watch_is_skipped(db):
    """scheduled_departure < now -> out of scope, never polled."""
    user = make_user(db)
    dep = utcnow() + timedelta(hours=1)
    svc, api = make_service(db, ["landed"], FakeNotifier())
    svc.create_watch(_id(), user.user_id, "P47123", "2026-08-20", dep)

    svc.clock = lambda: dep + timedelta(minutes=30)   # after departure
    n = svc.run_watch_cycle()
    assert n == 0
    assert api.calls == 0
