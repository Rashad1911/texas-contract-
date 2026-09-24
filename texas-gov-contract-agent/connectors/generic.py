"""Reusable connectors for portals without a dedicated API.

  HtmlListConnector  public listing pages (tables, lists of links)
  RssConnector       RSS feeds, optionally filtered by title
  JsonApiConnector   a JSON endpoint you paste into sources.yaml (for JavaScript-only portals)
  BeaconConnector    Beacon Bid agency pages (reads the page's embedded data)
  ManualConnector    portals that need a login — listed on the dashboard, never scraped
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core import http
from core.extract import clean, deadline_from_text, html_to_text, parse_date, parse_money

from .base import BaseConnector, SourceError
from .documents import extract_attachments

SOLICITATION_NO = re.compile(
    r"\b(?:IFB|RFP|RFQ|RFB|RFCSP|RFx|CSP|ITB|BID|RFI|RFO|SOL)[\s#:\-]*[A-Z0-9][A-Z0-9\-/]{2,}\b"
    r"|\b[A-Z]{1,4}\d{2}[-/]\d{3,}[A-Z0-9\-]*\b|\b\d{2}/\d{4}\b", re.I)
NAV_WORDS = {"home", "login", "log in", "register", "contact", "faq", "faqs", "help", "search", "about",
             "sign in", "create account", "privacy", "accessibility", "back to top", "print", "share"}


def _get_text(url: str) -> str:
    try:
        resp = http.get(url)
    except Exception as exc:
        raise SourceError(f"Could not reach the page ({exc.__class__.__name__}).") from exc
    if resp.status_code != 200:
        raise SourceError(f"The page returned HTTP {resp.status_code}.")
    return resp.text


class HtmlListConnector(BaseConnector):
    type = "html"

    def fetch(self):
        html = _get_text(self.url)
        soup = BeautifulSoup(html, "html.parser")
        link_pattern = self.cfg.get("link_pattern")
        rows = soup.select(self.cfg.get("row_selector", "")) if self.cfg.get("row_selector") else []
        if not rows:
            rows = [tr for tr in soup.find_all("tr") if tr.find("a", href=True)]
        if not rows:
            rows = soup.find_all(["li", "article", "div"],
                                 class_=re.compile(r"bid|solicit|opportun|rfp|listing", re.I))
        out, seen = [], set()
        for row in rows:
            link = row.find("a", href=True) if hasattr(row, "find") else None
            if not link:
                continue
            href = urljoin(self.url, link["href"])
            if link_pattern and not re.search(link_pattern, href, re.I):
                continue
            title = clean(link.get_text(" "))
            row_text = clean(row.get_text(" "))
            if len(title) < 8 or title.lower() in NAV_WORDS:
                title = row_text[:180]
            if len(title) < 8 or title.lower() in NAV_WORDS or href in seen:
                continue
            if not link_pattern and not _looks_like_solicitation(row_text, href):
                continue
            seen.add(href)
            sol = SOLICITATION_NO.search(row_text)
            out.append(self.make_opp(
                source_id=(sol.group(0) if sol else hashlib.sha1(href.encode()).hexdigest()[:12]),
                title=title,
                url=href,
                solicitation_number=sol.group(0) if sol else "",
                response_deadline=deadline_from_text(row_text),
                description=row_text if row_text != title else "",
            ))
        return out

    def enrich(self, opp):
        """Open the solicitation's detail page and keep its text (public pages only)."""
        if not opp.url or opp.url == self.url:
            return
        html = _safe_get(opp.url)
        if not html:
            return
        text = html_to_text(html)
        if len(text) > len(opp.description):
            opp.description = text[:20000]
        if not opp.response_deadline:
            opp.response_deadline = deadline_from_text(text)
        known = {a.get("url") for a in opp.attachments}
        for match in re.finditer(r'href="([^"]+\.(?:pdf|docx))"', html, re.I):
            url = urljoin(opp.url, match.group(1))
            if url not in known:
                known.add(url)
                opp.attachments.append({"name": url.rsplit("/", 1)[-1], "url": url})
        if opp.attachments and not opp.doc_text:          # public PDFs/DOCX: read the actual requirements
            text, report = extract_attachments(
                opp.attachments,
                max_files=int(self.config.get("limits.max_attachments_per_opp", 3)),
                max_mb=float(self.config.get("limits.max_attachment_mb", 8)),
                max_chars=int(self.config.get("llm.max_chars_per_document", 30000)))
            opp.doc_text = text
            opp.analysis["documents_read"] = report


def _safe_get(url: str) -> str:
    try:
        return _get_text(url)
    except SourceError:
        return ""


def _looks_like_solicitation(text: str, href: str) -> bool:
    blob = f"{text} {href}".lower()
    return bool(SOLICITATION_NO.search(text)) or any(
        w in blob for w in ("bid", "rfp", "rfq", "ifb", "solicitation", "proposal", "services", "contract"))


class RssConnector(BaseConnector):
    type = "rss"

    def fetch(self):
        body = _get_text(self.url)
        try:
            root = ET.fromstring(body.encode("utf-8", "ignore"))
        except ET.ParseError as exc:
            raise SourceError("The feed isn't valid RSS/XML — check the feed URL.") from exc
        title_filter = (self.cfg.get("title_filter") or "").lower()
        out, seen = [], set()
        for item in root.iter("item"):
            title = clean(item.findtext("title"))
            if not title or (title_filter and title_filter not in title.lower()):
                continue
            link = clean(item.findtext("link")) or self.url
            desc = html_to_text(item.findtext("description") or "")
            key = re.sub(r"\W+", "", title.lower())
            event = re.search(r"event\s*#\s*(\d+)", title, re.I)
            source_id = event.group(1) if event else hashlib.sha1(key.encode()).hexdigest()[:12]
            if source_id in seen:          # calendars repeat the same bid on several dates
                continue
            seen.add(source_id)
            closes = re.search(r"closes?:\s*(.{0,60})", desc, re.I)
            deadline = parse_date(closes.group(1)) if closes else deadline_from_text(desc)
            clean_title = re.sub(r"^bid alert!?\s*(event\s*#\s*\d+\s*[-–:]\s*)?", "", title, flags=re.I).strip()
            out.append(self.make_opp(source_id=source_id, title=clean_title or title, url=link,
                                     solicitation_number=f"Event #{event.group(1)}" if event else "",
                                     description=desc, response_deadline=deadline))
        return out


class JsonApiConnector(BaseConnector):
    """For portals that are JavaScript apps (e.g. Texas ESBD). Paste the JSON request URL the page
    itself uses into `api_url` in sources.yaml — README §6 shows how to find it in 2 minutes."""
    type = "json_api"

    def fetch(self):
        api_url = self.cfg.get("api_url")
        if not api_url:
            raise SourceError("SETUP: add this portal's JSON listing URL as api_url in config/sources.yaml "
                              "(README §6). Until then, check it by hand.")
        method = (self.cfg.get("api_method") or "GET").upper()
        try:
            if method == "POST":
                resp = http.post(api_url, json=self.cfg.get("api_body") or {})
            else:
                resp = http.get(api_url, params=self.cfg.get("api_params") or {})
            data = resp.json()
        except Exception as exc:
            raise SourceError(f"JSON endpoint failed ({exc.__class__.__name__}).") from exc
        fields = self.cfg.get("fields") or {}
        return [o for o in (self._record_to_opp(r, fields) for r in find_records(data)) if o]

    def _record_to_opp(self, rec: dict, fields: dict):
        title = _field(rec, fields.get("title"), "title", "name", "solicitationTitle", "description", "subject")
        if not title:
            return None
        rid = _field(rec, fields.get("id"), "solicitationId", "solicitationID", "bidNumber", "id", "number", "slug")
        deadline = _field(rec, fields.get("deadline"), "responseDueDate", "dueDate", "closeDate", "closingDate",
                          "responseDue", "deadline", "endDate")
        agency = _field(rec, fields.get("agency"), "agency", "agencyName", "organization", "department")
        url = self.url
        if fields.get("url_template") and rid:
            url = fields["url_template"].format(id=rid, **{k: v for k, v in rec.items() if isinstance(v, (str, int))})
        elif _field(rec, None, "url", "link", "href"):
            url = urljoin(self.url, _field(rec, None, "url", "link", "href"))
        return self.make_opp(source_id=rid or title, title=clean(title), url=url,
                             solicitation_number=str(rid or ""), agency=clean(agency) or self.name,
                             response_deadline=parse_date(deadline),
                             description=clean(_field(rec, fields.get("description"), "summary", "details")),
                             estimated_value=parse_money(_field(rec, None, "estimatedValue", "value", "amount")),
                             raw={"record": {k: rec[k] for k in list(rec)[:30]}})


class BeaconConnector(JsonApiConnector):
    """Beacon Bid agency pages embed their data as JSON in the page (Next.js). We read that."""
    type = "beacon"

    def fetch(self):
        if self.cfg.get("api_url"):
            return super().fetch()
        html = _get_text(self.url)
        match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        records = []
        if match:
            try:
                records = find_records(json.loads(match.group(1)))
            except ValueError:
                records = []
        if not records:
            raise SourceError("Beacon page loaded but no opportunity data was found in it — check by hand "
                              "or set api_url (README §6).")
        fields = self.cfg.get("fields") or {}
        out = [o for o in (self._record_to_opp(r, fields) for r in records) if o]
        for opp in out:
            rid = opp.raw.get("record", {}).get("id") or opp.raw.get("record", {}).get("slug")
            if rid and opp.url == self.url:
                opp.url = self.url.replace("/open", f"/{rid}") if "/open" in self.url else self.url
        return out


class ManualConnector(BaseConnector):
    type = "manual"

    def fetch(self):
        raise SourceError("MANUAL: this portal needs a login to view solicitations. It's listed on your "
                          "dashboard so you can check it by hand.")


# ── helpers ───────────────────────────────────────────────────────────
def _field(rec: dict, preferred, *candidates):
    keys = ([preferred] if preferred else []) + list(candidates)
    lower = {k.lower(): k for k in rec}
    for key in keys:
        if key in rec and rec[key] not in (None, ""):
            return rec[key] if not isinstance(rec[key], dict) else rec[key].get("name") or ""
        real = lower.get(key.lower())
        if real and rec[real] not in (None, ""):
            return rec[real] if not isinstance(rec[real], dict) else rec[real].get("name") or ""
    return ""


def find_records(obj, depth: int = 0) -> list[dict]:
    """Find the largest list of dicts that look like solicitations anywhere in a JSON document."""
    best: list[dict] = []
    if depth > 8:
        return best
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]):
        sample = " ".join(k.lower() for k in obj[0].keys())
        if any(w in sample for w in ("title", "name", "solicitation", "bid")) and \
           any(w in sample for w in ("due", "close", "deadline", "date", "end")):
            best = obj
    children = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for child in children:
        if isinstance(child, (dict, list)):
            found = find_records(child, depth + 1)
            if len(found) > len(best):
                best = found
    return best
