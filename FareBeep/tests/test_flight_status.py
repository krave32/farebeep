"""DELAY FEED STUB - proves the guardrails, not a transport.

The stub must fail LOUDLY until wired, so nobody mistakes silence for
"no delays" and nobody plugs inventory creds into it.
"""
import pytest

from FareBeep.flight_status import (
    FlightStatusClient, FlightStatusNotConfigured,
)


def test_unconfigured_client_is_not_configured(monkeypatch):
    monkeypatch.delenv("DELAY_API_URL", raising=False)
    monkeypatch.delenv("DELAY_API_KEY", raising=False)
    assert FlightStatusClient().configured is False


def test_unconfigured_status_raises_loudly(monkeypatch):
    monkeypatch.delenv("DELAY_API_URL", raising=False)
    monkeypatch.delenv("DELAY_API_KEY", raising=False)
    with pytest.raises(FlightStatusNotConfigured):
        FlightStatusClient().get_status("P47123", "2026-09-10")
