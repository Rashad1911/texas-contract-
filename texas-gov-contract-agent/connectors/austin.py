"""Austin — City of Austin (Austin Finance Online), Travis County (BidNet — manual).

The exact portals live in config/sources.yaml so you can fix a URL without touching code.
To add another public entity in this market (a school district, transit agency, airport…), add a
block to sources.yaml with market: austin.
"""
from core.config import Config

from .factory import build_market

MARKET = "austin"


def build(config: Config):
    return build_market(config, MARKET)
