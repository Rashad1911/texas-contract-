"""Base class for every procurement source."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from core.config import Config
from core.geo import match_market
from core.opportunity import Opportunity

log = logging.getLogger(__name__)


class SourceError(Exception):
    """A source could not be read. The message is shown to you in the Source health section."""


@dataclass
class SourceHealth:
    key: str
    name: str
    market: str
    status: str            # ok | empty | error | manual | setup
    count: int = 0
    message: str = ""
    url: str = ""
    info_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class BaseConnector:
    type = "base"

    def __init__(self, source_cfg: dict, config: Config):
        self.cfg = source_cfg
        self.config = config
        self.key = source_cfg.get("key", self.type)
        self.name = source_cfg.get("name", self.key)
        self.market = source_cfg.get("market", "")
        self.jurisdiction = source_cfg.get("jurisdiction", "other")
        self.url = source_cfg.get("url", "")
        self.info_url = source_cfg.get("info_url", "")

    # Every connector returns a list of Opportunity objects or raises SourceError.
    def fetch(self) -> list[Opportunity]:
        raise NotImplementedError

    # Optional: pull full description / attachments for a shortlisted opportunity.
    def enrich(self, opp: Opportunity) -> None:
        return None

    # Optional: prior awards for the Prior Winner agent. Returns list of dicts.
    def past_awards(self, opp: Opportunity) -> list[dict]:
        return []

    # ── helpers ───────────────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        parsed = urlparse(self.url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def make_opp(self, source_id: str, title: str, **fields) -> Opportunity:
        opp = Opportunity(source=self.key, source_id=str(source_id or _hash(title)), title=title, **fields)
        opp.jurisdiction = opp.jurisdiction or self.jurisdiction
        if not opp.agency:
            opp.agency = self.name.split("—")[0].strip()
        if not opp.market:
            if self.market and self.market not in ("all",):
                opp.market = self.market
            else:
                opp.market = match_market(self.config, opp.city, opp.county, opp.zip, opp.text(False))
        return opp


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").encode()).hexdigest()[:12]
