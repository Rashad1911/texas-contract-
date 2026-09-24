"""AGENT 4 — Requirements.

Reads the solicitation text (listing + description + attachment text) and builds a short checklist:
MUST HAVE / NICE TO HAVE / POTENTIAL PROBLEM / TO CONFIRM. Every rule-based item keeps the exact
snippet it came from, so nothing is asserted without evidence. The LLM (if configured) only adds a
plain-English summary and anything the rules missed — with the same "text only" guardrail.
"""
from __future__ import annotations

import re

from core.config import Config
from core.extract import find_first, fmt_money, money_near, snippet
from core.llm import LLM
from core.opportunity import Opportunity

RULES = [
    # (bucket, fact_key, label, patterns)
    ("must", "mandatory_visit", "Mandatory pre-bid meeting or site visit", [r"mandatory (?:pre[- ]?(?:bid|proposal|submittal)|site visit|walk[- ]?through)[^.]{0,80}"]),
    ("must", "sca", "Pay Service Contract Act wages (DOL wage determination attached)", [r"wage determination", r"service contract (?:labor standards|act)", r"52\.222-41"]),
    ("must", "e_verify", "E-Verify enrollment", [r"e-verify", r"52\.222-54"]),
    ("must", "background_checks", "Background checks for workers", [r"background (?:check|investigation)s?", r"criminal history check"]),
    ("must", "hub_plan", "HUB Subcontracting Plan (Texas state contracts)", [r"hub subcontracting plan", r"\bHSP\b"]),
    ("must", "mwbe_goals", "Meet M/WBE or SBE participation goals", [r"m/?wbe (?:goal|participation)", r"mwbe goal", r"sbe (?:goal|participation)", r"sbeda", r"business inclusion"]),
    ("must", "past_performance", "Submit past-performance references", [r"past performance", r"(?:three|3|five|5) (?:\(\d\) )?references"]),
    ("must", "key_personnel", "Named on-site supervisor / project manager", [r"(?:on[- ]site|full[- ]time) (?:supervisor|project manager|site manager)", r"key personnel"]),
    ("must", "tx_sos", "Texas Secretary of State business registration", [r"secretary of state"]),
    ("nice", "local_preference", "Local / small-business preference points available", [r"local preference", r"hire houston first", r"veteran[- ]owned small business preference", r"small business preference"]),
    ("nice", "prebid_optional", "Pre-bid meeting (optional) — worth attending", [r"(?:optional|non[- ]mandatory) pre[- ]?(?:bid|proposal|submittal)", r"pre[- ]?(?:bid|proposal|submittal) conference"]),
    ("nice", "green_products", "Green / sustainable products preferred", [r"green seal", r"environmentally preferr", r"sustainable (?:products|cleaning)"]),
    ("nice", "questions_period", "Questions period — you can ask the buyer", [r"questions? (?:are )?due", r"deadline for questions", r"questions (?:must|shall) be submitted"]),
    ("problem", "liquidated_damages", "Liquidated damages for missed performance", [r"liquidated damages"]),
    ("problem", "perf_payment_bond", "Performance / payment bond required", [r"performance bond", r"payment bond"]),
    ("problem", "bid_bond", "Bid bond / bid guarantee required", [r"bid bond", r"bid guarantee", r"bid security"]),
    ("problem", "coverage_24_7", "24/7 or emergency response coverage", [r"24/7", r"24 hours a day", r"twenty[- ]four \(24\) hours", r"emergency (?:call[- ]?out|response) within"]),
    ("problem", "multi_site", "Multiple sites / locations", [r"\b(\d{2,3})\s+(?:sites|locations|facilities|buildings|branches)\b"]),
    ("problem", "self_perform", "Self-performance language", [r"self[- ]perform", r"shall not (?:sub-?contract|assign)", r"no sub-?contracting"]),
    ("problem", "qasp", "Performance measured by inspections / QASP", [r"quality assurance surveillance plan", r"\bQASP\b", r"deductions? (?:for|from) (?:non-?performance|unsatisfactory)"]),
]

LICENSE_PATTERNS = [
    (r"structural pest control|tda license|texas department of agriculture", "TDA Structural Pest Control license"),
    (r"private security|dps license|class b license|psb", "Texas DPS Private Security license"),
    (r"tdlr|air conditioning and refrigeration|acr license", "TDLR license"),
    (r"master plumber|tsbpe", "Texas plumbing license (TSBPE)"),
    (r"electrical contractor license", "Electrical contractor license"),
    (r"certified arborist|isa certified", "ISA Certified Arborist"),
    (r"naid aaa|naid certified|i-sigma", "NAID AAA certification"),
    (r"tceq", "TCEQ registration/license"),
    (r"pesticide applicator", "Pesticide applicator license"),
    (r"cdl|commercial driver", "Commercial driver's licenses (CDL)"),
]


class RequirementsAgent:
    def __init__(self, config: Config, llm: LLM | None = None):
        self.config = config
        self.llm = llm
        self.company = config.company

    def run(self, opp: Opportunity) -> dict:
        text = opp.text()
        req = {"must_have": [], "nice_to_have": [], "potential_problems": [], "to_confirm": [],
               "facts": {}, "reviewed_chars": len(text), "summary": ""}
        buckets = {"must": "must_have", "nice": "nice_to_have", "problem": "potential_problems"}
        for bucket, key, label, patterns in RULES:
            match, evidence = find_first(patterns, text)
            if match:
                req[buckets[bucket]].append({"item": label, "evidence": evidence})
                req["facts"][key] = True

        f = req["facts"]
        gl = money_near(text, ["general liability", "commercial general liability", "cgl"])
        if gl:
            amount, ev = max(gl, key=lambda x: x[0])
            f["general_liability"] = amount
            req["must_have"].append({"item": f"General liability insurance ≥ {fmt_money(amount)}", "evidence": ev})
        auto = money_near(text, ["automobile liability", "auto liability", "business auto", "automobile insurance"])
        if auto:
            amount, ev = max(auto, key=lambda x: x[0])
            f["auto_liability"] = amount
            req["must_have"].append({"item": f"Auto liability insurance ≥ {fmt_money(amount)}", "evidence": ev})
        if re.search(r"workers'? comp", text, re.I):
            f["workers_comp"] = True
            m, ev = find_first([r"workers'? comp\w*"], text)
            req["must_have"].append({"item": "Workers' compensation coverage", "evidence": ev})
        bond_pct = re.search(r"(\d{1,3})\s*%\s*(?:of the (?:total )?(?:bid|contract)[^.]{0,30})?(?:performance|payment|bid) bond"
                             r"|(?:performance|payment|bid) bond[^.]{0,60}?(\d{1,3})\s*%", text, re.I)
        if bond_pct:
            f["bond_percent"] = int(bond_pct.group(1) or bond_pct.group(2))

        years = re.search(r"(?:minimum of|at least|no less than)?\s*(\w+|\d+)\s*(?:\(\d+\)\s*)?(?:\+\s*)?years?\s+(?:of\s+)?"
                          r"(?:continuous\s+|documented\s+|relevant\s+|verifiable\s+)?(?:experience|in business|providing)",
                          text, re.I)
        if years:
            n = _num(years.group(1))
            if n:
                f["experience_years"] = n
                req["must_have"].append({"item": f"{n}+ years of relevant experience",
                                         "evidence": snippet(text, years.start(), years.end())})
                if n > int(self.company.get("years_in_business", 0) or 0):
                    req["potential_problems"].append({
                        "item": f"Experience bar ({n} years) is above your LLC's {self.company.get('years_in_business', 0)} — "
                                f"check whether a subcontractor's or key person's experience can count",
                        "evidence": snippet(text, years.start(), years.end())})

        licenses = []
        for pattern, label in LICENSE_PATTERNS:
            m, ev = find_first([pattern], text)
            if m:
                licenses.append(label)
                req["must_have"].append({"item": label, "evidence": ev})
        f["licenses"] = licenses

        visit = re.search(r"mandatory[^.]{0,120}?((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
                          r"|\d{1,2}/\d{1,2}/\d{2,4})", text, re.I)
        if visit:
            f["mandatory_visit_date"] = visit.group(1)

        sites = re.search(r"\b(\d{2,3})\s+(?:sites|locations|facilities|buildings|branches)\b", text, re.I)
        if sites:
            f["site_count"] = int(sites.group(1))

        length = re.search(r"(?:base (?:period|year)[^.]{0,60}?(?:option (?:years?|periods?))[^.]{0,40})"
                           r"|(?:(?:one|two|three|four|five|\d)\s*\(?\d?\)?[- ]year (?:contract|term|agreement)[^.]{0,60})"
                           r"|(?:initial term of[^.]{0,80})", text, re.I)
        if length:
            f["contract_length"] = length.group(0).strip()
            if not opp.contract_length:
                opp.contract_length = length.group(0).strip()[:120]

        # Registration requirements that always apply
        if opp.jurisdiction == "federal":
            req["must_have"].insert(0, {"item": "Active SAM.gov entity registration (UEI) at time of offer",
                                        "evidence": "Standard for federal awards (FAR 52.204-7); confirm in the solicitation."})
            if not self.company.get("sam_registered"):
                req["potential_problems"].append({
                    "item": "Your LLC isn't marked as SAM.gov-registered yet — start registration now; it can take weeks",
                    "evidence": "settings.yaml → company.sam_registered is false"})
        else:
            req["must_have"].insert(0, {"item": "Vendor registration on the agency's bidding portal",
                                        "evidence": "Listed on the official procurement page for this agency."})

        # Typical licenses for the category that the text didn't mention → confirm, don't assert
        cat = self.config.service_categories().get(opp.service_category, {})
        for lic in cat.get("licenses", []):
            if not any(lic.split(" ")[0].lower() in l.lower() for l in licenses):
                req["to_confirm"].append({"item": f"Typical for this service in Texas: {lic}",
                                          "evidence": "Not found in the text reviewed — confirm in the full solicitation."})
        if req["reviewed_chars"] < 1500:
            req["to_confirm"].append({"item": "Only a short listing was available — read the full solicitation packet "
                                              "before relying on this checklist", "evidence": f"{req['reviewed_chars']} characters reviewed"})

        self._llm_pass(opp, text, req)
        for key in ("must_have", "nice_to_have", "potential_problems", "to_confirm"):
            req[key] = _dedupe(req[key])[:9]
        return req

    def _llm_pass(self, opp: Opportunity, text: str, req: dict):
        if not self.llm or not self.llm.enabled or len(text) < 400:
            return
        limit = int(self.config.get("llm.max_chars_per_document", 30000))
        task = ("Extract bidding requirements from this solicitation for a small LLC that manages subcontractors. "
                "Return JSON: {\"summary\": \"2-3 sentences: what the government wants\", "
                "\"scope_bullets\": [up to 5 short bullets], \"must_have\": [short strings], "
                "\"nice_to_have\": [short strings], \"potential_problems\": [short strings], "
                "\"contract_length\": \"or 'not stated in the text reviewed'\", "
                "\"needs_professional_review\": [items a lawyer/accountant/procurement pro should check]}. "
                "Each list max 6 items, each item under 18 words, only from the text.")
        result = self.llm.json(task, f"TITLE: {opp.title}\nAGENCY: {opp.agency}\n\n{text[:limit]}")
        if not result:
            return
        req["summary"] = str(result.get("summary", ""))[:600]
        req["scope_bullets"] = [str(x)[:160] for x in result.get("scope_bullets", [])][:5]
        req["needs_professional_review"] = [str(x)[:200] for x in result.get("needs_professional_review", [])][:5]
        for src, dst in (("must_have", "must_have"), ("nice_to_have", "nice_to_have"),
                         ("potential_problems", "potential_problems")):
            for item in result.get(src, [])[:6]:
                req[dst].append({"item": str(item)[:200], "evidence": "AI summary of the solicitation text"})
        cl = str(result.get("contract_length", ""))
        if cl and "not stated" not in cl.lower() and not opp.contract_length:
            opp.contract_length = cl[:120]


def _num(word: str) -> int | None:
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10}
    if word.isdigit():
        n = int(word)
        return n if 0 < n < 40 else None
    return words.get(word.lower())


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\W+", "", it["item"].lower())[:28]
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out
