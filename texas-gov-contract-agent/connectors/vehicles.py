"""Contract vehicles already in place — IDIQs, BPAs, and other indefinite-delivery vehicles (IDVs) —
from USAspending.gov (official, free, no key).

Task orders under an existing IDIQ usually go only to its holders and are rarely posted publicly. So this
source answers two different questions instead:

  Teaming leads    Who holds an active vehicle for your kind of work in Texas? Those companies need
                   subcontractors to perform the orders they win.
  Recompete watch  Which vehicles stop taking orders within the next ~18 months? The agency will usually
                   compete a replacement — this is how you hear about it a year early. These become
                   ⚪ Watchlist items ("forecast" notices) in the email and dashboard.

Endpoint: POST https://api.usaspending.gov/api/v2/search/spending_by_award/ with the IDV award type codes.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from core import http
from core.extract import clean, fmt_money, now, parse_date
from core.geo import match_market

from .base import BaseConnector, SourceError
from .usaspending import AWARD_PAGE, URL

log = logging.getLogger(__name__)

IDV_CODES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]
FIELDS = ["Award ID", "Recipient Name", "Start Date", "Award Amount", "Last Date to Order", "Awarding Agency",
          "Awarding Sub Agency", "Description", "Contract Award Type", "NAICS", "generated_internal_id",
          "Primary Place of Performance"]


class IdvConnector(BaseConnector):
    type = "idv"

    def __init__(self, source_cfg, config):
        super().__init__(source_cfg, config)
        self.leads: list[dict] = []

    def fetch(self):
        if not self.config.get("vehicles.enabled", True) or not self.config.get("vehicles.idv_scan", True):
            return []
        codes = [str(c) for c in (self.config.get("vehicles.idv_naics") or [])]
        if not codes:
            raise SourceError("SETUP: list NAICS codes under vehicles.idv_naics in config/settings.yaml.")
        today = now()
        window = today + timedelta(days=30.4 * float(self.config.get("vehicles.recompete_window_months", 18)))
        body = {
            "filters": {
                "award_type_codes": IDV_CODES,
                "time_period": [{"start_date": (today - timedelta(days=365 * 7)).date().isoformat(),
                                 "end_date": today.date().isoformat()}],
                "naics_codes": codes,
                "place_of_performance_locations": [{"country": "USA", "state": "TX"}],
            },
            "fields": FIELDS, "sort": "Award Amount", "order": "desc", "limit": 100, "page": 1,
        }
        try:
            resp = http.post(URL, json=body, timeout=60)
        except Exception as exc:
            raise SourceError(f"Could not reach USAspending ({exc.__class__.__name__}).") from exc
        if resp.status_code != 200:
            raise SourceError(f"USAspending returned HTTP {resp.status_code}.")
        rows = (resp.json() or {}).get("results") or []
        self.leads, forecasts = [], []
        for r in rows:
            last_order = parse_date(r.get("Last Date to Order"))
            if last_order and last_order < today:
                continue                                      # no longer taking orders
            lead = _lead(r, last_order)
            self.leads.append(lead)
            if last_order and last_order <= window:
                forecasts.append(self._forecast(r, lead, last_order))
        forecasts.sort(key=lambda o: o.raw["vehicle_lead"]["last_order_date"] or "")
        return forecasts[: int(self.config.get("vehicles.max_recompete_items", 8))]

    def _forecast(self, r: dict, lead: dict, last_order):
        desc = lead["description"] or "Contract vehicle"
        if desc.isupper():
            desc = desc.capitalize()
        city = lead.get("city", "")
        market = match_market(self.config, city=city) if city else ""
        title = f"Recompete expected: {desc[:90]}"
        summary = (f"{lead['holder']} holds this {lead['vehicle'].lower()} ({lead['award_id']}); it stops taking orders "
                   f"on {last_order:%b %d, %Y}. {fmt_money(lead['amount'])} has been obligated through it so far.")
        return self.make_opp(
            source_id=f"IDV-{lead['award_id']}",
            title=title,
            url=lead["url"],
            solicitation_number=lead["award_id"],
            agency=lead["agency"],
            office=lead.get("sub_agency", ""),
            city=city,
            market=market or "unknown",
            naics=lead.get("naics", ""),
            description=(f"Existing contract vehicle: {desc}. {summary} Agencies usually compete a replacement before "
                         "an ordering period ends — watch SAM.gov for a sources sought or pre-solicitation notice and "
                         "introduce yourself to the contracting office and the incumbent now."),
            notice_type="forecast",
            procurement_type="Existing IDIQ/BPA (recompete forecast)",
            jurisdiction="federal",
            place_of_performance=f"{city}, TX" if city else "Texas",
            raw={"vehicle_lead": lead, "pop_missing": not city,
                 "award_history_seed": {
                     "summary": "Last time: " + summary, "previous_winner": lead["holder"],
                     "previous_award": lead["amount"], "contract_period": f"{lead['start'] or '?'} to {last_order:%Y-%m-%d}",
                     "exact": [{"award_id": lead["award_id"], "recipient": lead["holder"], "amount": lead["amount"],
                                "start": lead["start"], "end": f"{last_order:%Y-%m-%d}", "agency": lead["agency"],
                                "sub_agency": lead.get("sub_agency", ""), "url": lead["url"], "source": "USAspending.gov"}],
                     "similar": [], "sources": ["USAspending.gov (official federal award data)"]}},
        )


def _lead(r: dict, last_order) -> dict:
    internal = r.get("generated_internal_id")
    pop = r.get("Primary Place of Performance")
    city = ""
    if isinstance(pop, dict):
        city = (pop.get("city_name") or pop.get("city") or "").title()
    elif isinstance(pop, str) and "," in pop:
        city = pop.split(",")[0].strip().title()
    naics = r.get("NAICS")
    if isinstance(naics, dict):
        naics = naics.get("code") or ""
    return {
        "holder": clean(r.get("Recipient Name") or "").title(),
        "award_id": r.get("Award ID") or "",
        "vehicle": clean(r.get("Contract Award Type") or "IDV"),
        "description": clean(r.get("Description") or "")[:200],
        "agency": clean(r.get("Awarding Agency") or ""),
        "sub_agency": clean(r.get("Awarding Sub Agency") or ""),
        "amount": r.get("Award Amount"),
        "start": r.get("Start Date") or "",
        "last_order_date": f"{last_order:%Y-%m-%d}" if last_order else "",
        "naics": str(naics or ""),
        "city": city,
        "url": AWARD_PAGE.format(internal) if internal else "https://www.usaspending.gov/search",
        "source": "USAspending.gov",
    }


def build(config):
    entry = next((e for e in (config.sources.get("federal") or []) if e.get("type") == "idv"), None)
    return [IdvConnector(entry, config)] if entry and entry.get("enabled", True) else []
