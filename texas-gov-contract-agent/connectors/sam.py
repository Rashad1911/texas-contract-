"""SAM.gov — federal contract opportunities (VA, DoD/Army/Air Force/Navy, GSA, DHS, USPS, and all others).

Official API: https://open.gsa.gov/api/get-opportunities-public-api/
  GET https://api.sam.gov/opportunities/v2/search?api_key=…&postedFrom=MM/dd/yyyy&postedTo=…&limit=1000

Daily limits are per API key: roleless non-federal keys get 10 requests/day; a key belonging to a user
with a role on a registered SAM.gov entity gets 1,000/day. This connector counts every request and never
exceeds the budget in settings.yaml → sam.daily_request_limit (minus sam.reserve_requests).
Keys expire periodically — if SAM starts returning 401/403, generate a new key (README §2).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from core import http
from core.config import env
from core.extract import clean, html_to_text, now, parse_date

from .base import BaseConnector, SourceError
from .documents import extract_attachments

log = logging.getLogger(__name__)

SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
TEXAS_HINT = re.compile(r"\btexas\b|\bTX\b|fort worth district|galveston district|\bJBSA\b|fort sam|lackland|"
                        r"randolph|fort hood|fort cavazos|fort bliss|dyess|sheppard|goodfellow|laughlin|corpus christi",
                        re.I)

NOTICE_TYPES = {
    "solicitation": "solicitation",
    "combined synopsis/solicitation": "solicitation",
    "presolicitation": "presolicitation",
    "sources sought": "sources_sought",
    "special notice": "special",
    "award notice": "award",
    "justification": "award",
    "sale of surplus property": "award",
    "intent to bundle requirements (dod-funded)": "special",
}

# SAM.gov set-aside codes → plain names (and whether an ordinary small business qualifies)
SET_ASIDES = {
    "SBA": ("Total Small Business Set-Aside", "small"),
    "SBP": ("Partial Small Business Set-Aside", "small"),
    "8A": ("8(a) Set-Aside", "8A"),
    "8AN": ("8(a) Sole Source", "8A"),
    "HZC": ("HUBZone Set-Aside", "HUBZONE"),
    "HZS": ("HUBZone Sole Source", "HUBZONE"),
    "SDVOSBC": ("Service-Disabled Veteran-Owned Small Business Set-Aside", "SDVOSB"),
    "SDVOSBS": ("SDVOSB Sole Source", "SDVOSB"),
    "WOSB": ("Women-Owned Small Business Set-Aside", "WOSB"),
    "WOSBSS": ("WOSB Sole Source", "WOSB"),
    "EDWOSB": ("Economically Disadvantaged WOSB Set-Aside", "EDWOSB"),
    "EDWOSBSS": ("EDWOSB Sole Source", "EDWOSB"),
    "VSA": ("Veteran-Owned Small Business Set-Aside (VA)", "VOSB"),
    "VSS": ("Veteran-Owned Small Business Sole Source (VA)", "VOSB"),
    "IEE": ("Indian Economic Enterprise Set-Aside", "IEE"),
    "ISBEE": ("Indian Small Business Economic Enterprise Set-Aside", "ISBEE"),
}


class SamBudget:
    def __init__(self, limit: int):
        self.limit = max(0, limit)
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        return self.limit - self.used


class SamConnector(BaseConnector):
    type = "sam"

    def __init__(self, source_cfg, config):
        super().__init__(source_cfg, config)
        daily = int(config.get("sam.daily_request_limit", 10))
        reserve = int(config.get("sam.reserve_requests", 2))
        self.budget = SamBudget(daily - reserve)
        mode = config.get("sam.query_mode", "auto")
        self.mode = ("all" if daily >= 100 else "state") if mode == "auto" else mode
        self.session = http.session(retry_statuses=())   # never auto-retry a metered call
        self.api_key = env("SAM_API_KEY")
        self.leads: list[dict] = []

    def fetch(self):
        if not self.api_key:
            raise SourceError("SAM_API_KEY is not set — add it as a GitHub secret (README §2–3).")
        lookback = int(self.cfg.get("lookback_days", 10))
        end = now()
        start = end - timedelta(days=lookback)
        ptypes = self.config.get("sam.notice_types", "o,k,p,r")
        track_awards = self.config.get("vehicles.enabled", True) and self.config.get("vehicles.track_sam_awards", True)
        if track_awards and "a" not in ptypes.split(","):
            ptypes += ",a"                      # award notices ride along in the same request (no extra cost)
        base = {"api_key": self.api_key, "postedFrom": start.strftime("%m/%d/%Y"),
                "postedTo": end.strftime("%m/%d/%Y"), "limit": 1000, "offset": 0, "ptype": ptypes}
        self.leads = []
        out, seen = [], set()
        first = dict(base)
        if self.mode == "state":
            first["state"] = "TX"
        self._query(first, out, seen)
        # Regional IDIQs/BPAs often list several states or none. Nationwide searches for your core NAICS codes
        # catch them; _to_opp keeps only records that involve Texas. Leaves budget for reading descriptions.
        if self.mode == "state" and self.config.get("vehicles.enabled", True):
            for code in self.config.get("vehicles.sam_naics_queries", []) or []:
                if self.budget.remaining <= int(self.config.get("vehicles.keep_sam_requests_for_details", 5)):
                    break
                self._query({**base, "ncode": str(code)}, out, seen, nationwide=True)
        log.info("SAM.gov: %s kept for Texas, %s award leads, %s requests used", len(out), len(self.leads),
                 self.budget.used)
        return out

    def _query(self, params: dict, out: list, seen: set, nationwide: bool = False):
        params = dict(params)
        while True:
            if not self.budget.take():
                log.warning("SAM.gov request budget reached; stopping.")
                return
            resp = self.session.get(SEARCH_URL, params=params, timeout=90)
            if resp.status_code in (401, 403):
                raise SourceError("SAM.gov rejected the API key (expired or invalid). Generate a new one (README §2).")
            if resp.status_code == 429:
                raise SourceError("SAM.gov daily request limit reached for this key. It resets at midnight UTC.")
            if resp.status_code != 200:
                raise SourceError(f"SAM.gov returned HTTP {resp.status_code}: {resp.text[:160]}")
            data = resp.json()
            total = int(data.get("totalRecords") or 0)
            records = data.get("opportunitiesData") or []
            for rec in records:
                nid = rec.get("noticeId") or rec.get("solicitationNumber")
                if nid in seen:
                    continue
                seen.add(nid)
                if (rec.get("type") or rec.get("baseType") or "").strip().lower() == "award notice":
                    lead = self._award_lead(rec)
                    if lead:
                        self.leads.append(lead)
                    continue
                opp = self._to_opp(rec, nationwide=nationwide)
                if opp:
                    out.append(opp)
            params["offset"] += params["limit"]
            if not records or params["offset"] >= total:
                return

    def _award_lead(self, rec: dict) -> dict | None:
        """An award notice for an IDIQ/BPA in Texas → a company you could offer to subcontract for."""
        from core.vehicles import detect
        pop = rec.get("placeOfPerformance") or {}
        state = ((pop.get("state") or {}).get("code") or (rec.get("officeAddress") or {}).get("state") or "").upper()
        if state != "TX":
            return None
        award = rec.get("award") or {}
        awardee = (award.get("awardee") or {}).get("name") or ""
        title = clean(rec.get("title"))
        vehicle = detect("", title)
        naics = str(rec.get("naicsCode") or "")
        mine = {str(n) for c in self.config.service_categories().values() for n in c.get("naics", [])}
        if not awardee or not (vehicle or naics in mine):
            return None
        path = [p.strip() for p in (rec.get("fullParentPathName") or "").split(".") if p.strip()]
        return {"holder": awardee.strip(), "title": title, "vehicle": (vehicle or {}).get("label", "Contract award"),
                "agency": path[0].title() if path else "", "naics": naics,
                "amount": _float(award.get("amount")), "award_date": award.get("date") or rec.get("postedDate"),
                "contract_number": award.get("number") or "", "city": ((pop.get("city") or {}).get("name") or "").title(),
                "url": rec.get("uiLink") or "", "source": "SAM.gov award notice"}

    def _to_opp(self, rec: dict, nationwide: bool = False):
        pop = rec.get("placeOfPerformance") or {}
        pop_state = ((pop.get("state") or {}).get("code") or "").upper()
        office_state = ((rec.get("officeAddress") or {}).get("state") or "").upper()
        if pop_state and pop_state != "TX":
            return None
        if nationwide and not pop_state and office_state != "TX" and \
                not TEXAS_HINT.search(f"{rec.get('title', '')} {rec.get('fullParentPathName', '')}"):
            return None                          # nationwide NAICS search: keep only if Texas is involved
        if not pop_state and office_state != "TX" and self.mode == "all":
            if not self.config.get("filter.include_unknown_location", True):
                return None
        city = ((pop.get("city") or {}).get("name") or "").title()
        notice_raw = (rec.get("type") or rec.get("baseType") or "").strip().lower()
        notice_type = NOTICE_TYPES.get(notice_raw, "solicitation")
        if notice_type == "award":
            return None
        code = (rec.get("typeOfSetAside") or "").upper()
        path = [p.strip() for p in (rec.get("fullParentPathName") or "").split(".") if p.strip()]
        notice_id = rec.get("noticeId") or rec.get("solicitationNumber")
        attachments = [{"name": f"Attachment {i + 1}", "url": u}
                       for i, u in enumerate(rec.get("resourceLinks") or []) if u]
        opp = self.make_opp(
            source_id=notice_id,
            title=clean(rec.get("title")),
            url=rec.get("uiLink") or f"https://sam.gov/opp/{notice_id}/view",
            solicitation_number=rec.get("solicitationNumber") or "",
            agency=path[0].title() if path else "",
            office=path[-1] if len(path) > 1 else "",
            city=city,
            state=pop_state or office_state or "TX",
            zip=str(pop.get("zip") or ""),
            naics=str(rec.get("naicsCode") or ""),
            psc=str(rec.get("classificationCode") or ""),
            posted_date=parse_date(rec.get("postedDate")),
            response_deadline=parse_date(rec.get("responseDeadLine")),
            set_aside=rec.get("typeOfSetAsideDescription") or SET_ASIDES.get(code, ("", ""))[0],
            set_aside_code=code,
            place_of_performance=", ".join(x for x in [city, pop_state] if x) or
            ("Not listed — contracting office in " + office_state if office_state else "Not listed"),
            attachments=attachments,
            procurement_type=rec.get("type") or "",
            notice_type=notice_type,
            jurisdiction="federal",
            raw={
                "description_link": rec.get("description"),
                "point_of_contact": rec.get("pointOfContact") or [],
                "office_address": rec.get("officeAddress") or {},
                "agency_path": rec.get("fullParentPathName"),
                "pop_missing": not pop_state,
                "additional_info": rec.get("additionalInfoLink"),
            },
        )
        return opp

    def enrich(self, opp):
        """Full description (1 metered request) + attachment text (public file links)."""
        link = (opp.raw or {}).get("description_link")
        if link and str(link).startswith("http") and not opp.description and self.budget.take():
            try:
                resp = self.session.get(link, params={"api_key": self.api_key}, timeout=60)
                if resp.status_code == 200:
                    try:
                        desc = resp.json().get("description", "")
                    except ValueError:
                        desc = resp.text
                    opp.description = html_to_text(desc)[:20000]
            except Exception as exc:
                log.info("SAM description fetch failed for %s: %s", opp.short_id, exc)
        if opp.attachments and not opp.doc_text:
            text, report = extract_attachments(
                opp.attachments,
                max_files=int(self.config.get("limits.max_attachments_per_opp", 3)),
                max_mb=float(self.config.get("limits.max_attachment_mb", 8)),
                max_chars=int(self.config.get("llm.max_chars_per_document", 30000)),
            )
            opp.doc_text = text
            opp.analysis["documents_read"] = report


def _float(v):
    try:
        return float(str(v).replace(",", "").replace("$", "")) if v not in (None, "") else None
    except ValueError:
        return None


def build(config):
    entry = next((e for e in (config.sources.get("federal") or []) if e.get("type") == "sam"), None)
    return [SamConnector(entry, config)] if entry and entry.get("enabled", True) else []
