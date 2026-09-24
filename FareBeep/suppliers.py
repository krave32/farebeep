"""Inventory suppliers - one interface, pluggable backends.

Every supplier speaks the SAME normalized contract so the brain, the
warmer, and the booking path never learn vendor differences:

    login() -> str
    search_offers(origin, destination, flight_date, adults=1,
                  children=0, infants=0, cabin="economy",
                  currency="NGN") -> list[normalized offer]
    verify_price(booking_token, adults=1, children=0, infants=0,
                 cabin="economy", currency="NGN") -> dict
    reserve(booking_token, travellers, adults=1, children=0,
            infants=0, ticket_time_limit_hours=48) -> dict
    close() -> None

Normalized offer (identical across suppliers):
    {flight_no, airline, airline_name, departure_code, arrival_code,
     departure_time, arrival_time, duration, baggage, cabin, price,
     currency, booking_token}

Selection is a one-line config flip:
    INVENTORY_PROVIDER=travels247   (default)
    INVENTORY_PROVIDER=quickair

The booking path (verify_price -> reserve) is UNCONDITIONAL ticketing:
production suppliers must only be reserved after payment confirms.
"""
import logging

logger = logging.getLogger("farebeep.suppliers")


def get_inventory_client():
    """Build the configured supplier client. Never raises for unknown
    providers - falls back to travels247 with a warning (a missing
    supplier must not take the bot down)."""
    from FareBeep.config import INVENTORY_PROVIDER
    provider = (INVENTORY_PROVIDER or "travels247").strip().lower()
    if provider == "quickair":
        from FareBeep.quickair import QuickAirClient
        return QuickAirClient()
    if provider != "travels247":
        logger.warning("Unknown INVENTORY_PROVIDER %r - using travels247",
                       provider)
    from FareBeep.travels247 import Travels247Client
    return Travels247Client()


def pick_cheapest(offers: list):
    """Cheapest offer that carries a booking_token (without one it can be
    quoted but never priced or reserved). Supplier-agnostic by contract."""
    usable = [o for o in (offers or []) if o.get("booking_token")]
    if not usable:
        return None
    return min(usable, key=lambda o: o["price"])


__all__ = ["get_inventory_client", "pick_cheapest"]
