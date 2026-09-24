"""AGENT 5 — Subcontracting.

Decides whether a manage-and-subcontract model looks compatible with the solicitation — and never
says subcontracting is permitted unless the text supports it.

Statuses
  SUPPORTED          the text expressly contemplates subcontractors (e.g. M/WBE subcontracting goals, HUB plan)
  LIMITED            a percentage cap applies (e.g. FAR 52.219-14 on small-business set-asides)
  APPROVAL_REQUIRED  subcontractors need the buyer's written consent
  RESTRICTED         self-performance required / subcontracting prohibited
  UNKNOWN            the text reviewed doesn't say — ask the contracting officer

Key federal rule (verify against the solicitation): on small-business set-asides for services, FAR
52.219-14 (Limitations on Subcontracting) says the prime may not pay more than 50% of the amount paid
by the government to firms that are not "similarly situated" (i.e. not also small under the NAICS).
Work done by similarly situated small subcontractors counts toward the prime's share.
Source: https://www.acquisition.gov/far/52.219-14
"""
from __future__ import annotations

from connectors.sam import SET_ASIDES
from core.config import Config
from core.extract import find_first
from core.llm import LLM
from core.opportunity import Opportunity

RESTRICT = [r"self[- ]perform(?:ance)?[^.]{0,80}", r"shall not (?:sub-?contract|assign)[^.]{0,80}",
            r"sub-?contracting (?:is|will) not (?:be )?(?:permitted|allowed)", r"no sub-?contract(?:ing|ors) (?:is |are |will be )?(?:permitted|allowed)"]
LIMIT = [r"52\.219-14", r"limitations? on sub-?contracting", r"(?:50|fifty)\s*(?:%|percent)[^.]{0,80}(?:personnel|own employees|cost of contract performance)"]
APPROVAL = [r"(?:prior )?(?:written )?(?:consent|approval) (?:of|from) the (?:contracting officer|city|county|owner|agency)[^.]{0,60}sub-?contract",
            r"sub-?contract[^.]{0,80}(?:prior )?(?:written )?(?:consent|approval)", r"52\.244-2"]
SUPPORT = [r"hub subcontracting plan", r"m/?wbe (?:subcontract|goal|participation)", r"mwbe goal",
           r"sbe (?:subcontract|goal|participation)", r"subcontracting (?:goal|opportunit|plan)",
           r"list (?:all|any|each) (?:proposed )?sub-?contractors", r"sub-?contractors? (?:shall|must|will) (?:be|comply)",
           r"prime contractor (?:and|or) (?:its|any) sub-?contractors?", r"sbeda"]


class SubcontractingAgent:
    def __init__(self, config: Config, llm: LLM | None = None):
        self.config = config
        self.llm = llm
        self.regs = config.regulations

    def run(self, opp: Opportunity) -> dict:
        text = opp.text()
        cat = self.config.service_categories().get(opp.service_category, {})
        sub_type = cat.get("subcontractor", "Qualified subcontractor for this service")
        findings = []

        restrict_m, restrict_ev = find_first(RESTRICT, text)
        limit_m, limit_ev = find_first(LIMIT, text)
        approval_m, approval_ev = find_first(APPROVAL, text)
        support_m, support_ev = find_first(SUPPORT, text)
        code = (opp.set_aside_code or "").upper()
        sb_set_aside = code in SET_ASIDES or "small business" in (opp.set_aside or "").lower()

        if restrict_m:
            status = "RESTRICTED"
            explain = "The solicitation text requires the prime to self-perform or bars subcontracting."
            findings.append({"finding": "Self-performance / no-subcontracting language", "evidence": restrict_ev})
        elif limit_m or (opp.jurisdiction == "federal" and sb_set_aside):
            status = "LIMITED"
            if limit_m:
                findings.append({"finding": "Limitations on Subcontracting language found", "evidence": limit_ev})
            else:
                findings.append({"finding": f"Small-business set-aside ({opp.set_aside or code})",
                                 "evidence": "FAR 19.505 / 52.219-14 normally apply to set-asides — confirm the clause in Section I."})
            explain = ("Likely capped: on a small-business set-aside for services, no more than 50% of what the "
                       "government pays you can go to subcontractors that are not also small under this NAICS. "
                       "Small ('similarly situated') subcontractors count toward your share. Your LLC would need its "
                       "own employees or small subs doing the work — a pure pass-through model likely won't comply.")
        elif approval_m:
            status = "APPROVAL_REQUIRED"
            explain = "Subcontractors appear to need the buyer's written approval before they start."
            findings.append({"finding": "Consent-to-subcontract language", "evidence": approval_ev})
        elif support_m:
            status = "SUPPORTED"
            explain = ("The solicitation text contemplates subcontractors (for example participation goals or a "
                       "subcontracting plan). That supports a manage-and-subcontract model, but read the full terms.")
            findings.append({"finding": "Text contemplates subcontractors", "evidence": support_ev})
        else:
            status = "UNKNOWN"
            explain = ("The text reviewed doesn't address subcontracting. Don't assume it's allowed — ask the "
                       "contracting officer or buyer before pricing a subcontracted approach.")

        if approval_m and status != "APPROVAL_REQUIRED":
            findings.append({"finding": "Subcontractors may also need written approval", "evidence": approval_ev})
        if support_m and status in ("LIMITED", "APPROVAL_REQUIRED"):
            findings.append({"finding": "Text also references subcontractors/participation goals", "evidence": support_ev})

        model = {
            "SUPPORTED": f"{opp.agency or 'Agency'} → your LLC (prime: management, QC, billing) → {sub_type}",
            "APPROVAL_REQUIRED": f"{opp.agency or 'Agency'} → your LLC (prime) → {sub_type} (after written approval)",
            "LIMITED": f"{opp.agency or 'Agency'} → your LLC (prime, must perform its required share with its own "
                       f"employees or similarly situated small subs) → {sub_type}",
            "UNKNOWN": f"Possible only if confirmed: {opp.agency or 'Agency'} → your LLC → {sub_type}",
            "RESTRICTED": "Not compatible with a subcontracting model as written.",
        }[status]

        questions = []
        if status in ("UNKNOWN", "APPROVAL_REQUIRED"):
            questions.append("Does the agency permit the prime contractor to subcontract the service work? "
                             "Is written approval of subcontractors required?")
        if status == "LIMITED":
            questions.append("Confirm FAR 52.219-14 applies and which NAICS size standard subcontractors must meet "
                             "to count as similarly situated.")
        if opp.jurisdiction in ("city", "county", "state", "cooperative"):
            questions.append("Are there M/WBE, SBE, or HUB subcontracting goals, and which certifications count?")

        result = {
            "status": status,
            "explanation": explain,
            "model": model,
            "subcontractor_type": sub_type,
            "findings": findings,
            "questions": questions,
            "compatible": status in ("SUPPORTED", "APPROVAL_REQUIRED"),
            "references": [self.regs.get("far_52_219_14"), self.regs.get("far_19_505")] if status == "LIMITED" else [],
            "disclaimer": "Subcontracting conclusions are preliminary — confirm with the contracting officer and, "
                          "for anything close, a procurement attorney.",
        }
        return result
