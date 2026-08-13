"""Local, authoritative city-name -> IATA-code dictionary.

The whole point of this module (per the reconstruction brief): do NOT rely on
Gemini (or any LLM) to be perfect with IATA codes ("PHC" vs "PH", "ABB" vs
"ABV"). A plain Python dict is deterministic, instant, and free.

Migrated from naijafly/app/utils/intent_parser.py (CITY_TO_IATA) - the
rule-based *parser* itself is dropped; only the categorical mapping survives.

Every city/IATA pair that reaches an external API (SerpApi, Aviationstack,
Google Flights links) must first pass through `resolve_iata()` here.
"""
from typing import Optional

# `CITY_TO_IATA`: every spelling/alias a Nigerian user may type -> IATA code.
CITY_TO_IATA: dict[str, str] = {}

_AIRPORT_MAP: list[tuple[list[str], str]] = [
    (["lagos", "los", "murtala muhammed", "lag"],                                      "LOS"),
    # "abj" is Abidjan's REAL code, but every Nigerian writes it for Abuja -
    # colloquial usage wins in this utility (Abidjan = "abidjan").
    (["abuja", "abv", "abj", "nnamdi azikiwe"],                                         "ABV"),
    (["port harcourt", "phc", "ph city", "portharcourt", "port-harcourt", "ph"],       "PHC"),
    (["enugu", "enu", "akanu ibiam"],                                                  "ENU"),
    (["benin", "benin city", "bni"],                                                   "BNI"),
    (["kano", "kan", "mallam aminu kano"],                                             "KAN"),
    (["calabar", "cbq", "margaret ekpo"],                                              "CBQ"),
    (["ilorin", "ilr"],                                                                "ILR"),
    (["owerri", "qow", "sam mbakwe", "oweri"],                                         "QOW"),
    (["asaba", "abb"],                                                                 "ABB"),
    (["uyo", "quo", "akwa ibom"],                                                      "QUO"),
    (["sokoto", "sko", "sadiq abubakar"],                                              "SKO"),
    (["yola", "yol"],                                                                  "YOL"),
    (["maiduguri", "miu"],                                                             "MIU"),
    (["akure", "akr"],                                                                 "AKR"),
    (["warri", "qrw", "oteneri"],                                                      "QRW"),
    (["jos", "jos"],                                                                   "JOS"),
    (["kaduna", "kad"],                                                                "KAD"),
    (["ibadan", "iba"],                                                                "IBA"),
    # West African / regional neighbors users also ask about
    (["accra", "acc", "kotoka"],                                                       "ACC"),
    (["abidjan", "felix houphouet"],                                                   "ABJ"),
    (["dakar", "dss", "blease diagne"],                                                "DSS"),
    (["lome", "lfw"],                                                                  "LFW"),
    (["cotonou", "coo", "cadjehoun"],                                                  "COO"),
    (["douala", "dla"],                                                                "DLA"),
    (["yaounde", "nsi", "yaounde nsimalen"],                                           "NSI"),
    (["niamey", "nim"],                                                                "NIM"),
    (["bamako", "bko", "modibo keita"],                                                "BKO"),
    (["monrovia", "rob", "roberts"],                                                   "ROB"),
    (["freetown", "fna", "lungi"],                                                     "FNA"),
    (["london", "lon", "gatwick", "heathrow"],                                         "LON"),
    (["new york", "nyc", "newark", "jfk"],                                             "NYC"),
    (["dubai", "dxb"],                                                                 "DXB"),
    (["jeddah", "jed"],                                                                "JED"),
    (["riyadh", "ruh"],                                                                "RUH"),
    (["cairo", "cai"],                                                                 "CAI"),
    (["johannesburg", "jnb", "or tambo"],                                              "JNB"),
]

for _names, _iata in _AIRPORT_MAP:
    for _name in _names:
        CITY_TO_IATA[_name.lower()] = _iata

IATA_TO_CITY: dict[str, str] = {}
for _names, _iata in _AIRPORT_MAP:
    IATA_TO_CITY.setdefault(_iata, _names[0].title())

ALL_IATA: set[str] = set(IATA_TO_CITY)


def resolve_iata(name: Optional[str]) -> Optional[str]:
    """Resolve any user-typed city name or code to an IATA code.

    - "Abuja"          -> "ABV"
    - "abv"            -> "ABV"
    - "port harcourt"  -> "PHC"
    - "PH"             -> "PHC"   (the classic Gemini mistake - handled here)
    - unknown          -> None
    """
    if not name or not isinstance(name, str):
        return None
    cleaned = name.strip()
    upper = cleaned.upper()
    if upper in ALL_IATA:
        return upper
    return CITY_TO_IATA.get(cleaned.lower())


def city_name(iata: str) -> str:
    """Reverse lookup: "ABV" -> "Abuja" (for user-facing messages)."""
    return IATA_TO_CITY.get(iata.upper(), iata.upper())


def normalize(origin: Optional[str], destination: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Normalize an (origin, destination) pair; returns (None, None) if bad."""
    return resolve_iata(origin), resolve_iata(destination)
