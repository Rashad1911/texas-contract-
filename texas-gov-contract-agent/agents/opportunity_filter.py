"""AGENT 2 — Opportunity Filter.

Classifies each opportunity into a service category and removes obvious bad fits. Every rejection
gets a plain-English reason you can read later. Difficult is NOT the same as disqualified: hard-but-
possible items pass through and the Red Team agent weighs them instead.
"""
from __future__ import annotations

import re

from connectors.sam import SET_ASIDES
from core import vehicles
from core.config import Config
from core.extract import days_until, fmt_money, money_near
from core.opportunity import Opportunity

SERVICE_WORDS = re.compile(r"\b(services?|maintenance|cleaning|removal|repair|install|support|management|"
                           r"collection|hauling|mowing|washing|transport|pickup|abatement|inspection)\b", re.I)
CLEARANCE = re.compile(r"\b(top secret|ts/sci|secret (?:security )?clearance|security clearance (?:is |shall be )?required|"
                       r"facility (?:security )?clearance|\bFCL\b)", re.I)
STAFF_COUNT = re.compile(r"\b(\d{2,4})\s+(?:full[- ]time\s+)?(?:FTEs?|employees|personnel|guards|officers|workers|staff)\b", re.I)
# Phrases so broad they shouldn't outweigh a specific one ("window cleaning" beats "cleaning services").
GENERIC_KEYWORDS = {"cleaning services", "maintenance services", "repair services", "support services",
                    "facility services", "building services", "general services"}
VALUE_TERMS = ["estimated value", "estimated contract value", "estimated amount", "not to exceed", "nte",
               "ceiling", "maximum value", "total value", "budget", "annual value", "estimated annual",
               "estimated cost", "magnitude", "estimated at"]


class OpportunityFilter:
    def __init__(self, config: Config):
        self.config = config
        self.categories = config.service_categories()
        self.company = config.company
        self.supply = [s.lower() for s in config.services.get("supply_signals", [])]
        self.out_of_scope = [s.lower() for s in config.services.get("out_of_scope_signals", [])]
        self.naics_map: dict[str, list[str]] = {}
        for key, cat in self.categories.items():
            for code in cat.get("naics", []):
                self.naics_map.setdefault(str(code), []).append(key)

    # ── classification ───────────────────────────────────────────────
    def classify(self, opp: Opportunity) -> tuple[str, float, list[str]]:
        title = (opp.title or "").lower()
        body = opp.text().lower()
        scores: dict[str, float] = {}
        hits: dict[str, list[str]] = {}
        for key, cat in self.categories.items():
            if any(re.search(rf"\b{re.escape(x.lower())}", title) for x in cat.get("exclude", [])):
                continue                     # e.g. "elevator maintenance and repair" is a licensed specialty trade
            s = 0.0
            for kw in cat.get("keywords", []):
                kw_l = kw.lower()
                pattern = rf"\b{re.escape(kw_l)}"
                if re.search(pattern, title):
                    s += 1.5 if kw_l in GENERIC_KEYWORDS else 3
                    hits.setdefault(key, []).append(kw)
                elif re.search(pattern, body):
                    s += 1
                    hits.setdefault(key, []).append(kw)
            if opp.naics and str(opp.naics) in [str(n) for n in cat.get("naics", [])]:
                s += 2.5 if len(self.naics_map.get(str(opp.naics), [])) == 1 else 1.2
            if s:
                scores[key] = s
        if not scores:
            return "", 0.0, []
        best = max(scores, key=scores.get)
        return best, scores[best], sorted(set(hits.get(best, [])))[:6]

    # ── evaluation ───────────────────────────────────────────────────
    def evaluate(self, opp: Opportunity) -> tuple[bool, str]:
        """Returns (passed, reason). Also fills opp.service_category, value, and pre-score."""
        vehicle = vehicles.detect(opp.text(), opp.title, opp.jurisdiction)
        if vehicle:
            opp.analysis["vehicle"] = vehicle
        else:
            opp.analysis.pop("vehicle", None)
        self._fill_value(opp, vehicle)
        cat, strength, terms = self.classify(opp)
        opp.service_category = cat
        opp.analysis["classification"] = {"category": cat, "strength": round(strength, 1), "matched": terms}
        title_l = (opp.title or "").lower()
        text = opp.text()
        days = days_until(opp.response_deadline)

        if days is not None and days < 0:
            return False, "Deadline has passed."
        if opp.notice_type == "award":
            return False, "Award notice (already awarded) — kept for contract history only."

        if not cat:
            if SERVICE_WORDS.search(opp.title or ""):
                opp.analysis["unclassified_service"] = True
                return False, "A service contract, but not in your service list yet (the Discovery agent reviews these)."
            return False, "Not a match for any of your service categories."

        if not self.config.get("filter.include_supply_contracts", False):
            supply_hit = next((s for s in self.supply if s in title_l), None)
            if supply_hit and strength < 6 and not re.search(r"\bservices?\b", title_l):
                return False, f"Looks like a goods purchase ('{supply_hit}'), not a service contract."

        scope_hit = next((s for s in self.out_of_scope if re.search(rf"\b{re.escape(s)}\b", title_l)), None)
        if scope_hit:
            return False, f"Outside the model — major construction or professional services ('{scope_hit}')."

        code = (opp.set_aside_code or "").upper()
        if code in SET_ASIDES:
            label, needed = SET_ASIDES[code]
            certs = {c.upper() for c in self.company.get("certifications", [])}
            if needed == "small" and not self.company.get("small_business", True):
                return False, f"{label}: only small businesses may bid."
            if needed not in ("small",) and needed not in certs:
                if "Sole Source" in label:
                    return False, f"{label} — not open to competition."
                return False, f"{label} — your LLC doesn't hold the {needed} certification (settings.yaml → certifications)."

        if vehicle.get("holders_only"):
            held = [h.lower() for h in self.company.get("vehicles_held") or []]
            if not any(h and h in text.lower() for h in held):
                return False, ("Task order under an existing contract — only that contract's current holders can "
                               "respond. See Contract vehicles on the dashboard for holders you could subcontract to.")

        m = CLEARANCE.search(text)
        if m:
            return False, f"Requires a security clearance ('{m.group(0)}')."

        if not opp.market:
            if opp.jurisdiction == "federal" and (opp.raw or {}).get("pop_missing") and \
                    self.config.get("filter.include_unknown_location", True):
                opp.market = "unknown"
            else:
                where = opp.place_of_performance or opp.city or "unknown location"
                return False, f"Work is outside your target markets ({where})."
        if opp.market == "statewide" and not self.config.get("filter.include_statewide", True):
            return False, "Statewide opportunity and statewide items are switched off."

        min_days = float(self.config.get("filter.min_days_to_deadline", 4))
        if days is not None and days < min_days and opp.notice_type == "solicitation":
            return False, f"Only {max(days, 0):.0f} day(s) left — not enough time to prepare a responsible bid."

        min_value = float(self.config.get("filter.min_contract_value", 25000))
        if opp.estimated_value and opp.estimated_value < min_value and not vehicle:   # vehicles are judged by orders
            return False, f"Estimated value {fmt_money(opp.estimated_value)} is below your {fmt_money(min_value)} minimum."

        staff = [int(x) for x in STAFF_COUNT.findall(text)]
        if staff and max(staff) >= 60 and not re.search(r"\b\d{2,4}\s+(?:employees|personnel)\s+or\s+fewer", text, re.I):
            return False, f"Needs a very large workforce (~{max(staff)} people)."

        opp.analysis["pre_score"] = self._pre_score(opp, strength, days)
        return True, ""

    def _fill_value(self, opp: Opportunity, vehicle: dict | None = None):
        if vehicle:
            # never treat an IDIQ/BPA ceiling as the contract's value — only an estimated annual amount counts
            if opp.estimated_value and vehicle.get("ceiling") and opp.estimated_value >= vehicle["ceiling"]:
                opp.estimated_value, opp.value_basis = None, ""
            if not opp.estimated_value and vehicle.get("estimated_annual"):
                opp.estimated_value = vehicle["estimated_annual"]
                opp.value_basis = "Estimated annual ordering stated in the solicitation (spread across holders)"
            return
        if opp.estimated_value:
            return
        found = money_near(opp.text(), VALUE_TERMS)
        if found:
            value, evidence = max(found, key=lambda x: x[0])
            if value >= 1000:
                opp.estimated_value = value
                opp.value_basis = f"Stated in the solicitation text: {evidence}"

    def _pre_score(self, opp: Opportunity, strength: float, days: float | None) -> float:
        cat = self.categories.get(opp.service_category, {})
        s = min(strength, 8) * 3
        s += {"high": 12, "medium": 6, "low": 0}.get(cat.get("outsource", "medium"), 6)
        s += {"low": 8, "medium": 4, "high": 0}.get(cat.get("startup", "medium"), 4)
        s += 10 if opp.market == self.company.get("home_market", "dfw") else 6 if opp.market not in ("unknown",) else 2
        if days is not None:
            s += 8 if days > 21 else 5 if days > 10 else 2
        if opp.service_category in self.company.get("existing_capabilities", []):
            s += 8
        if opp.notice_type != "solicitation":
            s -= 4
        return round(s, 1)
