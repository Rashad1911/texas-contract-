"""Offline tests: no network, no API keys. Run with `python -m pytest -q`."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from agents.opportunity_filter import OpportunityFilter
from agents.proposal_agent import ProposalAgent
from agents.red_team_agent import RedTeamAgent
from agents.requirements_agent import RequirementsAgent
from agents.scoring import Scorer
from agents.subcontracting_agent import SubcontractingAgent
from core import http
from core.config import Config
from core.extract import money_near, now, parse_money
from core.opportunity import Opportunity
from database.repository import Repository


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path)


def opp(**kw) -> Opportunity:
    base = dict(source="dallas_city", source_id=kw.pop("sid", "T1"), title="Janitorial Services", market="dfw",
                jurisdiction="city", agency="City of Dallas", response_deadline=now() + timedelta(days=20))
    base.update(kw)
    return Opportunity(**base)


# ── helpers ─────────────────────────────────────────────────────────────
def test_money_parsing():
    assert parse_money("$1,000,000") == 1_000_000
    assert parse_money("$2.5 million") == 2_500_000
    assert money_near("general liability of $1,000,000 per occurrence", ["general liability"])[0][0] == 1_000_000


# ── filter ──────────────────────────────────────────────────────────────
def test_filter_explains_rejections(config):
    f = OpportunityFilter(config)
    cases = [
        (opp(title="Grounds Maintenance", set_aside_code="SDVOSBC", jurisdiction="federal"), "SDVOSB"),
        (opp(title="Janitorial Supplies, Paper Products"), "goods purchase"),
        (opp(title="New Construction of Fire Station"), ""),
        (opp(title="Unarmed Security Guard Services", description="approximately 85 officers"), "large workforce"),
        (opp(title="Janitorial Services", response_deadline=now() - timedelta(days=1)), "passed"),
        (opp(title="Janitorial Services", market="", city="El Paso"), "outside your target markets"),
    ]
    for o, expect in cases:
        ok, reason = f.evaluate(o)
        assert not ok and reason, o.title
        assert expect.lower() in reason.lower(), (o.title, reason)


def test_specific_keyword_beats_generic(config):
    f = OpportunityFilter(config)
    assert f.classify(opp(title="Window Cleaning Services, State Office Buildings"))[0] == "window_cleaning"
    # specialty trades are excluded from general facility maintenance
    assert f.classify(opp(title="Elevator Maintenance and Repair Services"))[0] != "facility_maintenance"


def test_good_fit_passes(config):
    ok, reason = OpportunityFilter(config).evaluate(opp(title="Janitorial Services for Library Branches"))
    assert ok, reason


# ── subcontracting (never assume it's allowed) ──────────────────────────
@pytest.mark.parametrize("text,status", [
    ("The contractor shall self-perform all work; subcontracting is not permitted.", "RESTRICTED"),
    ("FAR 52.219-14 Limitations on Subcontracting applies.", "LIMITED"),
    ("Subcontractors require prior written approval of the County.", "APPROVAL_REQUIRED"),
    ("An M/WBE participation goal of 25% applies.", "SUPPORTED"),
    ("Nightly cleaning of three buildings.", "UNKNOWN"),
])
def test_subcontracting_status(config, text, status):
    o = opp(description=text, service_category="janitorial")
    assert SubcontractingAgent(config).run(o)["status"] == status


# ── red team + scoring ──────────────────────────────────────────────────
def _analyze(config, o):
    o.service_category = o.service_category or "janitorial"
    o.analysis["requirements"] = RequirementsAgent(config).run(o)
    o.analysis["subcontracting"] = SubcontractingAgent(config).run(o)
    o.analysis["award_history"] = {}
    o.analysis["red_team"] = RedTeamAgent(config).run(o)
    s = Scorer(config)
    score, _ = s.score(o)
    return s.categorize(o, score), score


def test_missed_mandatory_meeting_is_filtered(config):
    o = opp(title="Debris Removal", service_category="debris_removal",
            description="A mandatory pre-bid meeting was held on August 28, 2020; only firms that attended may bid.")
    cat, _ = _analyze(config, o)
    assert cat == "filtered"
    assert "already happened" in o.analysis["category_note"]


def test_short_listing_cannot_be_top(config):
    o = opp(title="Janitorial Services", description="Cleaning of a building. Subcontractors must comply with all terms.")
    cat, score = _analyze(config, o)
    assert cat != "top"


def test_insurance_extracted(config):
    o = opp(description="Contractor shall maintain commercial general liability insurance of $2,000,000 per occurrence.")
    req = RequirementsAgent(config).run(o)
    assert req["facts"]["general_liability"] == 2_000_000


# ── database: change tracking and decisions ─────────────────────────────
def test_repository_tracks_changes_and_decisions(tmp_path):
    repo = Repository(url=f"sqlite:///{tmp_path / 'db.sqlite'}", data_dir=tmp_path)
    o = opp(sid="R1", solicitation_number="IFB-100")
    _, is_new, _ = repo.upsert(o)
    repo.commit()
    assert is_new
    o2 = opp(sid="R1", solicitation_number="IFB-100", response_deadline=o.response_deadline + timedelta(days=7),
             attachments=[{"name": "Addendum 1.pdf", "url": ""}])
    repo.upsert(o2)
    o3 = opp(sid="R1", solicitation_number="IFB-100", response_deadline=o2.response_deadline,
             attachments=[{"name": "Addendum 1.pdf", "url": ""}, {"name": "Addendum 2.pdf", "url": ""}])
    _, _, changes = repo.upsert(o3)
    repo.commit()
    assert any("Addendum 2" in c for c in changes)
    row = repo.write_decision(o.short_id, "pursue", "good fit")
    assert row.decision == "PURSUE" and repo.find("IFB-100").decision == "PURSUE"
    assert "PURSUE" in (tmp_path / "decisions.yaml").read_text()
    with pytest.raises(LookupError):
        repo.write_decision("TX-000000", "PASS")


# ── connectors (offline, fake responses) ────────────────────────────────
class FakeResp:
    def __init__(self, payload=None, text="", status=200):
        self._payload, self.text, self.status_code = payload, text, status
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_bonfire_parser(config, monkeypatch):
    from connectors.bonfire import BonfireConnector
    payload = {"payload": {"projects": {"1": {"ProjectID": 555, "ProjectName": "Pressure Washing Services",
                                              "ReferenceID": "RFB-24-01", "DateClose": "2030-01-15 14:00:00",
                                              "DepartmentName": "Parks"}}}}
    monkeypatch.setattr(http, "get", lambda *a, **k: FakeResp(payload))
    conn = BonfireConnector(config.source("dallas_city"), config)
    out = conn.fetch()
    assert out[0].solicitation_number == "RFB-24-01" and out[0].market == "dfw"
    assert out[0].url.endswith("/opportunities/555")


def test_rss_parser_dedupes_calendar_repeats(config, monkeypatch):
    from connectors.generic import RssConnector
    xml = """<rss><channel>
      <item><title>Bid Alert! Event #1234 - Janitorial Services</title><link>https://example.org/e1</link>
            <description>Closes: January 15, 2030 2:00 PM</description></item>
      <item><title>Bid Alert! Event #1234 - Janitorial Services</title><link>https://example.org/e1b</link></item>
      <item><title>Commissioners Court</title><link>https://example.org/x</link></item>
    </channel></rss>"""
    monkeypatch.setattr(http, "get", lambda *a, **k: FakeResp(text=xml))
    out = RssConnector(config.source("bexar_county"), config).fetch()
    assert len(out) == 1 and out[0].title == "Janitorial Services" and out[0].response_deadline.year == 2030


def test_html_list_parser(config, monkeypatch):
    from connectors.generic import HtmlListConnector
    html = """<table><tr><td><a href="/bids/IFB-2030-01">IFB-2030-01 Custodial Services for City Hall</a></td>
              <td>Due 01/15/2030</td></tr><tr><td><a href="/contact">Contact</a></td></tr></table>"""
    monkeypatch.setattr(http, "get", lambda *a, **k: FakeResp(text=html))
    cfg = dict(config.source("dallas_county"))
    out = HtmlListConnector(cfg, config).fetch()
    assert len(out) == 1 and "Custodial" in out[0].title and out[0].response_deadline.year == 2030


def test_json_api_needs_setup_message(config):
    from connectors.base import SourceError
    from connectors.generic import JsonApiConnector
    with pytest.raises(SourceError, match="SETUP"):
        JsonApiConnector(config.source("texas_esbd"), config).fetch()


def test_sam_record_mapping(config):
    from connectors.sam import SamConnector
    conn = SamConnector(config.source("sam"), config)
    rec = {"noticeId": "abc123", "title": "Janitorial Services, VA Clinic", "solicitationNumber": "36C257-26-Q-0001",
           "fullParentPathName": "VETERANS AFFAIRS, DEPARTMENT OF.VETERANS AFFAIRS, DEPARTMENT OF.NCO 17",
           "naicsCode": "561720", "typeOfSetAside": "SBA", "type": "Combined Synopsis/Solicitation",
           "responseDeadLine": "2030-01-15T14:00:00-06:00", "postedDate": "2029-12-01",
           "placeOfPerformance": {"city": {"name": "FORT WORTH"}, "state": {"code": "TX"}, "zip": "76104"},
           "uiLink": "https://sam.gov/opp/abc123/view"}
    o = conn._to_opp(rec)
    assert o.market == "dfw" and o.set_aside_code == "SBA" and o.notice_type == "solicitation"
    rec["placeOfPerformance"]["state"]["code"] = "OK"
    assert conn._to_opp(rec) is None              # outside Texas


# ── proposal drafts ─────────────────────────────────────────────────────
def test_proposal_kit_is_signed_and_flags_review(config):
    o = opp(description="An M/WBE participation goal applies. General liability of $1,000,000.")
    _analyze(config, o)
    kit = ProposalAgent(config).run(o, full=True, write_files=False)
    assert "Rashad" in kit["drafts"]["co_questions"]["text"]
    assert "review" in kit["drafts"]["vendor_agreement_outline"]["review"].lower()
    assert kit["drafts"]["pricing_worksheet"]["text"].startswith("Line item")


# ── full demo run: pipeline, email, dashboard ───────────────────────────
def test_demo_end_to_end(tmp_path):
    from main import run_demo
    res = run_demo(tmp_path / "demo")
    stats = res["second"]["stats"]
    assert stats["found"] == 21
    assert 1 <= stats["top"] <= 5, stats
    assert stats["filtered"] >= 6
    page = Path(res["site"]).read_text()
    assert "__DATA__" not in page and "DEMO-DAL-JAN-0412" in page
    first_email = Path(res["email"]).read_text()
    assert "TOP OPPORTUNITIES" in first_email and "SAMPLE" in first_email
    followup = Path(res["followup_email"]).read_text()
    assert "Deadline moved" in followup                     # change tracking reaches the email
    data = json.loads((tmp_path / "demo" / "site" / "data.json").read_text())
    assert any(o["decision"] == "PURSUE" and o.get("drafts") for o in data["opportunities"])
    assert len(data["vehicles"]["leads"]) == 3 and stats["teaming_leads"] == 3
    by_title = {o["title"]: o for o in data["opportunities"]}
    matoc = next(o for t, o in by_title.items() if "MATOC" in t)
    assert matoc["category"] in ("top", "review") and "not guaranteed" in matoc["value"]
    task_order = next(o for t, o in by_title.items() if t.startswith("Task Order Request"))
    assert task_order["category"] == "filtered" and "holders" in task_order["filter_reason"]
    assert next(o for t, o in by_title.items() if t.startswith("Recompete"))["category"] == "watch"


def test_dashboard_escapes_script_tags(tmp_path, config):
    from reports.site_builder import SiteBuilder
    repo = Repository(url=f"sqlite:///{tmp_path / 'db.sqlite'}", data_dir=tmp_path)
    o = opp(sid="X1", title="Bad </script><script>alert(1)</script> title")
    o.category = "watch"
    repo.save_results(o)
    repo.commit()
    page = SiteBuilder(config, repo).build(tmp_path / "site").read_text()
    assert "</script><script>alert(1)" not in page


def test_email_without_top_items(config):
    from reports.email_report import EmailReport
    report = {"date": now().date(), "stats": {"found": 3}, "top": [], "review": [], "updates": [], "still_open": [],
              "watch_best": None, "site_url": "", "source_problems": 0, "is_demo": False}
    assert not EmailReport.is_meaningful(report)
    msg = EmailReport(config).build(report)
    assert "No strong opportunities found this cycle." in msg["text"]
    assert msg["subject"].startswith("Texas Government Contract Report — ")


def test_decide_from_issue_title_parsing(tmp_path, monkeypatch):
    import main as m
    repo = Repository(url=f"sqlite:///{tmp_path / 'db.sqlite'}", data_dir=tmp_path)
    o = opp(sid="I1")
    repo.upsert(o)
    repo.commit()
    monkeypatch.setattr(m, "Repository", lambda *a, **k: repo)
    monkeypatch.setattr(m.SiteBuilder, "build", lambda self, *a, **k: None)
    rc = m.main(["decide-from-issue", "--title", f"WATCH {o.short_id}", "--body", "Reason (optional, one line):\nwaiting on quotes\n---\nx"])
    assert rc == 0
    assert repo.find(o.short_id).decision == "WATCH"
    assert repo.find(o.short_id).decision_reason == "waiting on quotes"
    assert m.main(["decide-from-issue", "--title", "hello"]) == 2


# ── contract vehicles: IDIQ / MATOC / BPA / task orders ─────────────────
MATOC_TEXT = ("The District intends to award up to five (5) multiple-award indefinite-delivery, indefinite-quantity (IDIQ) "
              "contracts for janitorial services, with a five-year ordering period. Each awardee is guaranteed a minimum of "
              "$2,500. The shared program ceiling is $24,000,000. Task orders will be competed among awardees.")


def test_vehicle_detection_reads_idiq_terms():
    from core.vehicles import detect, value_label
    from core.extract import fmt_money
    v = detect(MATOC_TEXT, "Janitorial Services MATOC")
    assert v["type"] == "matoc" and not v["holders_only"]
    assert (v["awards"], v["min_guarantee"], v["ceiling"], v["ordering_years"]) == (5, 2500, 24_000_000, 5)
    assert value_label(v, fmt_money).startswith("Ceiling $24.0M")
    assert detect("Nightly cleaning of the library.", "Janitorial Services") == {}


def test_idiq_ceiling_is_not_treated_as_value(config):
    o = opp(title="Janitorial Services MATOC", description=MATOC_TEXT + " Not to exceed $24,000,000.",
            jurisdiction="federal", set_aside_code="SBA")
    ok, reason = OpportunityFilter(config).evaluate(o)
    assert ok, reason
    assert o.estimated_value is None                      # the $24M ceiling is not revenue


def test_task_order_under_existing_vehicle_is_explained(config):
    o = opp(title="Task Order Request: Custodial Services", jurisdiction="federal",
            description="This task order request is issued under the Region 7 Custodial BPA. Only current BPA holders may respond.")
    ok, reason = OpportunityFilter(config).evaluate(o)
    assert not ok and "current holders" in reason
    config.settings["company"]["vehicles_held"] = ["Region 7 Custodial BPA"]
    ok, _ = OpportunityFilter(config).evaluate(opp(title=o.title, description=o.description, jurisdiction="federal"))
    assert ok                                             # you hold it, so you can respond


def test_idv_connector_leads_and_recompetes(config, monkeypatch):
    from connectors.vehicles import IdvConnector
    soon = (now() + timedelta(days=200)).strftime("%Y-%m-%d")
    later = (now() + timedelta(days=1500)).strftime("%Y-%m-%d")
    past = (now() - timedelta(days=30)).strftime("%Y-%m-%d")
    rows = [{"Award ID": "A1", "Recipient Name": "ACME CLEANING LLC", "Last Date to Order": soon, "Award Amount": 900000,
             "Description": "JANITORIAL SERVICES BPA", "Awarding Agency": "Department of Veterans Affairs",
             "Contract Award Type": "BPA", "Primary Place of Performance": {"city_name": "DALLAS"}, "generated_internal_id": "X1"},
            {"Award ID": "A2", "Recipient Name": "BIG GROUNDS INC", "Last Date to Order": later, "Award Amount": 5e6,
             "Description": "GROUNDS IDIQ", "Awarding Agency": "Department of Defense", "Contract Award Type": "IDC"},
            {"Award ID": "A3", "Recipient Name": "OLD CO", "Last Date to Order": past, "Award Amount": 1}]
    monkeypatch.setattr(http, "post", lambda *a, **k: FakeResp({"results": rows}))
    conn = IdvConnector(config.source("usaspending_idv"), config)
    forecasts = conn.fetch()
    assert [l["holder"] for l in conn.leads] == ["Acme Cleaning Llc", "Big Grounds Inc"]      # expired one dropped
    assert len(forecasts) == 1 and forecasts[0].notice_type == "forecast" and forecasts[0].market == "dfw"


def test_sam_award_notice_becomes_teaming_lead(config):
    from connectors.sam import SamConnector
    conn = SamConnector(config.source("sam"), config)
    rec = {"noticeId": "aw1", "type": "Award Notice", "title": "Janitorial Services IDIQ, Houston Federal Buildings",
           "naicsCode": "561720", "fullParentPathName": "GENERAL SERVICES ADMINISTRATION.PBS",
           "placeOfPerformance": {"state": {"code": "TX"}, "city": {"name": "HOUSTON"}},
           "award": {"awardee": {"name": "Gulf Coast Building Services Inc."}, "amount": "450000", "number": "47PH-26-D-0004"}}
    lead = conn._award_lead(rec)
    assert lead["holder"] == "Gulf Coast Building Services Inc." and lead["amount"] == 450000
    rec["placeOfPerformance"]["state"]["code"] = "CA"
    assert conn._award_lead(rec) is None
