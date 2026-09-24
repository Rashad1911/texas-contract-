"""Builds connector objects from config/sources.yaml."""
from __future__ import annotations

from core.config import Config

from .base import BaseConnector
from .bonfire import BonfireConnector
from .generic import BeaconConnector, HtmlListConnector, JsonApiConnector, ManualConnector, RssConnector

TYPES: dict[str, type[BaseConnector]] = {
    "bonfire": BonfireConnector,
    "html": HtmlListConnector,
    "rss": RssConnector,
    "json_api": JsonApiConnector,
    "beacon": BeaconConnector,
    "manual": ManualConnector,
}


def build_source(entry: dict, config: Config) -> BaseConnector | None:
    if entry.get("type") == "sam":
        from .sam import SamConnector
        return SamConnector(entry, config)
    cls = TYPES.get(entry.get("type", ""))
    return cls(entry, config) if cls else None


def build_market(config: Config, market: str) -> list[BaseConnector]:
    """All enabled local sources for one market key (dfw, houston, austin, san_antonio, statewide)."""
    if market not in ("statewide",) and market not in config.markets:
        return []   # market switched off in locations.yaml
    out = []
    for entry in config.sources.get("local") or []:
        if entry.get("enabled", True) and entry.get("market") == market:
            conn = build_source(entry, config)
            if conn:
                out.append(conn)
    return out
