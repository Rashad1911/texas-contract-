"""AGENT 6 — Bid Strategy.

For each serious opportunity:
  1 what the government wants        6 major costs
  2 what your LLC would need         7 major risks
  3 potential subcontractor type     8 questions that need answers
  4 estimated startup cost           9 documents needed
  5 potential contract revenue      10 suggested next steps
Plus the short fields used in the email: why it made the list, biggest risk, what to do (3 steps).
Cost figures are rough planning ranges, clearly labeled — not quotes.
"""
from __future__ import annotations

from core.config import Config
from core import vehicles as veh
from core.extract import days_until, fmt_money
from core.llm import LLM
from core.opportunity import Opportunity

from .scoring import FACTOR_LABELS

STARTUP_BANDS = {
    "low": ("LOW", "≈ $1.5K–$6K: registrations, insurance down payment, bid prep, first-month float"),
    "medium": ("MEDIUM", "≈ $6K–$25K: insurance, supervision, sub mobilization, 1–2 months of float"),
    "high": ("HIGH", "≈ $25K+: equipment or facilities, larger insurance, payroll float"),
}


class BidStrategyAgent:
    def __init__(self, config: Config, llm: LLM | None = None):
        self.config = config
        self.llm = llm
        self.company = config.company

    def run(self, opp: Opportunity, factors: dict | None = None) -> dict:
        a = opp.analysis
        cat = self.config.service_categories().get(opp.service_category, {})
        req = a.get("requirements", {})
        facts = req.get("facts", {})
        sub = a.get("subcontracting", {})
        red = a.get("red_team", {})
        awards = a.get("award_history", {})
        days = days_until(opp.response_deadline)

        # 1 what the government wants
        wants = req.get("summary") or _first_sentences(opp.description, 2) or opp.title

        # 2 what the LLC needs
        needs = [m["item"] for m in req.get("must_have", [])][:7]

        # 4 startup cost
        level = cat.get("startup", "medium")
        bonds = bool(facts.get("perf_payment_bond") or facts.get("bid_bond"))
        if bonds and level == "low":
            level = "medium"
        startup_label, startup_detail = STARTUP_BANDS[level]
        cost_notes = [startup_detail]
        if bonds:
            cost_notes.append("Performance bond premiums commonly run ~1–3% of contract value (depends on credit).")
        value = opp.estimated_value or awards.get("previous_award") or awards.get("median_similar_award")
        if value:
            monthly = value / 12
            cost_notes.append(f"Cash float: if you pay subs monthly and the agency pays net 30, plan to carry "
                              f"≈ {fmt_money(monthly * 1.5)}–{fmt_money(monthly * 2)} (assumes a 12-month value of "
                              f"{fmt_money(value)}; adjust if the total covers several years).")

        # 5 revenue
        vehicle = a.get("vehicle") or {}
        vlabel = veh.value_label(vehicle, fmt_money) if vehicle else ""
        if vehicle and vlabel:
            revenue = vlabel + ". Revenue comes from the task orders you win after award."
        elif opp.estimated_value:
            revenue = f"{fmt_money(opp.estimated_value)} ({opp.value_basis or 'listed value'})"
        elif awards.get("previous_award"):
            revenue = f"≈ {fmt_money(awards['previous_award'])} based on the previous contract"
        elif awards.get("median_similar_award"):
            revenue = f"≈ {fmt_money(awards['median_similar_award'])} median of similar Texas awards (rough)"
        else:
            revenue = "Not listed — ask for estimated quantities or the prior contract amount."

        # 6 costs
        costs = ["Subcontractor labor (largest cost)", "Insurance premiums", "Supervision / quality checks",
                 "Supplies & consumables (if not provided by sub)"]
        if facts.get("sca"):
            costs.insert(1, "Service Contract Act wage + fringe floors")
        if bonds:
            costs.append("Bond premiums")
        if (facts.get("site_count") or 0) >= 10:
            costs.append("Travel between sites")

        # 7 risks
        risks = [f["message"] for f in red.get("findings", []) if f["severity"] in ("kill", "major")][:5] or \
            [red.get("biggest_risk", "")]

        # 8 questions
        questions = list(sub.get("questions", []))
        if not opp.estimated_value:
            questions.append("What is the estimated annual value or the prior contract amount?")
        if not awards.get("previous_winner"):
            questions.append("Who is the incumbent and when does the current contract expire?")
        questions.append("How are quantities / service frequencies measured and invoiced?")
        if vehicle.get("type") in ("matoc", "idiq", "bpa", "satoc"):
            questions[:0] = ["How many awards do you plan to make, and what is the minimum guarantee per award?",
                             "What task order volume do you expect in the first year, and how will orders be competed?"]
            if vehicle.get("on_ramp") is False:
                questions.append("Will the contract have on-ramps to add holders later?")
        if facts.get("liquidated_damages"):
            questions.append("How are liquidated damages or deductions calculated?")
        if not facts.get("general_liability"):
            questions.append("What insurance types and limits are required?")

        # 9 documents
        docs = ["Capability statement (1 page)", "W-9", "Certificate of insurance (or broker quote letter)",
                "Completed pricing form / bid sheet", "References / past performance (yours or key personnel)"]
        if opp.jurisdiction == "federal":
            docs += ["SAM.gov registration (UEI) and representations & certifications",
                     "Signed SF-1449/SF-33 and all amendments (as the solicitation requires)"]
        else:
            docs += ["Agency bid forms and signed addenda",
                     "Texas forms the packet asks for (commonly Conflict of Interest Questionnaire CIQ, "
                     "Texas Ethics Commission Form 1295 for council/court-approved contracts, statutory verifications) — confirm"]
        if facts.get("hub_plan"):
            docs.append("HUB Subcontracting Plan (state form)")
        if facts.get("mwbe_goals"):
            docs.append("M/WBE / SBE participation forms and sub commitment letters")

        # 10 next steps (sequence)
        steps = self._steps(opp, facts, sub, days)
        if vehicle.get("type") in ("matoc", "idiq", "bpa", "cooperative"):
            steps.insert(len(steps) - 1, "Plan how you'll win orders after award: which buyers to meet, your "
                                         "standard rates, and a sub who can mobilize fast.")

        # outsource level for the email
        outsource = "HIGH" if sub.get("status") in ("SUPPORTED",) and cat.get("outsource") == "high" else \
            "MEDIUM" if sub.get("status") in ("SUPPORTED", "APPROVAL_REQUIRED", "UNKNOWN") else "LOW"

        why = self._why(opp, factors or {}, sub, days, cat)
        result = {
            "what_government_wants": wants,
            "what_llc_needs": needs,
            "subcontractor_type": sub.get("subcontractor_type") or cat.get("subcontractor", ""),
            "startup_level": startup_label,
            "startup_cost": cost_notes,
            "revenue": revenue,
            "major_costs": costs,
            "major_risks": [r for r in risks if r],
            "questions": questions[:7],
            "documents_needed": docs,
            "next_steps": steps,
            "outsource_level": outsource,
            "why_it_made_the_list": why,
            "biggest_risk": red.get("biggest_risk", ""),
            "todo_top3": steps[:3],
            "disclaimer": "Planning estimates only. Have a CPA review pricing and an attorney or procurement "
                          "professional review contract terms before you sign.",
        }
        self._llm_polish(opp, result)
        return result

    def _steps(self, opp, facts, sub, days) -> list[str]:
        steps = []
        src = self.config.source(opp.source)
        if opp.jurisdiction == "federal":
            if not self.company.get("sam_registered"):
                steps.append("Start (or finish) your LLC's SAM.gov entity registration today — it's required before award.")
            steps.append("Download every attachment and amendment from the SAM.gov notice.")
        else:
            portal = src.get("submit_url") or src.get("url") or "the agency portal"
            steps.append(f"Register (free) on the bidding portal and download the full packet: {portal}")
        if facts.get("mandatory_visit_date"):
            steps.append(f"Put the mandatory pre-bid/site visit on your calendar ({facts['mandatory_visit_date']}).")
        steps.append("Get 2–3 subcontractor quotes against the scope (share the specs, not your pricing).")
        if sub.get("status") in ("UNKNOWN", "APPROVAL_REQUIRED", "LIMITED"):
            steps.append("Email the contracting officer/buyer your subcontracting question before the Q&A deadline.")
        if facts.get("general_liability") or facts.get("perf_payment_bond") or facts.get("bid_bond"):
            steps.append("Ask your insurance broker (and a surety, if bonds apply) for quotes at the required limits.")
        steps.append("Build the price sheet: sub cost + supervision + insurance + overhead + margin.")
        steps.append("Run a final bid/no-bid check, then submit before the deadline with all signed addenda.")
        return steps

    def _why(self, opp, factors, sub, days, cat) -> str:
        label = cat.get("label", opp.service_category.replace("_", " "))
        where = self.config.market_label(opp.market)
        parts = [f"{label} work in {where}"]
        if opp.service_category in self.company.get("existing_capabilities", []):
            parts[0] += ", which matches your existing capability"
        sentence1 = parts[0] + "."
        good = [FACTOR_LABELS[k].lower() for k, v in sorted(factors.items(), key=lambda kv: -kv[1])[:3] if v >= 7]
        sentence2 = f"Strongest factors: {', '.join(good)}." if good else ""
        sub_line = {"SUPPORTED": "The solicitation expressly contemplates subcontractors.",
                    "APPROVAL_REQUIRED": "Subcontractors are allowed with the buyer's approval.",
                    "UNKNOWN": "Subcontracting isn't addressed yet — needs a question to the buyer.",
                    "LIMITED": "Subcontracting is capped, so part of the work must be yours or a small sub's.",
                    "RESTRICTED": "Self-performance is required."}.get(sub.get("status"), "")
        time_line = f" {int(days)} days to prepare." if days is not None and days > 0 else ""
        return " ".join(x for x in [sentence1, sentence2, sub_line] if x) + time_line

    def _llm_polish(self, opp: Opportunity, result: dict):
        if not self.llm or not self.llm.enabled:
            return
        task = ("Rewrite two fields for a busy owner reading on a phone. Keep every fact; add none. "
                "Return JSON {\"why_it_made_the_list\": \"2-3 short sentences\", \"biggest_risk\": \"1-2 short sentences\"}.")
        payload = f"TITLE: {opp.title}\nWHY: {result['why_it_made_the_list']}\nRISK: {result['biggest_risk']}\n" \
                  f"RISKS: {'; '.join(result['major_risks'])}"
        out = self.llm.json(task, payload, max_tokens=300)
        if out:
            result["why_it_made_the_list"] = str(out.get("why_it_made_the_list") or result["why_it_made_the_list"])[:500]
            result["biggest_risk"] = str(out.get("biggest_risk") or result["biggest_risk"])[:300]


def _first_sentences(text: str, n: int) -> str:
    import re
    sentences = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return " ".join(sentences[:n])[:500]
