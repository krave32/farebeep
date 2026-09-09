"""FLIGHT STATUS / DELAYS - reserved module for the delay feed.

This is NOT the inventory API. Inventory (search / price / reserve /
PNR) lives ONLY in FareBeep/travels247.py and speaks to
247travels.com/api with TRAVELS247_EMAIL + TRAVELS247_PASSWORD.
The delay feed is a SEPARATE service with its own URL + key
(DELAY_API_URL / DELAY_API_KEY) - see .env.example. Mixing them up
is the exact error this file exists to prevent: nothing here may
import travels247, and nothing in travels247 may answer delay
questions.

Status until the feed doc arrives: status.py serves delay questions
from Aviationstack. Wire the real feed here, then point status.py
at get_status().

Interface (stable - build against this):
    FlightStatusClient().get_status(flight_no, flight_date)
        -> {flight_no, scheduled_departure, actual_departure,
            status, delay_minutes, source} | None
"""
import logging
import os

logger = logging.getLogger("farebeep.flight_status")


class FlightStatusError(Exception):
    """The delay feed call failed."""


class FlightStatusNotConfigured(FlightStatusError):
    """Raised LOUDLY (never silent) until the delay feed is wired."""


class FlightStatusClient:
    """Delay-feed client. Instantiable today, usable once configured."""

    def __init__(self, base_url: str = None, api_key: str = None):
        self.base_url = (base_url or os.getenv("DELAY_API_URL") or "").rstrip("/")
        self.api_key = api_key or os.getenv("DELAY_API_KEY")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def get_status(self, flight_no: str, flight_date: str) -> dict | None:
        """Live status for one flight. Raises FlightStatusNotConfigured
        until DELAY_API_URL + DELAY_API_KEY are set (fail loud, and the
        caller falls back to Aviationstack)."""
        if not self.configured:
            raise FlightStatusNotConfigured(
                "Delay feed not wired: set DELAY_API_URL + DELAY_API_KEY "
                "(see .env.example DELAY FEED) - this is NOT the 247travels "
                "inventory login, do not reuse TRAVELS247_* here.")
        raise NotImplementedError(
            "Delay-feed transport not implemented yet - paste the vendor "
            "doc and implement against the get_status() contract above.")


__all__ = ["FlightStatusClient", "FlightStatusError",
           "FlightStatusNotConfigured"]
