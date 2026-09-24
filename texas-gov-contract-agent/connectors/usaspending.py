"""USAspending.gov — official federal award history (free, no API key).

Endpoint: POST https://api.usaspending.gov/api/v2/search/spending_by_award/
Used by the Prior Winner agent to answer "what happened last time?" for federal opportunities.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from core import http
from core.extract import now

log = logging.getLogger(__name__)

URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
AWARD_PAGE = "https://www.usaspending.gov/award/{}"
FIELDS = ["Award ID", "Recipient Name", "Award Amount", "Start Date", "End Date",
          "Awarding Agency", "Awarding Sub Agency", "Description", "generated_internal_id"]


def search_awards(naics: str = "", state: str = "TX", city: str = "", keywords: list[str] | None = None,
                  agency: str = "", years: int = 5, limit: int = 10) -> list[dict]:
    """Contract awards (types A–D) matching the filters, largest first. Never raises."""
    end = now().date()
    start = end - timedelta(days=365 * years)
    filters: dict = {
        "award_type_codes": ["A", "B", "C", "D"],
        "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
    }
    if naics:
        filters["naics_codes"] = {"require": [naics]}
    if keywords:
        filters["keywords"] = [k for k in keywords if k and len(k) >= 3][:5]
    location = {"country": "USA", "state": state} if state else None
    if location and city:
        location["city"] = city.upper()
    if location:
        filters["place_of_performance_locations"] = [location]
    if agency:
        filters["agencies"] = [{"type": "awarding", "tier": "toptier", "name": agency}]

    body = {"filters": filters, "fields": FIELDS, "sort": "Award Amount", "order": "desc",
            "limit": limit, "page": 1}
    for attempt in _variants(body):
        try:
            resp = http.post(URL, json=attempt, timeout=60)
            if resp.status_code == 200:
                return [_normalize(r) for r in resp.json().get("results", [])]
            log.info("USAspending %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.info("USAspending request failed: %s", exc)
    return []


def _variants(body: dict):
    """Try the full query, then progressively looser ones if the API rejects a filter."""
    yield body
    f = dict(body["filters"])
    if "agencies" in f:
        f.pop("agencies")
        yield {**body, "filters": dict(f)}
    locs = f.get("place_of_performance_locations")
    if locs and "city" in locs[0]:
        f["place_of_performance_locations"] = [{k: v for k, v in locs[0].items() if k != "city"}]
        yield {**body, "filters": dict(f)}
    if isinstance(f.get("naics_codes"), dict):
        f["naics_codes"] = f["naics_codes"]["require"]
        yield {**body, "filters": dict(f)}


def _normalize(r: dict) -> dict:
    internal = r.get("generated_internal_id")
    return {
        "award_id": r.get("Award ID"),
        "recipient": r.get("Recipient Name"),
        "amount": r.get("Award Amount"),
        "start": r.get("Start Date"),
        "end": r.get("End Date"),
        "agency": r.get("Awarding Agency"),
        "sub_agency": r.get("Awarding Sub Agency"),
        "description": (r.get("Description") or "")[:240],
        "url": AWARD_PAGE.format(internal) if internal else "https://www.usaspending.gov/search",
        "source": "USAspending.gov",
    }
