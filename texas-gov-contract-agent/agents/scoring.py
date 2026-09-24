"""Scoring — how well an opportunity matches YOUR criteria (not how likely you are to win).

Fifteen factors score 0–10 each; weights in settings.yaml → scoring.weights. The weighted average is
scaled to 0–100, then the Red Team verdict and notice stage decide the final bucket:

  top      strong fit on the evidence, survived red team, biddable now
  review   worth a look but has real questions or risks
  watch    interesting but not ready (early notice, queued, or middling fit)
  filtered not sent to you (reason stored)
"""
from __future__ import annotations

import math

from core import vehicles as veh
from core.config import Config
from core.extract import days_until
from core.opportunity import Opportunity

LEVEL = {"low": 0, "medium": 1, "high": 2}
BLOCKING_AREAS = ("Mandatory meeting",)   # kill findings that make bidding impossible → filtered
FACTOR_LABELS = {                      # phrased so a HIGH score reads as good news
    "startup_difficulty": "Easy to start",
    "capital_required": "Low capital needed",
    "outsourcing_potential": "Can be subcontracted",
    "contract_value": "Worthwhile contract size",
    "competition": "Beatable competition",
    "experience_requirements": "Low experience bar",
    "licensing_requirements": "Few licenses needed",
    "insurance_requirements": "Manageable insurance",
    "bonding": "Little or no bonding",
    "time_to_deadline": "Time to prepare",
    "geographic_fit": "Location fit",
    "complexity": "Simple scope",
    "margin_potential": "Margin potential",
    "past_award_info": "Past award data available",
    "small_llc_realism": "Realistic for a small LLC",
}


class Scorer:
    def __init__(self, config: Config):
        self.config = config
        self.weights = config.get("scoring.weights", {}) or {}
        self.thresholds = config.get("scoring.thresholds", {}) or {}
        self.company = config.company

    def factors(self, opp: Opportunity) -> dict[str, float]:
        a = opp.analysis
        cat = self.config.service_categories().get(opp.service_category, {})
        req = a.get("requirements", {})
        facts = req.get("facts", {})
        sub = a.get("subcontracting", {})
        awards = a.get("award_history", {})
        red = a.get("red_team", {})
        startup = LEVEL.get(cat.get("startup", "medium"), 1)
        bonds = bool(facts.get("perf_payment_bond") or facts.get("bid_bond"))
        gl = facts.get("general_liability") or 0
        days = days_until(opp.response_deadline)

        f: dict[str, float] = {}
        f["startup_difficulty"] = [9, 6, 3][startup] - (1.5 if bonds else 0) - (1 if gl > 2_000_000 else 0)
        f["capital_required"] = [9, 6, 2][startup] - (2 if opp.service_category in ("staffing", "equipment_rental") else 0)
        f["outsourcing_potential"] = {"SUPPORTED": 10, "APPROVAL_REQUIRED": 7, "UNKNOWN": 5.5, "LIMITED": 4,
                                      "RESTRICTED": 0}.get(sub.get("status", "UNKNOWN"), 5) \
            + {"high": 0, "medium": -1, "low": -3}.get(cat.get("outsource", "medium"), -1)
        vehicle = a.get("vehicle") or {}
        v = opp.estimated_value
        if vehicle:
            v = veh.rough_annual_share(vehicle) or (v / max(1, vehicle.get("awards") or 1) if v else None)
        if v is None:
            v = awards.get("previous_award") or awards.get("median_similar_award")
        f["contract_value"] = 5 if not v else 2 if v < 25_000 else 5 if v < 100_000 else 8 if v < 500_000 \
            else 10 if v < 2_000_000 else 7 if v < 10_000_000 else 4
        if vehicle and vehicle.get("type") in ("matoc", "idiq", "bpa", "cooperative", "term", "jobs_order"):
            f["contract_value"] = min(f["contract_value"], 7.5)    # upside, but nothing beyond the minimum is promised
        comp = 5.0
        if opp.set_aside_code in ("SBA", "SBP") or "small business" in (opp.set_aside or "").lower():
            comp = 7.5
        elif opp.jurisdiction == "federal" and not opp.set_aside:
            comp = 3.5
        if facts.get("mwbe_goals") or facts.get("local_preference"):
            comp += 1
        if vehicle.get("type") == "matoc":
            comp -= 0.5                                  # you compete once for the seat, then again for each order
        n_awards = awards.get("number_of_similar_awards") or 0
        comp -= 1.5 if n_awards >= 8 else 0
        comp -= 1 if awards.get("previous_winner") else 0
        f["competition"] = comp
        yrs = facts.get("experience_years") or 0
        f["experience_requirements"] = 9 if not yrs else 7 if yrs <= 2 else 4 if yrs <= 5 else 2
        if facts.get("past_performance"):
            f["experience_requirements"] -= 1.5
        n_lic = len(facts.get("licenses") or [])
        f["licensing_requirements"] = 9 - 2.5 * n_lic
        f["insurance_requirements"] = 9 if not gl else 7 if gl <= 1_000_000 else 5 if gl <= 2_000_000 else 3
        f["bonding"] = 9 if not bonds else (5 if float(self.company.get("bonding_capacity") or 0) > 0 else 2.5)
        f["time_to_deadline"] = 6 if days is None else 10 if days > 21 else 8 if days > 14 else 5 if days > 7 else 2
        home = self.company.get("home_market", "dfw")
        f["geographic_fit"] = 10 if opp.market == home else 7 if opp.market in self.config.markets \
            else 5 if opp.market == "statewide" else 3
        sites = facts.get("site_count") or 0
        cx = 8 - (2 if sites >= 15 else 0) - (1.5 if facts.get("coverage_24_7") else 0) \
            - (1 if facts.get("qasp") else 0) - (1 if len(opp.doc_text or "") > 25000 else 0)
        f["complexity"] = cx
        f["margin_potential"] = {"low": 4, "medium": 6.5, "high": 8.5}.get(cat.get("margin", "medium"), 6)
        f["past_award_info"] = 8 if (awards.get("exact") or awards.get("local_matches")) else \
            6.5 if awards.get("similar") else 4
        realism = 6.0
        if opp.service_category in self.company.get("existing_capabilities", []):
            realism += 2.5
        realism -= 1.5 * (red.get("major_count") or 0) + 4 * (red.get("kill_count") or 0)
        f["small_llc_realism"] = realism
        return {k: round(max(0.0, min(10.0, v)), 1) for k, v in f.items()}

    def score(self, opp: Opportunity) -> tuple[float, dict]:
        f = self.factors(opp)
        total_w = sum(float(self.weights.get(k, 1)) for k in f)
        raw = sum(f[k] * float(self.weights.get(k, 1)) for k in f) / (total_w or 1)
        score = round(raw * 10, 1)
        red = opp.analysis.get("red_team", {})
        score = round(score - 3 * (red.get("major_count") or 0), 1)   # each unresolved major risk costs extra
        if red.get("kill_count"):
            score = min(score, float(self.thresholds.get("review", 52)) - 0.1)
        return score, f

    def categorize(self, opp: Opportunity, score: float) -> str:
        """Bucket + a plain-English note (opp.analysis["category_note"]) when a rule held it back."""
        top = float(self.thresholds.get("top", 72))
        review = float(self.thresholds.get("review", 55))
        watch = float(self.thresholds.get("watch", 42))
        a = opp.analysis
        red = a.get("red_team", {})
        sub = a.get("subcontracting", {})
        days = days_until(opp.response_deadline)
        a.pop("category_note", None)
        early = opp.notice_type in ("sources_sought", "presolicitation", "special", "forecast")
        if early:
            return "watch" if score >= watch - 5 else "filtered"
        blocker = next((f for f in red.get("findings", []) if f.get("severity") == "kill"
                        and f.get("area") in BLOCKING_AREAS), None)
        if blocker:
            a["category_note"] = blocker["message"]
            return "filtered"
        if red.get("verdict") == "fails":
            return "review" if score >= review + 8 else "watch" if score >= watch else "filtered"
        if score >= top:
            held = self._top_blocker(opp, red, sub, days)
            if not held:
                return "top"
            a["category_note"] = held
            return "review"
        if score >= review:
            return "review"
        if score >= watch:
            return "watch"
        return "filtered"

    def _top_blocker(self, opp: Opportunity, red: dict, sub: dict, days) -> str:
        """Why a high-scoring item still isn't a Top Opportunity ('' = nothing holds it back)."""
        if days is not None and days < 5:
            return "Scores well, but there are fewer than 5 days to prepare."
        if (red.get("major_count") or 0) > 1:
            return "Scores well, but has more than one major risk to resolve first."
        chars = (opp.analysis.get("requirements") or {}).get("reviewed_chars", len(opp.text()))
        if chars < 600:
            return "Scores well on what's listed, but only a short listing was available — read the full packet first."
        can_self_perform = opp.service_category in (self.company.get("existing_capabilities") or [])
        if sub.get("status") == "UNKNOWN" and not can_self_perform:
            return "Scores well, but the text doesn't say whether subcontracting is allowed — ask the buyer first."
        return ""

    @staticmethod
    def weakest(factors: dict, n: int = 2) -> list[str]:
        return [FACTOR_LABELS[k] for k, _ in sorted(factors.items(), key=lambda kv: kv[1])[:n]]

    @staticmethod
    def strongest(factors: dict, n: int = 3) -> list[str]:
        return [FACTOR_LABELS[k] for k, _ in sorted(factors.items(), key=lambda kv: -kv[1])[:n]]


def deadline_flag(opp: Opportunity) -> tuple[str, str]:
    """(emoji, label) — never hides a deadline."""
    days = days_until(opp.response_deadline)
    if days is None:
        return "⚪", "No deadline listed"
    if days < 0:
        return "⚫", "Expired"
    whole = math.floor(days)
    label = "Due today" if whole == 0 else f"{whole} day{'s' if whole != 1 else ''} left"
    if days <= 7:
        return "🔴", label
    if days <= 14:
        return "🟠", label
    return "🟢", label
