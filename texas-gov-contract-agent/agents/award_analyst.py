"""AGENT 3 — Prior Winner / Contract History.

"Here is what happened last time."
  Federal: USAspending.gov (official award data) — the exact prior contract when the solicitation names
           it, plus similar Texas awards by NAICS and place of performance.
  Local:   the portal's past/awarded opportunities when it publishes them (Bonfire), otherwise a direct
           link to the agency's official bid-tabulation / award page for a quick manual check.
"""
from __future__ import annotations

import re
import statistics

from connectors.usaspending import search_awards
from core.config import Config
from core.extract import fmt_money
from core.opportunity import Opportunity

PIID = re.compile(r"\b(?:[A-Z0-9]{6}-?\d{2}-?[A-Z]-?\d{4}|\d{2}[A-Z]\d{3}\d{2}[A-Z]\d{4})\b")
INCUMBENT = re.compile(r"incumbent(?: contractor)?(?: is|:)?\s+([A-Z][A-Za-z0-9&,.'\- ]{3,60}?)(?:[.,;(]|\s+(?:under|with|since|has))")


def usaspending_agency(sam_name: str) -> str:
    """'VETERANS AFFAIRS, DEPARTMENT OF' → 'Department of Veterans Affairs'."""
    name = (sam_name or "").strip()
    m = re.match(r"(.+),\s*department of$", name, re.I)
    if m:
        return "Department of " + m.group(1).title().replace(" And ", " and ")
    return name.title() if name.isupper() else name


class AwardAnalyst:
    def __init__(self, config: Config, scout=None, offline: bool = False):
        self.config = config
        self.scout = scout
        self.offline = offline

    def run(self, opp: Opportunity) -> dict:
        seed = (opp.raw or {}).get("award_history_seed")
        if seed:                                      # demo / test data
            return seed
        if self.offline:
            return {"summary": "Award history lookup skipped (offline run).", "awards": [], "sources": []}
        if opp.jurisdiction == "federal":
            return self._federal(opp)
        return self._local(opp)

    def _federal(self, opp: Opportunity) -> dict:
        text = opp.text()
        piids = sorted(set(PIID.findall(text)) - {opp.solicitation_number})[:3]
        incumbent = INCUMBENT.search(text)
        exact = search_awards(keywords=piids, state="", years=10, limit=5) if piids else []
        agency = usaspending_agency(opp.agency)
        similar = []
        if opp.naics:
            similar = search_awards(naics=opp.naics, state="TX", city=opp.city, agency=agency, years=5, limit=10)
        out = {"exact": exact, "similar": similar, "searched": {"prior_contract_numbers": piids, "naics": opp.naics,
                                                                "city": opp.city, "agency": agency},
               "sources": ["USAspending.gov (official federal award data)"]}
        if incumbent:
            out["incumbent_named_in_text"] = incumbent.group(1).strip()
        return self._summarize(out)

    def _local(self, opp: Opportunity) -> dict:
        past = self.scout.past_awards(opp) if self.scout else []
        src = self.config.source(opp.source)
        awards_url = src.get("awards_url") or src.get("info_url") or ""
        out = {"exact": [], "similar": [], "local_matches": past, "awards_url": awards_url,
               "sources": [src.get("name", opp.source)]}
        if past:
            first = past[0]
            out["summary"] = (f"A similar past solicitation was found: “{first['title']}” "
                              f"(closed {first.get('closed') or 'date not listed'}). Open it to see the award/tabulation.")
        else:
            out["summary"] = ("No machine-readable award history for this agency. Check the official bid tabulations: "
                              f"{awards_url}" if awards_url else "No award history source configured for this agency.")
        out["previous_winner"] = ""
        return out

    def _summarize(self, out: dict) -> dict:
        exact, similar = out.get("exact") or [], out.get("similar") or []
        amounts = [a["amount"] for a in similar if isinstance(a.get("amount"), (int, float)) and a["amount"] > 0]
        winners = [a["recipient"] for a in similar if a.get("recipient")]
        out["number_of_similar_awards"] = len(similar)
        out["median_similar_award"] = statistics.median(amounts) if amounts else None
        out["distinct_winners"] = sorted(set(winners))[:8]
        if exact:
            e = exact[0]
            out["previous_winner"] = e.get("recipient") or ""
            out["previous_award"] = e.get("amount")
            out["contract_period"] = f"{e.get('start') or '?'} to {e.get('end') or '?'}"
            out["summary"] = (f"Last time: {e.get('recipient')} held contract {e.get('award_id')} "
                              f"({fmt_money(e.get('amount'))}, {out['contract_period']}).")
        elif similar:
            top = similar[0]
            repeat = max((winners.count(w) for w in set(winners)), default=0)
            out["previous_winner"] = out.get("incumbent_named_in_text", "")
            out["previous_award"] = None
            out["summary"] = (f"No exact prior contract identified. {len(similar)} similar Texas awards in the last 5 years; "
                              f"largest went to {top.get('recipient')} ({fmt_money(top.get('amount'))}). "
                              f"Median {fmt_money(out['median_similar_award'])}."
                              + (" One company won several — a strong incumbent may exist." if repeat >= 3 else ""))
        else:
            out["previous_winner"] = out.get("incumbent_named_in_text", "")
            out["summary"] = "No prior awards found in USAspending for this NAICS and location."
        if out.get("incumbent_named_in_text") and not exact:
            out["summary"] += f" The solicitation names the incumbent as {out['incumbent_named_in_text']}."
        return out
