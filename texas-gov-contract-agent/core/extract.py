"""Small, dependency-light helpers for cleaning text and pulling out dates and dollar amounts."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

LOCAL_TZ = ZoneInfo("America/Chicago")

_WS = re.compile(r"\s+")


def clean(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        return clean(BeautifulSoup(html, "html.parser").get_text(" "))
    except Exception:
        return clean(re.sub(r"<[^>]+>", " ", html))


def now() -> datetime:
    return datetime.now(LOCAL_TZ)


def parse_date(value, default_tz=LOCAL_TZ) -> datetime | None:
    """Parse almost any date string. Naive results are treated as Central time."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = value / 1000 if value > 10_000_000_000 else value
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = dateparser.parse(text, fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def days_until(dt: datetime | None) -> float | None:
    if not dt:
        return None
    return (dt - now()).total_seconds() / 86400


_DATE_PATTERNS = [
    r"\b\d{1,2}/\d{1,2}/\d{2,4}(?:,?\s+\d{1,2}:\d{2}\s*(?:[AaPp]\.?[Mm]\.?)?)?",
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?",
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"(?:,?\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?)?",
]
_DATE_RE = re.compile("|".join(_DATE_PATTERNS))


def find_dates(text: str) -> list[datetime]:
    found = []
    for match in _DATE_RE.finditer(text or ""):
        dt = parse_date(match.group(0))
        if dt and 2000 < dt.year < 2100:
            found.append(dt)
    return found


def deadline_from_text(text: str) -> datetime | None:
    """Pick the most plausible response deadline from free text (label-aware, then latest future date)."""
    if not text:
        return None
    labeled = re.search(
        r"(?:due|closes?|closing|deadline|response date|submission|bid opening|opens?)\s*(?:date)?\s*[:\-]?\s*(.{0,60})",
        text, re.I)
    if labeled:
        dates = find_dates(labeled.group(1))
        if dates:
            return dates[0]
    future = [d for d in find_dates(text) if (days_until(d) or -1) > -1]
    return max(future) if future else None


_MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*(million|mil\b|m\b|k\b|thousand)?", re.I)


def parse_money(text: str | None) -> float | None:
    if not text:
        return None
    match = _MONEY_RE.search(str(text))
    if not match:
        try:
            return float(str(text).replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    amount = float(match.group(1).replace(",", ""))
    if match.group(2):
        amount += float("0." + match.group(2))
    unit = (match.group(3) or "").lower()
    if unit in ("million", "mil", "m"):
        amount *= 1_000_000
    elif unit in ("k", "thousand"):
        amount *= 1_000
    return amount


def money_near(text: str, keywords: list[str], window: int = 120) -> list[tuple[float, str]]:
    """Dollar amounts that appear within `window` characters after any keyword. Returns (amount, snippet)."""
    results = []
    if not text:
        return results
    lowered = text.lower()
    for kw in keywords:
        for m in re.finditer(re.escape(kw.lower()), lowered):
            chunk = text[m.start(): m.end() + window]
            for money in _MONEY_RE.finditer(chunk):
                value = parse_money(money.group(0))
                if value:
                    results.append((value, snippet(text, m.start(), m.end() + window)))
    return results


def snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    out = clean(text[s:e])
    return ("…" if s > 0 else "") + out + ("…" if e < len(text) else "")


def find_first(patterns: list[str], text: str, flags=re.I) -> tuple[re.Match | None, str]:
    """First regex match across patterns plus a readable evidence snippet."""
    for pattern in patterns:
        match = re.search(pattern, text or "", flags)
        if match:
            return match, snippet(text, match.start(), match.end())
    return None, ""


def fmt_money(value: float | None) -> str:
    if value is None:
        return "Not listed"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"${value / 1_000:,.0f}K"
    return f"${value:,.0f}"
