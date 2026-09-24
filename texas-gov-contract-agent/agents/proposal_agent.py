"""AGENT 7 — Contract / Proposal Writing.

Builds a starter proposal kit from what the other agents found. Everything is a DRAFT for you to edit:
  capability statement · bid/no-bid analysis · proposal outline · scope-of-work response · management plan
  staffing plan · subcontractor scope · pricing worksheet (CSV) · questions for the contracting officer
  proposal checklist · vendor/subcontractor agreement outline

Ground rules (the same ones the LLM guardrail enforces):
  * It never invents legal requirements, clause numbers, dollar amounts, or deadlines. Facts come from the
    solicitation text the Requirements agent read; everything else is a bracketed [PLACEHOLDER].
  * The solicitation's own instructions (Section L/M for federal, the bid packet for local) always win over
    this outline.
  * Anything with legal, tax, insurance, or pricing consequences is flagged for professional review.

Authoritative references are linked, not paraphrased into rules. Note: the FAR has been undergoing a
large rewrite (the "Revolutionary FAR Overhaul" that began in 2025), so always use the clause text that is
actually incorporated in the solicitation you're bidding.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from core.config import OUTPUT_DIR, Config
from core.extract import days_until, fmt_money
from core.llm import LLM
from core.opportunity import Opportunity

REVIEW = "⚠️ Needs professional review"

# Agency FAR supplements hosted on acquisition.gov (official). Matched against the agency name.
SUPPLEMENTS = [
    (r"\barmy\b", "AFARS — Army FAR Supplement", "https://www.acquisition.gov/afars"),
    (r"air force|space force", "DAFFARS — Department of the Air Force FAR Supplement", "https://www.acquisition.gov/daffars"),
    (r"\bnavy\b|marine corps", "NMCARS — Navy Marine Corps Acquisition Regulation Supplement", "https://www.acquisition.gov/nmcars"),
    (r"veterans affairs|\bva\b", "VAAR — VA Acquisition Regulation", "https://www.acquisition.gov/vaar"),
    (r"homeland security|\bdhs\b|customs and border|\bice\b|\bfema\b|coast guard|\btsa\b",
     "HSAR — Homeland Security Acquisition Regulation", "https://www.acquisition.gov/hsar"),
    (r"general services|\bgsa\b", "GSAM — GSA Acquisition Manual", "https://www.acquisition.gov/gsam"),
]
DOD = re.compile(r"defense|\bdod\b|\barmy\b|\bnavy\b|air force|space force|marine corps|\bdla\b|\bdha\b|\bdeca\b", re.I)
USPS = re.compile(r"postal service|\busps\b", re.I)


class ProposalAgent:
    def __init__(self, config: Config, llm: LLM | None = None):
        self.config = config
        self.llm = llm
        self.company = config.company
        self.regs = config.regulations

    # ── public ───────────────────────────────────────────────────────
    def run(self, opp: Opportunity, full: bool = True, write_files: bool = True) -> dict:
        """Returns {"drafts": {key: {"title", "text", "review"}}, "references": [...], "folder": str}.

        full=False builds the quick kit (bid/no-bid + CO questions) used for "worth reviewing" items.
        """
        drafts = {
            "bid_no_bid": self.bid_no_bid(opp),
            "co_questions": self.co_questions(opp),
        }
        if full:
            drafts.update({
                "capability_statement": self.capability_statement(opp),
                "proposal_outline": self.proposal_outline(opp),
                "scope_response": self.scope_response(opp),
                "management_plan": self.management_plan(opp),
                "staffing_plan": self.staffing_plan(opp),
                "subcontractor_scope": self.subcontractor_scope(opp),
                "pricing_worksheet": self.pricing_worksheet(opp),
                "proposal_checklist": self.proposal_checklist(opp),
                "vendor_agreement_outline": self.vendor_agreement(opp),
            })
        result = {"drafts": drafts, "references": self.references(opp), "full": full, "folder": ""}
        if write_files:
            result["folder"] = str(self._write(opp, result))
        return result

    def references(self, opp: Opportunity) -> list[dict]:
        refs = []
        agency = f"{opp.agency} {opp.office}"
        if opp.jurisdiction == "federal":
            if USPS.search(agency):
                refs.append({"label": "USPS is not covered by the FAR — it buys under its own Supplying Principles "
                                      "and Practices. Follow the solicitation's instructions.", "url": ""})
            else:
                refs.append({"label": "FAR — Federal Acquisition Regulation", "url": self.regs.get("far", "")})
                if DOD.search(agency):
                    refs.append({"label": "DFARS — Defense FAR Supplement (DoD)", "url": self.regs.get("dfars", "")})
                for pattern, label, url in SUPPLEMENTS:
                    if re.search(pattern, agency, re.I):
                        refs.append({"label": label, "url": url})
            if opp.set_aside:
                refs.append({"label": "FAR 52.219-14 Limitations on Subcontracting", "url": self.regs.get("far_52_219_14", "")})
                refs.append({"label": "SBA size standards (check your size under this NAICS)",
                             "url": self.regs.get("sba_size_standards", "")})
            refs.append({"label": "SBA — federal contracting guide", "url": self.regs.get("sba_contracting", "")})
        else:
            src = self.config.source(opp.source)
            if src.get("info_url"):
                refs.append({"label": f"{src.get('name', 'Agency')} — official procurement rules and forms",
                             "url": src["info_url"]})
            if opp.jurisdiction == "state" or opp.analysis.get("requirements", {}).get("facts", {}).get("hub_plan"):
                refs.append({"label": "Texas HUB program (HUB Subcontracting Plan)", "url": self.regs.get("texas_hub_program", "")})
                refs.append({"label": "Texas Comptroller — state purchasing", "url": self.regs.get("texas_procurement_manual", "")})
        return [r for r in refs if r["label"]]

    # ── drafts ───────────────────────────────────────────────────────
    def capability_statement(self, opp: Opportunity) -> dict:
        c = self.company
        cats = self.config.service_categories()
        cat = cats.get(opp.service_category, {})
        caps = [cats.get(k, {}).get("label", k) for k in c.get("existing_capabilities", [])]
        if cat and cat.get("label") not in caps:
            caps.append(f"{cat.get('label')} (managed delivery with vetted partners)")
        naics = sorted({str(n) for k in c.get("existing_capabilities", []) for n in cats.get(k, {}).get("naics", [])}
                       | {str(n) for n in cat.get("naics", [])} | ({str(opp.naics)} if opp.naics else set()))
        certs = ", ".join(c.get("certifications") or []) or "[None yet — add HUB / SBE / M/WBE if you certify]"
        text = f"""CAPABILITY STATEMENT — {c.get('legal_name', '[LLC legal name]')}
{('DBA ' + c['dba']) if c.get('dba') else ''}

CORE COMPETENCIES
{_bullets(caps)}

DIFFERENTIATORS  [edit — only claim what you can prove]
• Owner-led account management: one point of contact, same-day response
• Documented quality checks with photo verification after every service
• [Add a real differentiator: response time, bilingual crews, green products, etc.]

PAST PERFORMANCE  [list 2–3 real jobs: client, scope, dates, contact]
• [Client / property] — [scope] — [dates] — [reference name, phone]
• [Client / property] — [scope] — [dates] — [reference name, phone]

COMPANY DATA
Legal name: {c.get('legal_name', '[LLC legal name]')}
UEI: {c.get('uei') or '[add after SAM.gov registration]'}    CAGE: {c.get('cage_code') or '[add after SAM.gov registration]'}
NAICS: {', '.join(naics) or '[add]'}
Certifications: {certs}
Service area: Dallas–Fort Worth, Houston, Austin, San Antonio  [edit to what you can truly cover]
Contact: {c.get('contact_name', '[name]')} · [phone] · [email] · {c.get('website', '')}
"""
        return {"title": "Capability statement (1 page)", "text": _tidy(text), "review": ""}

    def bid_no_bid(self, opp: Opportunity) -> dict:
        a = opp.analysis
        red, sub = a.get("red_team", {}), a.get("subcontracting", {})
        factors = a.get("factors", {})
        from .scoring import FACTOR_LABELS
        days = days_until(opp.response_deadline)
        kills = [f["message"] for f in red.get("findings", []) if f["severity"] == "kill"]
        majors = [f["message"] for f in red.get("findings", []) if f["severity"] == "major"]
        if kills:
            call = "LEAN NO-BID unless the kill issue can be resolved in writing."
        elif opp.notice_type != "solicitation":
            call = "NOT BIDDABLE YET — respond to the notice to get on the buyer's radar, then watch for the solicitation."
        elif majors or sub.get("status") in ("UNKNOWN", "LIMITED"):
            call = "CONDITIONAL BID — bid only if the open questions below get acceptable answers."
        else:
            call = "LEAN BID — the evidence supports preparing a response."
        lines = [f"Opportunity: {opp.title}", f"Solicitation: {opp.solicitation_number or 'n/a'} · {opp.agency}",
                 f"Match score: {opp.score if opp.score is not None else 'n/a'} / 100 (fit to your criteria, not odds of winning)",
                 f"Time left: {'unknown' if days is None else f'{max(days, 0):.0f} days'}", "",
                 f"CALL: {call}", ""]
        if factors:
            lines.append("Factor scores (0–10):")
            for k, v in sorted(factors.items(), key=lambda kv: -kv[1]):
                lines.append(f"  {FACTOR_LABELS.get(k, k):<28} {v:>4}")
            lines.append("")
        if kills:
            lines += ["Deal-breakers:"] + [f"  ✖ {k}" for k in kills] + [""]
        if majors:
            lines += ["Must resolve before bidding:"] + [f"  ! {m}" for m in majors] + [""]
        lines.append(f"Subcontracting: {sub.get('status', 'UNKNOWN')} — {sub.get('explanation', '')}")
        return {"title": "Bid / no-bid analysis", "text": "\n".join(lines),
                "review": "A recommendation from the evidence reviewed — your call, not a guarantee."}

    def co_questions(self, opp: Opportunity) -> dict:
        strat = opp.analysis.get("bid_strategy", {})
        qs = list(dict.fromkeys(strat.get("questions") or opp.analysis.get("subcontracting", {}).get("questions") or []))
        if not qs:
            qs = ["Is subcontracting of the service work permitted, and is written approval required?",
                  "What is the estimated value or the prior contract amount?"]
        facts = opp.analysis.get("requirements", {}).get("facts", {})
        deadline_note = ("[Check the solicitation for the questions deadline — it is usually well before the bid due date.]"
                         if not facts.get("questions_period") else
                         "[The solicitation sets a questions period — send this before it closes.]")
        who = "Contracting Officer" if opp.jurisdiction == "federal" else "Buyer / Procurement Officer"
        body = "\n".join(f"{i}. {q}" for i, q in enumerate(qs[:8], 1))
        company = self.company.get("legal_name", "")
        text = f"""{deadline_note}

Subject: Questions — {opp.solicitation_number or opp.title[:60]}

Dear {who},

{company} is reviewing solicitation {opp.solicitation_number or '[number]'}, "{opp.title}", and respectfully submits the following questions:

{body}

Thank you for your time. Please let me know if these should be submitted through a different channel.

Respectfully,
Rashad
{self.company.get('contact_name', '')}, {company}
[phone] · [email]
"""
        return {"title": "Questions for the contracting officer / buyer (email)", "text": _tidy(text), "review": ""}

    def proposal_outline(self, opp: Opportunity) -> dict:
        if opp.jurisdiction == "federal":
            sections = [
                "Cover letter (signed; solicitation number; acknowledge every amendment)",
                "Standard form the solicitation uses (commonly SF-1449 or SF-33), completed and signed",
                "Technical approach — mirror the Performance Work Statement paragraph by paragraph",
                "Management plan & quality control plan (matched to any QASP)",
                "Staffing plan / key personnel",
                "Past performance (yours, or key personnel / subcontractor experience if the solicitation allows it)",
                "Subcontracting approach (only as permitted — see the Limitations on Subcontracting check)",
                "Price volume / schedule of services (the government's pricing format, exactly)",
                "Representations & certifications (SAM.gov entity registration) and any solicitation-specific certifications",
            ]
            note = ("Build the final structure from the solicitation's instructions to offerors (Section L or the "
                    "equivalent) and evaluation factors (Section M) — graders score against those.")
        else:
            sections = [
                "Cover letter / transmittal (signed by an authorized officer of the LLC)",
                "All required agency forms (bid form, signature page, addenda acknowledgments)",
                "Company qualifications & experience",
                "Service approach / work plan",
                "Staffing, supervision, and quality control",
                "References",
                "Subcontractor list and M/WBE / SBE / HUB forms (if the packet includes them)",
                "Pricing (the agency's price form, exactly as provided)",
                "Insurance certificate or broker letter, W-9, and any Texas statutory forms the packet lists",
            ]
            note = "Use the packet's required order and page limits; missing forms are the #1 reason local bids are rejected."
        text = "PROPOSAL OUTLINE\n\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(sections, 1)) + f"\n\nNote: {note}"
        return {"title": "Proposal outline", "text": text, "review": ""}

    def scope_response(self, opp: Opportunity) -> dict:
        a = opp.analysis
        wants = a.get("bid_strategy", {}).get("what_government_wants") or opp.title
        sub = a.get("subcontracting", {})
        cat = self.config.service_categories().get(opp.service_category, {})
        label = (cat.get("label") or "the required services").lower()
        c = self.company
        delivery = {
            "SUPPORTED": f"{c.get('legal_name')} will serve as prime contractor and single point of accountability, "
                         f"delivering the work through a vetted {cat.get('subcontractor', 'subcontractor').lower()} "
                         f"under our direct supervision and quality control.",
            "APPROVAL_REQUIRED": f"{c.get('legal_name')} will serve as prime contractor. Any subcontractor will be "
                                 f"submitted for the agency's written approval before starting work.",
            "LIMITED": f"{c.get('legal_name')} will perform [the required share] of the work with its own employees "
                       f"[and/or similarly situated small-business subcontractors], in compliance with the "
                       f"Limitations on Subcontracting clause.",
            "RESTRICTED": f"{c.get('legal_name')} will self-perform the work with its own employees.",
        }.get(sub.get("status"), f"{c.get('legal_name')} will [describe delivery model — confirm subcontracting "
                                 f"is permitted before describing subcontractors].")
        text = f"""SCOPE-OF-WORK RESPONSE (starter draft)

Understanding of the requirement
{wants}

Our approach
{delivery} We will begin with a walk-through of each location to confirm quantities, frequencies, access
rules, and site contacts, then set up the service schedule and inspection checklist before day one.

Service delivery
• Scheduled {label} per the Performance Work Statement / specifications, by location and frequency  [map each PWS item]
• Supervisor inspections with photo documentation; deficiencies corrected within [time the solicitation requires]
• Supplies, equipment, and safety data sheets managed as the specifications require  [confirm who furnishes supplies]

Quality control
• Written inspection checklist tied to each specification item; results logged and available to the agency
• Customer complaint response within [hours]; root-cause and corrective-action notes on repeat issues

Transition / start-up
• Mobilization plan: [x] days from notice to proceed; site orientation, badging, and background checks as required
"""
        return {"title": "Scope-of-work response", "text": _tidy(text),
                "review": "Edit to match the exact specification items; don't promise anything you can't deliver."}

    def management_plan(self, opp: Opportunity) -> dict:
        c = self.company
        facts = opp.analysis.get("requirements", {}).get("facts", {})
        extra = []
        if facts.get("qasp"):
            extra.append("• Align the QC plan to the government's Quality Assurance Surveillance Plan (QASP) standards")
        if facts.get("coverage_24_7"):
            extra.append("• 24/7 contact line and on-call supervisor rotation")
        if facts.get("background_checks"):
            extra.append("• Background checks completed and documented before anyone starts on site")
        text = f"""MANAGEMENT PLAN

Organization
• Program manager: {c.get('contact_name', '[name]')} — contract owner, government point of contact, invoicing
• Site supervisor(s): [name / your sub's supervisor] — daily oversight, inspections, crew scheduling
• Service provider: [your company crew or approved subcontractor] — performs the work

Communication
• Named point of contact with phone and email; response to agency calls within [x] hours
• Monthly service report: work completed, inspection results, issues and fixes

Quality control
• Inspection checklist per location; photo log; corrective action tracked to closure
• Periodic customer walk-through with the agency representative
{chr(10).join(extra)}

Risk & continuity
• Backup subcontractor or crew identified before award
• Insurance certificates tracked for every subcontractor; expired coverage = no work
"""
        return {"title": "Management plan", "text": _tidy(text), "review": ""}

    def staffing_plan(self, opp: Opportunity) -> dict:
        facts = opp.analysis.get("requirements", {}).get("facts", {})
        sites = facts.get("site_count")
        rows = [("Program manager", "1 (part-time)", "Contract admin, government liaison, invoicing"),
                ("Site supervisor", "1 per [x] sites" if sites else "1", "Daily oversight, inspections"),
                ("Technicians / crew", "[calculate from hours ÷ productivity]", "Service delivery"),
                ("Backup / relief", "[x]", "Absences and surge")]
        lines = ["STAFFING PLAN", "", f"{'Role':<22}{'Count':<34}Responsibility"]
        lines += [f"{r[0]:<22}{r[1]:<34}{r[2]}" for r in rows]
        lines.append("")
        if sites:
            lines.append(f"Scope mentions about {sites} locations — schedule routes so one supervisor covers nearby sites.")
        if facts.get("sca"):
            lines.append("Service Contract Act applies (per the text): every worker must be paid at least the wage and "
                         "fringe in the attached wage determination — including your subcontractor's crew.")
        if facts.get("key_personnel"):
            lines.append("Key personnel are named in the solicitation: résumés/qualifications may be required and "
                         "substitutions may need approval.")
        lines.append("Estimate hours from the specification (square footage × frequency ÷ production rate) before "
                     "setting headcount.")
        return {"title": "Staffing plan", "text": "\n".join(lines), "review": ""}

    def subcontractor_scope(self, opp: Opportunity) -> dict:
        sub = opp.analysis.get("subcontracting", {})
        facts = opp.analysis.get("requirements", {}).get("facts", {})
        status = sub.get("status", "UNKNOWN")
        warning = {
            "RESTRICTED": "⛔ The solicitation appears to require self-performance. Do not subcontract unless the buyer confirms otherwise in writing.",
            "LIMITED": "⚠️ Limitations on Subcontracting likely apply — keep the subcontracted share within the cap.",
            "UNKNOWN": "⚠️ Subcontracting isn't addressed in the text reviewed — get written confirmation first.",
            "APPROVAL_REQUIRED": "⚠️ Submit this subcontractor for written approval before they start.",
        }.get(status, "")
        req = []
        for key, label in [("background_checks", "Background checks for all workers as the prime contract requires"),
                           ("e_verify", "E-Verify enrollment (the prime contract references it)"),
                           ("sca", "Pay at least the wage determination rates and fringe benefits (Service Contract Act)"),
                           ("green_products", "Use the green / environmentally preferable products the specification requires")]:
            if facts.get(key):
                req.append(label)
        gl = facts.get("general_liability")
        req.append(f"General liability ≥ {fmt_money(gl)} naming the prime (and agency, if required) as additional insured"
                   if gl else "Insurance at the limits the prime contract requires, naming the prime as additional insured")
        req.append("Workers' compensation for all employees")
        text = f"""SUBCONTRACTOR SCOPE OF WORK — {opp.title}
{warning}

1. Work to be performed: [copy the exact specification items the sub will do, by location and frequency]
2. Schedule: [days / hours / frequencies]; access rules and site contacts per the prime contract
3. Standards: work must meet the prime contract's specifications and pass the prime's inspections
4. Requirements passed down to the subcontractor:
{_bullets(req)}
5. Reporting: completion log with photos after each service; issues reported to the prime same day
6. Corrective action: re-perform deficient work within [time] at no cost
7. Flow-down: include every clause the prime contract requires to be included in subcontracts (the clause text itself says when it must flow down)
8. Pricing: [unit / monthly price], invoiced [monthly], payable [terms — see vendor agreement]
9. Non-circumvention: the subcontractor won't solicit the agency for this work during the term  [attorney to review]
"""
        return {"title": "Subcontractor scope", "text": _tidy(text),
                "review": f"{REVIEW} — have an attorney review flow-down and payment terms."}

    def pricing_worksheet(self, opp: Opportunity) -> dict:
        header = ["Line item", "Location", "Frequency", "Unit", "Quantity (per year)", "Subcontractor cost per unit",
                  "Subcontractor cost (year)", "Supervision & QC %", "Insurance %", "Overhead %", "Margin %",
                  "Price (year)", "Price per unit"]
        rows = [header]
        items = ["[Service item 1 from the price form]", "[Service item 2]", "[Service item 3]"]
        for i, item in enumerate(items, start=2):
            rows.append([item, "[site]", "[weekly]", "[each / sq ft / hour]", "", "",
                         f"=E{i}*F{i}", "8%", "3%", "7%", "10%",
                         f"=G{i}*(1+H{i}+I{i}+J{i})/(1-K{i})", f'=IF(E{i}>0,L{i}/E{i},"")'])
        n = len(rows)
        rows.append(["TOTAL", "", "", "", "", "", f"=SUM(G2:G{n})", "", "", "", "", f"=SUM(L2:L{n})", ""])
        rows.append([])
        rows.append(["Notes"])
        rows.append(["Percentages are placeholders — replace with your real costs. Margin is applied as a % of price."])
        rows.append(["If the Service Contract Act applies, sub labor must meet the wage determination (wage + fringe)."])
        rows.append(["Use the agency's price form for the actual bid; this sheet is for your math only."])
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        return {"title": "Pricing worksheet (CSV — opens in Excel or Google Sheets)", "text": buf.getvalue(),
                "review": f"{REVIEW} — have a CPA sanity-check pricing, taxes, and cash flow.", "filename": "pricing_worksheet.csv"}

    def proposal_checklist(self, opp: Opportunity) -> dict:
        a = opp.analysis
        req = a.get("requirements", {})
        strat = a.get("bid_strategy", {})
        facts = req.get("facts", {})
        deadline = opp.response_deadline.strftime("%a %b %d, %Y %I:%M %p %Z") if opp.response_deadline else "[not listed — confirm]"
        items = [f"Due: {deadline} — submit at least one business day early"]
        if facts.get("mandatory_visit_date"):
            items.append(f"Attend the mandatory pre-bid / site visit ({facts['mandatory_visit_date']})")
        items += [f"Meet: {m['item']}" for m in req.get("must_have", [])]
        items += [f"Include: {d}" for d in strat.get("documents_needed", [])]
        items += ["Acknowledge every addendum / amendment", "Signed by an authorized officer of the LLC",
                  "Pricing matches the agency's form exactly (units, periods, totals)",
                  "Follow page limits, file formats, and naming rules in the instructions"]
        items += [f"Confirm: {t['item']}" for t in req.get("to_confirm", [])]
        text = "PROPOSAL CHECKLIST\n\n" + "\n".join(f"☐ {x}" for x in dict.fromkeys(items))
        return {"title": "Proposal checklist", "text": text, "review": ""}

    def vendor_agreement(self, opp: Opportunity) -> dict:
        text = """VENDOR / SUBCONTRACTOR AGREEMENT — OUTLINE (not a contract)

1. Parties and recitals (reference the prime contract number and agency)
2. Scope of work (attach the Subcontractor Scope) and order of precedence
3. Term — tied to the prime contract, including option periods; ends if the prime contract ends
4. Pricing, invoicing, and payment timing  [attorney/CPA: how payment relates to when the agency pays you]
5. Insurance — types, limits, additional-insured and waiver-of-subrogation requirements; certificates before work
6. Indemnification and limitation of liability
7. Compliance — licenses, background checks, wage requirements, safety, and every clause the prime contract requires to flow down
8. Quality standards, inspections, and corrective action; right to remove workers from the site
9. Confidentiality and records / audit access matching the prime contract
10. Non-solicitation / non-circumvention of the agency and your employees
11. Independent-contractor status; subcontractor pays its own workers and taxes
12. Termination for convenience and for default, mirroring the prime contract's rights
13. Changes — only by written change order; no work outside scope without approval
14. Disputes and governing law (Texas)
15. Signatures by authorized representatives
"""
        return {"title": "Vendor / subcontractor agreement outline", "text": text,
                "review": f"{REVIEW} — an attorney must prepare or review the actual agreement."}

    # ── files ────────────────────────────────────────────────────────
    def _write(self, opp: Opportunity, result: dict) -> Path:
        folder = OUTPUT_DIR / "proposals" / opp.short_id
        folder.mkdir(parents=True, exist_ok=True)
        combined = [f"# Proposal kit — {opp.title}", f"{opp.agency} · {opp.solicitation_number}", f"Official link: {opp.url}", "",
                    "> Drafts to edit. Items marked ⚠️ need professional review before use.", ""]
        for key, d in result["drafts"].items():
            if d.get("filename"):
                (folder / d["filename"]).write_text(d["text"], encoding="utf-8")
                combined += [f"## {d['title']}", f"Saved as `{d['filename']}`.", ""]
                continue
            combined += [f"## {d['title']}"]
            if d.get("review"):
                combined.append(f"*{d['review']}*")
            combined += ["", "```", d["text"].rstrip(), "```", ""]
        combined += ["## References"] + [f"- {r['label']}" + (f": {r['url']}" if r.get("url") else "")
                                         for r in result["references"]]
        (folder / "proposal_kit.md").write_text("\n".join(combined), encoding="utf-8")
        return folder


def _bullets(items) -> str:
    return "\n".join(f"• {i}" for i in items if i) or "• [add]"


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
