"""Recognize contract vehicles — IDIQs, MATOCs, BPAs, task orders, term contracts, co-op contracts —
and read them correctly.

Why this matters: an IDIQ's "ceiling" (say $24M) is the most the government MIGHT order across every
holder over several years. It is not revenue. The only money you're promised is the minimum guarantee
(often a few thousand dollars); everything else comes from winning task orders, which are competed again
among the holders (FAR 16.505 "fair opportunity"). Treating the ceiling as contract value would make a
great small-business IDIQ look "too big" and a weak one look lucrative.

Task orders under an EXISTING vehicle are usually open only to that vehicle's holders. Those are flagged
`holders_only` so the filter can explain why you can't bid — and the holders list becomes a teaming lead.
"""
from __future__ import annotations

import re

from .extract import money_near, snippet

FAIR_OPPORTUNITY = "https://www.acquisition.gov/far/16.505"

# (key, label, pattern) — first match wins, most specific first
TYPES = [
    ("matoc", "Multiple-award IDIQ (MATOC)",
     r"\bMATOCs?\b|multiple[- ]award (?:task order |delivery order |indefinite)|multiple[- ]award IDIQ|"
     r"multiple[- ]award contract|\bMACs?\b(?=[^a-z]{0,3}(?:contract|idiq|vehicle))"),
    ("satoc", "Single-award IDIQ (SATOC)", r"\bSATOCs?\b|single[- ]award (?:task order |IDIQ|indefinite)"),
    ("idiq", "IDIQ contract",
     r"\bIDIQ\b|\bID/IQ\b|indefinite[- ]delivery[, /-]+indefinite[- ]quantity|indefinite[- ]quantity|"
     r"indefinite[- ]delivery (?:contract|vehicle)|requirements[- ]type contract|\brequirements contract\b"),
    ("bpa", "Blanket purchase agreement (BPA)", r"blanket purchase agreement|\bBPAs?\b"),
    ("cooperative", "Cooperative purchasing contract",
     r"cooperative (?:purchasing|contract)|purchasing cooperative|interlocal (?:agreement|contract)|piggyback"),
    ("jobs_order", "Job order contract (JOC)", r"job order contract(?:ing)?|\bJOC\b"),
    ("term", "Term / as-needed contract",
     r"\bterm contract\b|annual (?:service )?contract|as[- ]needed basis|on[- ]call (?:services|basis|contract)|"
     r"master (?:services |price )?agreement|price agreement|\bas-needed\b"),
]
ORDER_UNDER_VEHICLE = re.compile(
    r"task order (?:request|proposal request|RFP|RFQ)|delivery order (?:request|RFQ)|\bTORFP\b|\bRFTOP\b|"
    r"\bFOPR\b|fair opportunity (?:notice|request|proposal)|request for task order|"
    r"only (?:current |existing )?(?:holders|awardees|contract holders|BPA holders)|"
    r"(?:issued|competed) (?:under|against|among (?:the )?(?:holders|awardees) of) (?:the |an |existing )?"
    r"[^.]{0,60}(?:IDIQ|MATOC|BPA|contract|schedule)", re.I)
NEW_VEHICLE = re.compile(r"(?:will|intends to|anticipates?|plans? to) (?:award|make)[^.]{0,110}"
                         r"(?:IDIQ|MATOC|SATOC|awards|contracts|BPAs?)|on[- ]?ramp|seed (?:task|delivery) order",
                         re.I)
ON_RAMP = re.compile(r"\bon[- ]?ramp", re.I)
AWARD_COUNT = re.compile(r"(?:up to|approximately|about|anticipate[sd]? (?:awarding|making)|intends? to award|"
                         r"plans? to award)\s+(\w+)(?:\s*\(\d+\))?\s+(?:[\w,()/-]+\s+){0,6}?(?:awards|contracts|IDIQs|BPAs)\b",
                         re.I)
ORDER_YEARS = re.compile(r"(\w+)(?:\s*\(\d+\))?[- ]year ordering period|ordering period of (\w+)(?:\s*\(\d+\))? years",
                         re.I)
WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
         "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20}

MIN_TERMS = ["minimum guarantee", "guaranteed minimum", "minimum guaranteed", "guaranteed a minimum", "minimum order amount",
             "minimum amount", "guaranteed amount"]
CEILING_TERMS = ["program ceiling", "shared ceiling", "contract ceiling", "maximum ceiling", "ceiling of",
                 "total ceiling", "maximum value", "maximum contract value", "maximum order amount",
                 "not-to-exceed", "not to exceed", "aggregate maximum"]
ANNUAL_TERMS = ["estimated annual", "annual estimate", "anticipated annual", "estimated yearly"]


def _num(word: str | None) -> int | None:
    if not word:
        return None
    w = word.lower()
    if w.isdigit():
        n = int(w)
        return n if 0 < n < 500 else None
    return WORDS.get(w)


def detect(text: str, title: str = "", jurisdiction: str = "") -> dict:
    """Returns {} when it's an ordinary one-off contract, else a dict describing the vehicle."""
    blob = f"{title}\n{text or ''}"
    kind = label = ""
    evidence = ""
    for key, lab, pattern in TYPES:
        m = re.search(pattern, blob, re.I)
        if m:
            kind, label, evidence = key, lab, snippet(blob, m.start(), m.end())
            break
    if not kind and jurisdiction == "cooperative":
        kind, label = "cooperative", "Cooperative purchasing contract"
    order = ORDER_UNDER_VEHICLE.search(blob)
    if not kind and not order:
        return {}
    new_vehicle = bool(NEW_VEHICLE.search(blob))
    holders_only = bool(order) and not new_vehicle
    if holders_only and not kind:
        kind, label = "task_order", "Task order under an existing contract"
    info = {"type": kind, "label": label, "holders_only": holders_only,
            "on_ramp": bool(ON_RAMP.search(blob)), "evidence": evidence or (snippet(blob, order.start(), order.end())
                                                                           if order else "")}
    if holders_only:
        info["label"] = "Task order under an existing contract"
        info["order_evidence"] = snippet(blob, order.start(), order.end())
    mins = [v for v, _ in money_near(blob, MIN_TERMS, window=90) if v >= 100]
    ceilings = [v for v, _ in money_near(blob, CEILING_TERMS, window=90) if v >= 1000]
    annual = [v for v, _ in money_near(blob, ANNUAL_TERMS, window=90) if v >= 1000]
    if mins:
        info["min_guarantee"] = min(mins)
    if ceilings:
        info["ceiling"] = max(ceilings)
    if annual:
        info["estimated_annual"] = max(annual)
    m = AWARD_COUNT.search(blob)
    if m and _num(m.group(1)):
        info["awards"] = _num(m.group(1))
    m = ORDER_YEARS.search(blob)
    if m and _num(m.group(1) or m.group(2)):
        info["ordering_years"] = _num(m.group(1) or m.group(2))
    return info


def rough_annual_share(v: dict) -> float | None:
    """A deliberately conservative guess at what ONE holder might see per year — for scoring only."""
    if not v:
        return None
    if v.get("estimated_annual"):
        return v["estimated_annual"] / max(1, v.get("awards") or 1)
    if v.get("ceiling"):
        holders = v.get("awards") or (1 if v.get("type") == "satoc" else 4)
        years = v.get("ordering_years") or 5
        return v["ceiling"] / holders / years * 0.5        # ceilings are rarely fully used
    return None


def value_label(v: dict, fmt) -> str:
    """Plain-English value for a vehicle, e.g. 'Ceiling $24.0M shared by up to 5 holders; $2,500 guaranteed'."""
    parts = []
    if v.get("estimated_annual"):
        parts.append(f"about {fmt(v['estimated_annual'])}/yr estimated")
    if v.get("ceiling"):
        share = f" shared by up to {v['awards']} holders" if v.get("awards") and v["awards"] > 1 else ""
        parts.append(f"ceiling {fmt(v['ceiling'])}{share} (not guaranteed)")
    if v.get("min_guarantee"):
        parts.append(f"{fmt(v['min_guarantee'])} guaranteed")
    text = "; ".join(parts)
    return text[:1].upper() + text[1:]
