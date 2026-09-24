#!/usr/bin/env python3
"""Texas Government Contract Opportunity Agent.

  python main.py run                 full cycle (skips if the last run was < 3 days ago)
  python main.py run --force         run now regardless of schedule
  python main.py run --no-email      research + dashboard, no email
  python main.py demo                sample-data run → build/demo (never touches your real data)
  python main.py mark TX-1A2B3C pursue --reason "..."   (pursue | watch | pass)
  python main.py list [--category top]
  python main.py show TX-1A2B3C
  python main.py proposal TX-1A2B3C  write the full proposal kit to output/proposals/
  python main.py build-site          rebuild the dashboard from the database
  python main.py test-email          send a test email with the latest report
  python main.py sources [--check]   list configured portals (and test them)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import timedelta
from pathlib import Path

from agents.award_analyst import AwardAnalyst
from agents.bid_strategy_agent import BidStrategyAgent
from agents.discovery_agent import DiscoveryAgent
from agents.opportunity_filter import OpportunityFilter
from agents.opportunity_scout import OpportunityScout
from agents.proposal_agent import ProposalAgent
from agents.red_team_agent import RedTeamAgent
from agents.requirements_agent import RequirementsAgent
from agents.scoring import Scorer
from agents.subcontracting_agent import SubcontractingAgent
from core.config import DATA_DIR, OUTPUT_DIR, ROOT, SITE_DIR, Config
from core.extract import LOCAL_TZ, now
from core.llm import LLM
from core.opportunity import Opportunity
from database.repository import Repository, _utc
from reports.cards import card
from reports.email_report import EmailReport
from reports.site_builder import SiteBuilder, site_url

log = logging.getLogger("contract-agent")
ACTIVE = ("top", "review", "watch")


# ═════════════════════════════════════════════════════════════════════════════
#  PIPELINE
# ═════════════════════════════════════════════════════════════════════════════
class Pipeline:
    def __init__(self, config: Config, repo: Repository, connectors=None, offline: bool = False,
                 site_dir: Path | None = None, output_dir: Path | None = None, is_demo: bool = False):
        self.config, self.repo = config, repo
        self.offline, self.is_demo = offline, is_demo
        self.site_dir = site_dir or SITE_DIR
        self.output_dir = output_dir or OUTPUT_DIR
        self.llm = LLM(config)
        if offline:
            self.llm.provider = "none"
        self.scout = OpportunityScout(config, connectors)
        self.filter = OpportunityFilter(config)
        self.requirements = RequirementsAgent(config, self.llm)
        self.subcontracting = SubcontractingAgent(config, self.llm)
        self.awards = AwardAnalyst(config, self.scout, offline=offline)
        self.red_team = RedTeamAgent(config, self.llm)
        self.scorer = Scorer(config)
        self.strategy = BidStrategyAgent(config, self.llm)
        self.quiet_strategy = BidStrategyAgent(config, None)
        self.proposal = ProposalAgent(config, self.llm)
        self.discovery = DiscoveryAgent(config, self.llm)

    # ── one full cycle ───────────────────────────────────────────────
    def run(self, kind: str = "scheduled", force: bool = False, send_email: bool = True,
            dry_run: bool = False) -> dict:
        self.repo.sync_decisions()
        every = float(self.config.get("schedule.run_every_days", 3))
        if not force and not self.repo.due(every):
            last = self.repo.last_full_run()
            msg = f"Not due yet — last full run {last.started_at.astimezone(LOCAL_TZ):%b %d %I:%M %p} (every {every:g} days). Use --force."
            log.info(msg)
            self.repo.commit()
            return {"skipped": True, "message": msg}

        run = self.repo.start_run(kind)
        log.info("Scouting %d sources…", len(self.scout.connectors))
        found, health = self.scout.run()
        if self.is_demo:
            for h in health:
                h.status, h.message = "demo", "Not contacted in this sample run. Real status appears after your first live run."
        log.info("Found %d opportunities", len(found))
        leads = self.save_leads()

        # 1) store everything, noting what changed
        changed_uids = set()
        for opp in found:
            _, is_new, changes = self.repo.upsert(opp)
            if changes:
                changed_uids.add(opp.uid)
        self.repo.commit()
        expired = self.repo.expire_past_deadlines()

        # 2) filter (or re-score items already fully reviewed)
        survivors, carried, results = [], [], []
        for fresh in found:
            row = self.repo.get(fresh.uid)
            opp = self.repo.to_opp(row)
            if row.fully_reviewed and opp.analysis.get("red_team") and fresh.uid not in changed_uids:
                ok, reason = self.filter.evaluate(opp)          # deadlines move; re-check cheaply
                if ok:
                    self._rescore(opp)
                    carried.append(opp)
                else:
                    self._filtered(opp, reason)
                results.append(opp)
                continue
            opp.analysis = {k: v for k, v in opp.analysis.items() if k in ("proposal",)}
            ok, reason = self.filter.evaluate(opp)
            if ok:
                survivors.append(opp)
            else:
                self._filtered(opp, reason)
            results.append(opp)
        self.repo.commit()
        log.info("Passed filter: %d new/changed, %d carried over", len(survivors), len(carried))

        # 3) deep review for the strongest survivors (tracked items first)
        decisions = {r.uid: r.decision for r in self.repo.all_rows() if r.decision}
        survivors.sort(key=lambda o: (decisions.get(o.uid) in ("PURSUE", "WATCH"), o.notice_type == "solicitation",
                                      o.analysis.get("pre_score", 0)), reverse=True)
        limit = int(self.config.get("limits.max_deep_reviews", 12))
        reviewed_now = 0
        for opp in survivors[:limit]:
            try:
                self.deep_review(opp, decision=decisions.get(opp.uid, ""))
                reviewed_now += 1
            except Exception as exc:                             # never lose the run to one bad record
                log.exception("Review failed for %s", opp.short_id)
                opp.category, opp.filter_reason = "watch", ""
                opp.analysis["review_error"] = f"{exc.__class__.__name__}: {exc}"[:200]
                self.repo.save_results(opp, fully_reviewed=False)
        for opp in survivors[limit:]:
            opp.category, opp.filter_reason = "watch", ""
            opp.analysis["queued"] = "Queued for full review next run (review limit reached this cycle)."
            opp.score = opp.analysis.get("pre_score")
            self.repo.save_results(opp, fully_reviewed=False)
        self.repo.commit()

        # 4) PURSUE items always get the full proposal kit + checklist
        for row in self.repo.open_rows():
            if row.decision == "PURSUE" and not (row.analysis or {}).get("proposal", {}).get("full"):
                opp = self.repo.to_opp(row)
                opp.analysis["proposal"] = self.proposal.run(opp, full=True, write_files=not self.is_demo)
                self.repo.save_results(opp)

        # 5) discovery
        discovery = self.discovery.run(results)

        # 6) stats
        by_cat = {c: 0 for c in ("top", "review", "watch", "filtered")}
        for opp in results:
            row = self.repo.get(opp.uid)
            cat = row.category if row.category in by_cat else "filtered"
            by_cat[cat] += 1
        passed = len(survivors) + len(carried)
        fully = sum(1 for o in results if (self.repo.get(o.uid).fully_reviewed and self.repo.get(o.uid).category in ACTIVE))
        stats = {"found": len(found), "passed": passed, "reviewed": fully, "reviewed_this_run": reviewed_now,
                 **by_cat, "expired_marked": expired, "changed": len(changed_uids),
                 "sources_ok": sum(1 for h in health if h.status in ("ok", "empty")),
                 "sources_total": len(health), "llm_calls": self.llm.calls, "teaming_leads": len(leads)}
        run.stats, run.source_health = stats, [h.to_dict() for h in health]
        run.discovery = discovery
        run.finished_at = _utc(now())
        self.repo.commit()

        try:
            self.repo.prune()
        except Exception as exc:                                  # housekeeping must never fail a run
            log.warning("Database prune skipped: %s", exc)

        # 7) dashboard + email
        site_path = SiteBuilder(self.config, self.repo).build(self.site_dir, is_demo=self.is_demo)
        report = self.build_report(stats, health)
        mailer = EmailReport(self.config)
        email_status = "skipped"
        if mailer.is_meaningful(report) or self.config.get("email.send_when_empty", False):
            msg = mailer.build(report)
            preview = mailer.save_preview(msg, self.output_dir)
            if send_email and not dry_run:
                ok, email_status = mailer.send(msg)
                if ok:
                    run.emailed = True
                    self._after_email(report)
            else:
                email_status = f"Preview only: {preview}"
        else:
            email_status = "No email — nothing new or meaningful this cycle."
        self.repo.commit()
        log.info("Dashboard: %s | %s", site_path, email_status)
        return {"skipped": False, "stats": stats, "email": email_status, "site": str(site_path),
                "sources": [h.to_dict() for h in health], "discovery": discovery}

    # ── contract vehicles: who holds IDIQs/BPAs for your work in Texas ───
    def save_leads(self) -> list[dict]:
        """Merge teaming leads from every source that produced them (SAM.gov award notices, USAspending IDVs)."""
        leads, seen = [], set()
        for conn in self.scout.connectors:
            for lead in getattr(conn, "leads", None) or []:
                key = (lead.get("holder", "").lower(), lead.get("award_id") or lead.get("contract_number") or lead.get("title"))
                if lead.get("holder") and key not in seen:
                    seen.add(key)
                    leads.append(lead)
        leads.sort(key=lambda x: -(x.get("amount") or 0))
        path = self.repo.data_dir / "vehicles.json"
        if leads or not path.exists():
            path.write_text(json.dumps({"generated_at": now().isoformat(), "leads": leads[:60]}, indent=1,
                                       default=str), encoding="utf-8")
        return leads

    # ── agents for one opportunity ───────────────────────────────────
    def deep_review(self, opp: Opportunity, decision: str = "") -> Opportunity:
        self.scout.enrich(opp)
        ok, reason = self.filter.evaluate(opp)        # the full text can reveal new disqualifiers
        if not ok:
            self._filtered(opp, reason)
            self.repo.save_results(opp, fully_reviewed=True)
            return opp
        a = opp.analysis
        a["requirements"] = self.requirements.run(opp)
        a["subcontracting"] = self.subcontracting.run(opp)
        a["award_history"] = self.awards.run(opp)
        a["red_team"] = self.red_team.run(opp)
        score, factors = self.scorer.score(opp)
        a["factors"] = factors
        opp.score = score
        opp.category = self.scorer.categorize(opp, score)
        opp.filter_reason = self._reason(opp, factors)
        a["bid_strategy"] = self.strategy.run(opp, factors)
        self._note_in_why(opp)
        if opp.category in ("top", "review") or decision == "PURSUE":
            full = opp.category == "top" or decision == "PURSUE"
            a["proposal"] = self.proposal.run(opp, full=full, write_files=full and not self.is_demo)
        a["reviewed_at"] = now().isoformat()
        self.repo.save_results(opp, fully_reviewed=True)
        return opp

    def _rescore(self, opp: Opportunity):
        """Deadlines move every day, so re-score carried-over items (no LLM calls)."""
        score, factors = self.scorer.score(opp)
        opp.analysis["factors"], opp.score = factors, score
        opp.category = self.scorer.categorize(opp, score)
        opp.filter_reason = self._reason(opp, factors)
        polish = (opp.analysis.get("bid_strategy") or {})
        fresh = self.quiet_strategy.run(opp, factors)
        for k in ("why_it_made_the_list", "biggest_risk"):       # keep any AI-polished wording
            if polish.get(k) and k in fresh and polish.get("_polished"):
                fresh[k] = polish[k]
        opp.analysis["bid_strategy"] = fresh
        self._note_in_why(opp)
        self.repo.save_results(opp)

    @staticmethod
    def _note_in_why(opp: Opportunity):
        note = opp.analysis.get("category_note")
        strat = opp.analysis.get("bid_strategy") or {}
        if note and opp.category == "review" and note not in strat.get("why_it_made_the_list", ""):
            strat["why_it_made_the_list"] = (strat.get("why_it_made_the_list", "") + " " + note).strip()

    def _filtered(self, opp: Opportunity, reason: str):
        opp.category, opp.filter_reason = "filtered", reason
        self.repo.save_results(opp, fully_reviewed=False)

    def _reason(self, opp: Opportunity, factors: dict) -> str:
        if opp.category != "filtered":
            return ""
        return opp.analysis.get("category_note") or self._low_score_reason(factors)

    @staticmethod
    def _low_score_reason(factors: dict) -> str:
        weak = Scorer.weakest(factors, 2)
        return f"Researched, but the overall fit is weak (lowest factors: {', '.join(weak).lower()})."

    # ── email content ────────────────────────────────────────────────
    def build_report(self, stats: dict, health) -> dict:
        rows = [r for r in self.repo.open_rows() if r.category in ACTIVE]
        cards = []
        for r in rows:
            opp = self.repo.to_opp(r)
            cards.append((r, card(opp, self.config, r)))
        live = [(r, c) for r, c in cards if r.decision != "PASS"]

        def fresh(r):
            return r.last_emailed_at is None

        top = sorted([c for r, c in live if c["category"] == "top" and fresh(r)], key=lambda c: -(c["score"] or 0))
        review = sorted([c for r, c in live if c["category"] == "review" and fresh(r)], key=lambda c: -(c["score"] or 0))
        updates = [c for r, c in cards if r.changes and (r.last_emailed_at or r.decision in ("PURSUE", "WATCH"))
                   and r.decision != "PASS"]
        update_ids = {c["id"] for c in updates}
        still = [c for r, c in live if not fresh(r) and c["category"] in ("top", "review") and c["id"] not in update_ids]
        still.sort(key=lambda c: c["deadline"]["days"] if c["deadline"]["days"] is not None else 1e9)
        watch_best = None
        if not top:
            watch = sorted([c for r, c in live if c["category"] == "watch" and fresh(r)], key=lambda c: -(c["score"] or 0))
            watch_best = watch[0] if watch else None
        return {"date": now().date(), "stats": stats,
                "top": top[: int(self.config.get("limits.top_in_email", 5))],
                "review": review[: int(self.config.get("limits.review_in_email", 3))],
                "watch_best": watch_best, "updates": updates, "still_open": still,
                "site_url": site_url(self.config) if not self.is_demo else "",
                "source_problems": sum(1 for h in health if h.status in ("error", "setup")),
                "teaming_leads": stats.get("teaming_leads", 0),
                "all_sources_failed": bool(health) and not self.is_demo and
                all(h.status in ("error", "setup", "manual") for h in health),
                "is_demo": self.is_demo}

    def _after_email(self, report: dict):
        sent = [c["id"] for c in report["top"] + report["review"]] + \
               ([report["watch_best"]["id"]] if report.get("watch_best") else [])
        uids = []
        for sid in sent + [c["id"] for c in report["updates"]]:
            row = self.repo.find(sid)
            if row:
                uids.append(row.uid)
        self.repo.mark_emailed(uids)
        self.repo.clear_changes([self.repo.find(c["id"]).uid for c in report["updates"] if self.repo.find(c["id"])])


# ═════════════════════════════════════════════════════════════════════════════
#  DEMO (sample data, isolated from your real database)
# ═════════════════════════════════════════════════════════════════════════════
class SampleConnector:
    """Serves sample opportunities for one real source key (so links and portal info resolve)."""

    def __init__(self, key, cfg, opps, leads=None):
        self.key, self.opps, self.leads = key, opps, leads or []
        self.name = cfg.get("name", key)
        self.market = cfg.get("market", "")
        self.url, self.info_url = cfg.get("url", ""), cfg.get("info_url", "")

    def fetch(self):
        return [Opportunity.from_dict(o.to_dict()) for o in self.opps]

    def enrich(self, opp):
        return None

    def past_awards(self, opp):
        return []


def load_samples(config: Config, path: Path | None = None, shift_days: float = 0) -> list[Opportunity]:
    path = path or (ROOT / "data" / "sample_opportunities.json")
    items = json.loads(path.read_text(encoding="utf-8"))["opportunities"]
    base = now().replace(minute=0, second=0, microsecond=0)
    out = []
    for item in items:
        item = dict(item)
        d = item.pop("deadline_in_days", None)
        p = item.pop("posted_days_ago", 5)
        opp = Opportunity.from_dict(item)
        if d is not None:
            opp.response_deadline = (base + timedelta(days=d + shift_days)).replace(hour=14)
        opp.posted_date = base - timedelta(days=p)
        out.append(opp)
    return out


def sample_leads(path: Path | None = None) -> list[dict]:
    """Sample teaming leads with dates relative to today."""
    path = path or (ROOT / "data" / "sample_opportunities.json")
    out = []
    for lead in json.loads(path.read_text(encoding="utf-8")).get("vehicle_leads", []):
        lead = dict(lead)
        if "last_order_in_days" in lead:
            lead["last_order_date"] = (now() + timedelta(days=lead.pop("last_order_in_days"))).strftime("%Y-%m-%d")
        if "award_days_ago" in lead:
            lead["award_date"] = (now() - timedelta(days=lead.pop("award_days_ago"))).strftime("%Y-%m-%d")
        out.append(lead)
    return out


def sample_connectors(config: Config, opps: list[Opportunity]):
    groups: dict[str, list] = {}
    for o in opps:
        groups.setdefault(o.source, []).append(o)
    out = []
    for entry in config.source_entries():
        leads = sample_leads() if entry["key"] == "usaspending_idv" else []
        out.append(SampleConnector(entry["key"], entry, groups.get(entry["key"], []), leads))
    return out


def run_demo(out: Path) -> dict:
    out = Path(out)
    data_dir = out / "data"
    if data_dir.exists():
        for f in data_dir.glob("*"):
            f.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    config = Config(data_dir=data_dir)
    repo = Repository(url=f"sqlite:///{data_dir / 'demo.db'}", data_dir=data_dir)
    samples = load_samples(config)
    pipe = Pipeline(config, repo, connectors=sample_connectors(config, samples), offline=True,
                    site_dir=out / "site", output_dir=out / "email", is_demo=True)
    first = pipe.run(kind="demo", force=True, send_email=False)
    email_dir = out / "email"
    for ext in ("html", "txt"):                       # keep cycle 1's email; cycle 2 overwrites last_email.*
        src = email_dir / f"last_email.{ext}"
        if src.exists():
            src.replace(email_dir / f"report_1_first_cycle.{ext}")
    # pretend the first email went out, then record some decisions like a real owner would
    report = pipe.build_report(first["stats"], [])
    pipe._after_email(report)
    for sol, decision, reason in [("DEMO-DAL-JAN-0412", "PURSUE", "Matches our janitorial crews; lining up two subs."),
                                  ("DEMO-AUS-CUS-0233", "PASS", "Requires 100% self-performance."),
                                  ("DEMO-SAM-FA3016-SS", "WATCH", "Respond to the sources sought; wait for the RFP.")]:
        try:
            repo.write_decision(sol, decision, reason)
        except LookupError:
            pass
    repo.commit()
    # second pass three days later: an addendum moves one deadline (shows change tracking)
    changed = load_samples(config)
    for o in changed:
        if o.solicitation_number == "DEMO-DAL-JAN-0412":
            o.response_deadline = o.response_deadline + timedelta(days=7)
            o.attachments = o.attachments + [{"name": "Addendum 1 - revised pricing form.pdf", "url": ""}]
    pipe.scout = OpportunityScout(config, sample_connectors(config, changed))
    pipe.awards.scout = pipe.scout
    second = pipe.run(kind="demo", force=True, send_email=False)
    for ext in ("html", "txt"):
        src = email_dir / f"last_email.{ext}"
        if src.exists():
            src.replace(email_dir / f"report_2_three_days_later.{ext}")
    return {"first": first, "second": second, "site": str(out / "site" / "index.html"),
            "email": str(email_dir / "report_1_first_cycle.html"),
            "followup_email": str(email_dir / "report_2_three_days_later.html")}


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════
def _print_row(r):
    dl = r.response_deadline.astimezone(LOCAL_TZ).strftime("%b %d") if r.response_deadline else "—"
    stamp = f" [{r.decision}]" if r.decision else ""
    print(f"{r.short_id}  {r.category or '?':<8} {(r.score or 0):>5.1f}  {dl:<6}  {r.title[:70]}{stamp}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Texas Government Contract Opportunity Agent")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a full search cycle")
    r.add_argument("--force", action="store_true", help="ignore the every-3-days schedule")
    r.add_argument("--no-email", action="store_true", help="don't send the email (preview is still saved)")
    r.add_argument("--dry-run", action="store_true", help="same as --no-email")
    r.add_argument("--manual", action="store_true", help="label this run as manual")

    d = sub.add_parser("demo", help="run on sample data into a separate folder")
    d.add_argument("--out", default="build/demo")

    m = sub.add_parser("mark", help="record PURSUE / WATCH / PASS")
    m.add_argument("id")
    m.add_argument("decision", choices=["pursue", "watch", "pass", "PURSUE", "WATCH", "PASS"])
    m.add_argument("--reason", default="")
    m.add_argument("--no-site", action="store_true")

    i = sub.add_parser("decide-from-issue", help="(used by GitHub Actions) record a decision from an issue")
    i.add_argument("--title", default=None)
    i.add_argument("--body", default=None)

    ls = sub.add_parser("list", help="list opportunities")
    ls.add_argument("--category", default="", help="top | review | watch | filtered | expired")

    s = sub.add_parser("show", help="show one opportunity")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")

    pr = sub.add_parser("proposal", help="write the full proposal kit for one opportunity")
    pr.add_argument("id")

    sub.add_parser("build-site", help="rebuild the dashboard")
    sub.add_parser("test-email", help="send the latest report as a test email")
    sc = sub.add_parser("sources", help="list sources")
    sc.add_argument("--check", action="store_true", help="try each source now")
    sub.add_parser("init-db", help="create the database")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if args.cmd == "demo":
        res = run_demo(Path(args.out))
        print(json.dumps({k: res[k] for k in ("site", "email", "followup_email")}, indent=2))
        print("Stats:", json.dumps(res["second"]["stats"]))
        return 0

    config = Config()
    repo = Repository()

    if args.cmd == "run":
        pipe = Pipeline(config, repo)
        res = pipe.run(kind="manual" if (args.manual or args.force) else "scheduled", force=args.force,
                       send_email=not (args.no_email or args.dry_run), dry_run=args.dry_run)
        if res.get("skipped"):
            print(res["message"])
            return 0
        print(json.dumps(res["stats"], indent=2))
        print(res["email"])
        bad = [s for s in res["sources"] if s["status"] in ("error", "setup")]
        for s in bad:
            print(f"  ! {s['name']}: {s['status']} — {s['message']}")
        return 0

    if args.cmd in ("mark", "decide-from-issue"):
        if args.cmd == "decide-from-issue":
            import os
            title = args.title if args.title is not None else os.getenv("ISSUE_TITLE", "")
            body = args.body if args.body is not None else os.getenv("ISSUE_BODY", "")
            mt = re.search(r"\b(PURSUE|WATCH|PASS)\b\s+(TX-[0-9A-F]{6})\b", title or "", re.I)
            if not mt:
                print("Issue title must look like 'PURSUE TX-1A2B3C'. Nothing recorded.")
                return 2
            decision, ref = mt.group(1).upper(), mt.group(2).upper()
            reason = ""
            rb = re.search(r"Reason[^:\n]*:\s*(.*?)(?:\n---|\Z)", body or "", re.S | re.I)
            if rb:
                reason = " ".join(rb.group(1).split())[:300]
        else:
            decision, ref, reason = args.decision.upper(), args.id, args.reason
        try:
            row = repo.write_decision(ref, decision, reason)
        except (LookupError, ValueError) as exc:
            print(exc)
            return 2
        if decision == "PURSUE":
            opp = repo.to_opp(row)
            opp.analysis["proposal"] = ProposalAgent(config).run(opp, full=True)
            repo.save_results(opp)
            print(f"Proposal kit + checklist written to {opp.analysis['proposal']['folder']}")
        repo.commit()
        if not getattr(args, "no_site", False):
            SiteBuilder(config, repo).build()
        print(f"{decision} recorded for {row.short_id}: {row.title[:80]}")
        return 0

    if args.cmd == "list":
        rows = sorted(repo.all_rows(), key=lambda r: ({"top": 0, "review": 1, "watch": 2}.get(r.category, 3), -(r.score or 0)))
        for r in rows:
            if not args.category or r.category == args.category:
                _print_row(r)
        return 0

    if args.cmd == "show":
        row = repo.find(args.id)
        if not row:
            print("Not found.")
            return 2
        opp = repo.to_opp(row)
        if args.json:
            print(json.dumps(opp.to_dict(), indent=2, default=str))
            return 0
        c = card(opp, config, row)
        print(f"{c['id']}  {c['category']}  score {c['score']}\n{c['title']}\n")
        for k in ("service", "location", "government", "value"):
            print(f"{k.title():<14}{c[k]}")
        print(f"{'Deadline':<14}{c['deadline']['emoji']} {c['deadline']['date']} ({c['deadline']['label']})")
        print(f"{'Startup':<14}{c['startup']}\n{'Outsource':<14}{c['outsource']}")
        print(f"\nWhy: {c['why']}\nRisk: {c['risk']}\n")
        for n, t in enumerate(c["todo"], 1):
            print(f"  {n}. {t}")
        print(f"\nPrevious winner: {c['previous_winner']}\nPrevious award: {c['previous_award']}\nLink: {c['link']}")
        if row.filter_reason:
            print(f"Filter reason: {row.filter_reason}")
        return 0

    if args.cmd == "proposal":
        row = repo.find(args.id)
        if not row:
            print("Not found.")
            return 2
        opp = repo.to_opp(row)
        res = ProposalAgent(config, LLM(config)).run(opp, full=True)
        opp.analysis["proposal"] = res
        repo.save_results(opp)
        repo.commit()
        print(f"Proposal kit written to {res['folder']}")
        return 0

    if args.cmd == "build-site":
        print(SiteBuilder(config, repo).build())
        return 0

    if args.cmd == "test-email":
        last = repo.last_full_run()
        pipe = Pipeline(config, repo)
        report = pipe.build_report((last.stats if last else {}) or {}, [])
        if not EmailReport.is_meaningful(report):
            # show what the latest report looks like even if everything was already sent
            report["top"] = report["top"] or report["still_open"][:3]
        mailer = EmailReport(config)
        msg = mailer.build(report)
        msg["subject"] = "[TEST] " + msg["subject"]
        print(mailer.save_preview(msg))
        ok, status = mailer.send(msg)
        print(status)
        return 0 if ok else 1

    if args.cmd == "sources":
        scout = OpportunityScout(config)
        if not args.check:
            for c in scout.connectors:
                print(f"{c.key:<18} {c.market:<12} {c.name}\n{'':<31}{c.url}")
            return 0
        _, health = scout.run()
        for h in health:
            print(f"{h.status:<7} {h.count:>4}  {h.name}  {h.message}")
        return 0

    if args.cmd == "init-db":
        print(f"Database ready: {repo.url}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
