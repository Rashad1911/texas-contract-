"""AGENT 1 — Opportunity Scout.

Searches every enabled portal in config/sources.yaml, normalizes results, and records whether each
source worked. A broken portal never stops the run; it shows up in Source health instead.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from connectors import all_connectors
from connectors.base import SourceError, SourceHealth
from core.config import Config

log = logging.getLogger(__name__)


class OpportunityScout:
    def __init__(self, config: Config, connectors=None):
        self.config = config
        self.connectors = connectors if connectors is not None else all_connectors(config)
        self.by_key = {c.key: c for c in self.connectors}

    def run(self):
        """Returns (opportunities, health_list)."""
        results, health = [], []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._fetch_one, c): c for c in self.connectors}
            for fut, conn in futures.items():
                opps, h = fut.result()
                results.extend(opps)
                health.append(h)
        # de-duplicate the same solicitation seen twice (e.g. posted on two portals)
        seen, unique = set(), []
        for opp in results:
            key = (opp.solicitation_number or "").strip().upper() or opp.uid
            if key in seen and opp.solicitation_number:
                continue
            seen.add(key)
            unique.append(opp)
        health.sort(key=lambda h: (h.market, h.name))
        return unique, health

    def _fetch_one(self, conn):
        h = SourceHealth(key=conn.key, name=conn.name, market=conn.market, status="ok",
                         url=conn.url, info_url=conn.info_url)
        try:
            opps = conn.fetch()
            h.count = len(opps)
            if not opps:
                h.status = "empty"
                h.message = "Reached the portal but found no open items (or the page layout changed)."
            return opps, h
        except SourceError as exc:
            msg = str(exc)
            h.status = "manual" if msg.startswith("MANUAL") else "setup" if msg.startswith("SETUP") else "error"
            h.message = msg.split(": ", 1)[-1] if msg.startswith(("MANUAL", "SETUP")) else msg
        except Exception as exc:   # never let one portal break the run
            log.exception("Source %s crashed", conn.key)
            h.status = "error"
            h.message = f"Unexpected error: {exc.__class__.__name__}: {exc}"[:240]
        return [], h

    def enrich(self, opp) -> None:
        conn = self.by_key.get(opp.source)
        if conn:
            try:
                conn.enrich(opp)
            except Exception as exc:
                log.info("Enrich failed for %s: %s", opp.short_id, exc)

    def past_awards(self, opp) -> list[dict]:
        conn = self.by_key.get(opp.source)
        if not conn:
            return []
        try:
            return conn.past_awards(opp)
        except Exception:
            return []
