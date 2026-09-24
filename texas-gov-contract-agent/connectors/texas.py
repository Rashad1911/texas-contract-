"""Texas statewide — Electronic State Business Daily (ESBD) and any other statewide sources.

State agencies must post solicitations over $25,000 on the ESBD. The ESBD is a JavaScript app; the
connector reads its JSON listing once you paste the listing URL into sources.yaml (README §6).
State contracts of $100,000+ with subcontracting opportunities typically require a HUB Subcontracting
Plan — the Requirements and Subcontracting agents look for that language.
"""
from core.config import Config

from .factory import build_market

MARKET = "statewide"


def build(config: Config):
    return build_market(config, MARKET)
