"""AGENT 8 — Red Team.

Asks one question: "What could make this a bad contract?" Each finding is rated
  kill   — a disqualifier for your model as written (cannot be a Top Opportunity)
  major  — could sink profit or eligibility; needs an answer before bidding
  minor  — worth knowing, manageable
Only opportunities that survive (no kill findings) can reach the TOP list.
"""
from __future__ import annotations

import re

from core.config import Config
from core.extract import days_until, find_first, fmt_money, parse_date
from core.vehicles import FAIR_OPPORTUNITY
from core.llm import LLM
from core.opportunity import Opportunity


class RedTeamAgent:
    def __init__(self, config: Config, llm: LLM | None = None):
        self.config = config
        self.llm = llm
        self.company = config.company

    def run(self, opp: Opportunity) -> dict:
        a = opp.analysis
        req = a.get("requirements", {})
        facts = req.get("facts", {})
        sub = a.get("subcontracting", {})
        awards = a.get("award_history", {})
        text = opp.text()
        findings: list[dict] = []

        def add(sev, area, msg, evidence=""):
            findings.append({"severity": sev, "area": area, "message": msg, "evidence": evidence})

        # Subcontracting
        status = sub.get("status")
        if status == "RESTRICTED":
            if opp.service_category in (self.company.get("existing_capabilities") or []):
                add("major", "Subcontracting", "Self-performance required — only workable with your own crew and "
                                               "payroll (you already do this kind of work).")
            else:
                add("kill", "Subcontracting", "Self-performance required — your manage-and-subcontract model doesn't fit as written.")
        elif status == "LIMITED":
            add("major", "Subcontracting", "Limitations on subcontracting: you'd need your own employees or small "
                                            "'similarly situated' subs to perform the required share.")
        elif status == "UNKNOWN":
            add("minor", "Subcontracting", "Subcontracting isn't addressed in the text reviewed — confirm before pricing.")

        # Deadline & mandatory events
        days = days_until(opp.response_deadline)
        if days is None:
            add("minor", "Deadline", "No response deadline listed — confirm the due date on the official page.")
        elif days <= 7:
            add("major", "Deadline", f"Only {days:.0f} days to prepare a bid.")
        visit = facts.get("mandatory_visit_date")
        if visit:
            vd = parse_date(visit)
            vdays = days_until(vd)
            if vdays is not None and vdays < 0:
                add("kill", "Mandatory meeting", f"Mandatory pre-bid/site visit on {visit} has already happened — "
                                                  "non-attendees are usually disqualified.")
            elif vdays is not None:
                add("major", "Mandatory meeting", f"Mandatory pre-bid/site visit on {visit} — someone must attend.")

        # Money: insurance, bonding, cash flow
        gl = facts.get("general_liability")
        have_gl = float((self.company.get("insurance_on_hand") or {}).get("general_liability") or 0)
        if gl and gl > have_gl:
            add("minor" if gl <= 2_000_000 else "major", "Insurance",
                f"Needs {fmt_money(gl)} general liability (you carry {fmt_money(have_gl) if have_gl else 'none on file'}). "
                "Get a quote before bidding.")
        if facts.get("auto_liability") and facts["auto_liability"] >= 1_000_000:
            add("minor", "Insurance", f"Auto liability {fmt_money(facts['auto_liability'])} required.")
        bonds = bool(facts.get("perf_payment_bond") or facts.get("bid_bond"))
        if bonds:
            cap = float(self.company.get("bonding_capacity") or 0)
            pct = facts.get("bond_percent")
            if cap <= 0:
                add("major", "Bonding", "Bonds are required and you have no bonding capacity on file. A surety "
                                        "relationship takes time — start early or pass." + (f" ({pct}% bond)" if pct else ""))
            elif opp.estimated_value and opp.estimated_value > cap:
                add("major", "Bonding", f"Contract value {fmt_money(opp.estimated_value)} exceeds your bonding capacity {fmt_money(cap)}.")
        m, ev = find_first([r"net\s*(45|60|90)", r"within (?:forty-five|sixty|ninety) \(?\d*\)? days"], text)
        if m:
            add("major", "Cash flow", "Slow payment terms — you'll float subcontractor pay for 45–90 days.", ev)
        if opp.service_category == "staffing":
            add("major", "Cash flow", "Staffing contracts mean carrying payroll before the agency pays you.")

        # Labor & performance
        if facts.get("sca"):
            add("minor", "Labor", "Service Contract Act wage rates apply — your subs must pay at least the wage "
                                   "determination plus fringe; price accordingly.")
        if facts.get("liquidated_damages"):
            add("major", "Penalties", "Liquidated damages can eat margin — read how they're calculated.")
        if facts.get("coverage_24_7"):
            add("major", "Staffing", "24/7 or emergency response coverage — hard to guarantee through subs.")
        sites = facts.get("site_count")
        if sites and sites >= 15:
            add("major" if sites >= 40 else "minor", "Travel/logistics", f"{sites} sites to cover — supervision and drive time add cost.")
        years = facts.get("experience_years")
        if years and years > int(self.company.get("years_in_business") or 0):
            add("major", "Experience", f"Requires {years}+ years of experience; your LLC shows "
                                       f"{self.company.get('years_in_business', 0)}. Ask whether sub or key-personnel experience counts.")
        lic = facts.get("licenses") or []
        if lic:
            add("minor", "Licensing", f"License(s) required: {', '.join(lic)}. A licensed sub may satisfy it — confirm "
                                      "whether the prime must hold it.")
        if re.search(r"\bequipment\b[^.]{0,60}\b(?:furnish|provide|supply)\b|contractor shall (?:furnish|provide) all equipment",
                     text, re.I) and opp.service_category in ("tree_services", "debris_removal", "parking_lot",
                                                              "equipment_rental", "warehouse", "transportation"):
            add("minor", "Equipment", "Contractor furnishes all equipment — your sub must bring it; confirm availability.")

        # Competition & incumbency
        winners = awards.get("distinct_winners") or []
        if awards.get("previous_winner"):
            add("minor", "Incumbent", f"Incumbent/previous winner: {awards['previous_winner']}. Incumbents win re-competes often.")
        elif len(winners) >= 1 and awards.get("number_of_similar_awards", 0) >= 6:
            add("minor", "Competition", "Active market with repeat winners — price sharply.")
        if not opp.set_aside and opp.jurisdiction == "federal":
            add("minor", "Competition", "Full-and-open (not set aside) — you'd compete with large firms.")
        if opp.estimated_value is None:
            add("minor", "Value", "No value listed — ask for estimated quantities or the prior contract amount.")

        # Contract vehicles (IDIQ / BPA / term / co-op): winning a seat isn't winning work
        vehicle = a.get("vehicle") or {}
        vtype = vehicle.get("type")
        if vtype in ("matoc", "idiq", "bpa"):
            if vtype == "matoc" or (vehicle.get("awards") or 1) > 1:
                add("minor", "Contract vehicle", "Multiple-award vehicle: winning gets you a seat, not guaranteed work. "
                                                 "Each task order is competed again among the holders (fair opportunity).",
                    FAIR_OPPORTUNITY)
            mg = vehicle.get("min_guarantee")
            if mg is not None and mg < 10_000:
                add("minor", "Contract vehicle", f"Only {fmt_money(mg)} is guaranteed; everything else depends on "
                                                 "winning orders.")
            if (vehicle.get("ceiling") or 0) >= 25_000_000 and not opp.set_aside:
                add("major", "Competition", f"{fmt_money(vehicle['ceiling'])} full-and-open vehicle — large, established "
                                            "firms usually compete for these.")
        elif vtype in ("term", "jobs_order"):
            add("minor", "Contract vehicle", "As-needed / term contract: quantities are estimates, not a promise of work.")
        elif vtype == "cooperative":
            add("minor", "Contract vehicle", "Co-op award makes you eligible to sell to member agencies; you still have "
                                             "to market to them to get orders.")

        # Notice stage
        if opp.notice_type in ("sources_sought", "presolicitation", "special", "forecast"):
            add("minor", "Stage", "Not biddable yet — this is an early notice. Responding can shape the set-aside and "
                                  "gets you on the buyer's radar.")

        self._llm_pass(opp, text, findings)
        order = {"kill": 0, "major": 1, "minor": 2}
        findings.sort(key=lambda f: order[f["severity"]])
        kills = [f for f in findings if f["severity"] == "kill"]
        majors = [f for f in findings if f["severity"] == "major"]
        verdict = "fails" if kills else "survives_with_risks" if majors else "survives"
        biggest = (findings[0]["message"] if findings else
                   "No major risks found in the text reviewed — the full packet may still hide some.")
        return {"verdict": verdict, "findings": findings, "biggest_risk": biggest,
                "kill_count": len(kills), "major_count": len(majors)}

    def _llm_pass(self, opp: Opportunity, text: str, findings: list):
        if not self.llm or not self.llm.enabled or len(text) < 1500:
            return
        task = ("Act as a skeptical bid/no-bid reviewer for a small LLC that would manage subcontractors. "
                "From the text only, list hidden costs or risks not already listed below. Return JSON "
                "{\"risks\": [{\"severity\": \"major|minor\", \"area\": \"...\", \"message\": \"under 25 words\"}]} "
                "with at most 4 items. Already listed: " + "; ".join(f["message"][:60] for f in findings))
        result = self.llm.json(task, text[: int(self.config.get("llm.max_chars_per_document", 30000))], max_tokens=700)
        for r in (result or {}).get("risks", [])[:4]:
            sev = r.get("severity") if r.get("severity") in ("major", "minor") else "minor"
            findings.append({"severity": sev, "area": str(r.get("area", "AI review"))[:40],
                             "message": str(r.get("message", ""))[:220], "evidence": "AI review of the solicitation text"})
