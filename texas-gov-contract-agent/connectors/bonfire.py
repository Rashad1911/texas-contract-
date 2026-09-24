"""Bonfire public portals (City of Dallas, City of Fort Worth, Harris County).

Bonfire's public portal page loads its opportunity table from a JSON endpoint on the same host.
This connector reads that public list (no login). If Bonfire changes the endpoint, the source shows
as "error" in Source health and you can still open the portal link by hand.
"""
from __future__ import annotations

import difflib
import time

from core import http
from core.extract import clean, parse_date

from .base import BaseConnector, SourceError

OPEN_ENDPOINT = "/PublicPortal/getOpenPublicOpportunitiesSectionData"
PAST_ENDPOINT = "/PublicPortal/getPastPublicOpportunitiesSectionData"


def _pick(d: dict, *keys, default=""):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


class BonfireConnector(BaseConnector):
    type = "bonfire"

    def _load(self, endpoint: str) -> list[dict]:
        url = self.base_url + endpoint
        try:
            resp = http.get(url, params={"_": int(time.time() * 1000)},
                            headers={"X-Requested-With": "XMLHttpRequest", "Referer": self.url})
        except Exception as exc:
            raise SourceError(f"Could not reach Bonfire ({exc.__class__.__name__}).") from exc
        if resp.status_code != 200:
            raise SourceError(f"Bonfire returned HTTP {resp.status_code}.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError("Bonfire did not return JSON — the public endpoint may have changed.") from exc
        payload = data.get("payload", data) if isinstance(data, dict) else data
        projects = payload.get("projects", payload) if isinstance(payload, dict) else payload
        if isinstance(projects, dict):
            projects = list(projects.values())
        if not isinstance(projects, list):
            raise SourceError("Unexpected Bonfire response shape.")
        return [p for p in projects if isinstance(p, dict)]

    def fetch(self):
        out = []
        for p in self._load(OPEN_ENDPOINT):
            pid = _pick(p, "ProjectID", "projectID", "id", "ID")
            title = clean(str(_pick(p, "ProjectName", "Name", "name", "title")))
            if not title:
                continue
            ref = clean(str(_pick(p, "ReferenceID", "referenceID", "Reference", "ref")))
            close = parse_date(_pick(p, "DateClose", "CloseDate", "dateClose", "closeDate", default=None))
            dept = clean(str(_pick(p, "DepartmentName", "Department", "department")))
            ptype = clean(str(_pick(p, "ProjectType", "Type", "type")))
            opp = self.make_opp(
                source_id=pid or ref,
                title=title,
                url=f"{self.base_url}/opportunities/{pid}" if pid else self.url,
                solicitation_number=ref,
                office=dept,
                response_deadline=close,
                procurement_type=ptype,
                description=clean(str(_pick(p, "Description", "description", "ShortDescription"))),
                raw={"bonfire": {k: p.get(k) for k in list(p)[:25]}},
            )
            out.append(opp)
        return out

    def past_awards(self, opp):
        """Closed Bonfire projects with a similar title — the local 'what happened last time'."""
        try:
            past = self._load(PAST_ENDPOINT)
        except SourceError:
            return []
        scored = []
        for p in past:
            title = clean(str(_pick(p, "ProjectName", "Name", "name")))
            ratio = difflib.SequenceMatcher(None, title.lower(), opp.title.lower()).ratio()
            if ratio >= 0.55:
                pid = _pick(p, "ProjectID", "id")
                scored.append((ratio, {
                    "title": title,
                    "reference": _pick(p, "ReferenceID"),
                    "closed": str(_pick(p, "DateClose", "CloseDate")),
                    "status": str(_pick(p, "ProjectStatus", "Status")),
                    "url": f"{self.base_url}/opportunities/{pid}" if pid else self.cfg.get("awards_url", ""),
                    "source": self.name,
                }))
        scored.sort(key=lambda x: -x[0])
        return [s[1] for s in scored[:5]]
