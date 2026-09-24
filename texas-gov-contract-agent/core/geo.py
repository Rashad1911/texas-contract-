"""Match an opportunity to one of the target markets (DFW, Houston, Austin, San Antonio)."""
from __future__ import annotations

import re

from .config import Config


def match_market(config: Config, city: str = "", county: str = "", zip_code: str = "",
                 text: str = "") -> str:
    """Return a market key, 'statewide', or '' when nothing matches."""
    city_l = (city or "").strip().lower()
    county_l = (county or "").lower().replace(" county", "").strip()
    zip3 = re.sub(r"\D", "", zip_code or "")[:3]
    blob = (text or "").lower()

    for key, market in config.markets.items():
        if city_l and city_l in [c.lower() for c in market.get("cities", [])]:
            return key
        if county_l and county_l in [c.lower() for c in market.get("counties", [])]:
            return key
        if zip3 and zip3 in market.get("zip_prefixes", []):
            return key

    if blob:
        for key, market in config.markets.items():
            for name in market.get("installations", []):
                if name.lower() in blob:
                    return key
        for key, market in config.markets.items():
            for c in market.get("counties", []):
                if re.search(rf"\b{re.escape(c.lower())} county\b", blob):
                    return key
            for c in market.get("cities", []):
                if re.search(rf"\b{re.escape(c.lower())},?\s+(?:tx|texas)\b", blob):
                    return key
        for term in config.locations.get("statewide_terms", []):
            if term in blob:
                return "statewide"
    return ""
