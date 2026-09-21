"""Generate FareBeep/whatsapp/flow_screens.json from the iata map.

The Set-a-Beep flow's airport dropdowns used to be hand-maintained and had
already drifted from FareBeep/iata.py (the JSON carried MDI/MXJ which the
product map doesn't resolve, and missed QUO + the regional neighbours).
iata._AIRPORT_MAP is the single authoritative airport universe - every code
that reaches an external API passes through resolve_iata() - so the screens
JSON is now a build artifact of it.

Usage:
  python build_flow_screens.py          # rewrite flow_screens.json

Dynamic bits served by the /flow endpoint (whatsapp/flows.py):
  min_date / max_date  - DatePicker bounds (INIT, BACK, data_exchange)
  fare_label           - Shared Ledger fare shown on the review screen,
                         filled by the data_exchange action on the
                         dates -> review step (cache-only, never live).
"""
import json
from pathlib import Path

from FareBeep.iata import _AIRPORT_MAP, IATA_TO_CITY

ROOT = Path(__file__).parent
OUT = ROOT / "FareBeep" / "whatsapp" / "flow_screens.json"


def airport_options() -> list[dict]:
    """Dropdown entries in product-map order (domestic first, then
    regional/intl) - deduped by code."""
    seen: set[str] = set()
    opts: list[dict] = []
    for _names, code in _AIRPORT_MAP:
        if code in seen:
            continue
        seen.add(code)
        opts.append({"id": code,
                     "title": f"{IATA_TO_CITY[code]} ({code})"})
    return opts


def build() -> dict:
    airports = airport_options()
    return {
        "version": "7.3",
        "data_api_version": "3.0",
        # Forward-only per Meta's validator: backward edges (error
        # re-routes) are NOT modelled - the endpoint answers COMPLETE
        # errors with the owning screen at runtime instead.
        "routing_model": {
            "SET_BEEP_TRIP": ["SET_BEEP_DATES"],
            "SET_BEEP_DATES": ["SET_BEEP_REVIEW"],
        },
        "screens": [
            {
                "id": "SET_BEEP_TRIP",
                "title": "Set a beep",
                "terminal": False,
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [{
                        "type": "Form",
                        "name": "trip_form",
                        "children": [
                            {"type": "TextHeading",
                             "text": "Set a price beep 🔔"},
                            {"type": "TextBody",
                             "text": "Pick your route - we'll watch fares "
                                     "and message you the moment they drop."},
                            {"type": "Dropdown", "name": "origin",
                             "label": "Flying from", "required": True,
                             "data-source": airports},
                            {"type": "Dropdown", "name": "destination",
                             "label": "Flying to", "required": True,
                             "data-source": airports},
                            # Endpoint step, not a static navigate: Meta
                            # requires a payload when the next screen's
                            # data model is non-empty, and SET_BEEP_DATES
                            # needs LIVE bounds (today .. +330d) rather
                            # than baked-in dates.
                            {"type": "Footer", "label": "Continue",
                             "on-click-action": {
                                 "name": "data_exchange",
                                 "payload": {
                                     "origin": {"path": "origin"},
                                     "destination":
                                         {"path": "destination"}}}},
                        ],
                    }],
                },
            },
            {
                "id": "SET_BEEP_DATES",
                "title": "Travel date",
                "terminal": False,
                # Every dynamic (${data.*}) property must carry an
                # __example__: Business Manager renders the flow preview
                # from these, and Meta's validator rejects the upload
                # without them (MISSING_REQUIRED_PROPERTY).
                "data": {
                    "min_date": {"type": "string",
                                 "__example__": "2026-09-19"},
                    "max_date": {"type": "string",
                                 "__example__": "2027-08-15"},
                },
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [{
                        "type": "Form",
                        "name": "dates_form",
                        "children": [
                            {"type": "TextHeading",
                             "text": "When do you fly?"},
                            {"type": "TextBody",
                             "text": "We'll watch fares for this route up "
                                     "to your date."},
                            # Bounds arrive from the /flow endpoint
                            # (min_date = today, max_date = +330d) so past
                            # dates can't even be picked. Meta's DatePicker
                            # takes min-date/max-date with ${data.*} binding.
                            {"type": "DatePicker", "name": "departure_date",
                             "label": "Departure date", "required": True,
                             "min-date": "${data.min_date}",
                             "max-date": "${data.max_date}"},
                            {"type": "Footer", "label": "Continue",
                             "on-click-action": {
                                 # Endpoint step: reads the Shared Ledger
                                 # (cache-only) and answers the review
                                 # screen with the current fare label.
                                 "name": "data_exchange",
                                 "payload": {
                                     "origin": {"path": "origin"},
                                     "destination": {"path": "destination"},
                                     "departure_date":
                                         {"path": "departure_date"}}}},
                        ],
                    }],
                },
            },
            {
                "id": "SET_BEEP_REVIEW",
                "title": "Alert settings",
                "terminal": True,
                "success": True,
                "data": {
                    "fare_label": {"type": "string",
                                   "__example__": "Right now: Air Peace "
                                                  "\u20a698,000 in the "
                                                  "Shared Ledger for "
                                                  "this date."},
                },
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [{
                        "type": "Form",
                        "name": "review_form",
                        "children": [
                            {"type": "TextHeading", "text": "Almost done"},
                            # Filled by the data_exchange step; empty when
                            # the ledger has nothing for this route/date.
                            {"type": "TextBody",
                             "text": "${data.fare_label}"},
                            {"type": "TextBody",
                             "text": "Optionally set the price we should "
                                     "watch for - or leave it empty to "
                                     "alert on ANY drop. Fares are "
                                     "re-checked throughout the day."},
                            {"type": "TextInput", "name": "target_price",
                             "label": "Alert me below this price (₦) - "
                                      "optional",
                             "input-type": "number", "required": False,
                             "helper-text": "Leave empty to alert on ANY "
                                            "price drop."},
                            {"type": "Footer", "label": "Set my beep",
                             "on-click-action": {"name": "complete"}},
                        ],
                    }],
                },
            },
        ],
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({len(airport_options())} airports)")
