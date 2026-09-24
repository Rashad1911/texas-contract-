"""All reads/writes to the database go through here."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session

from core.config import DATA_DIR, env
from core.extract import now
from core.opportunity import Opportunity

from .models import Base, OpportunityRow, RunRow

UPDATABLE = ["title", "url", "solicitation_number", "agency", "office", "city", "state", "zip", "market",
             "jurisdiction", "naics", "psc", "notice_type", "procurement_type", "set_aside", "set_aside_code",
             "posted_date", "response_deadline", "place_of_performance", "contract_length"]


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return None if dt is None else (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt)


class Repository:
    def __init__(self, url: str | None = None, data_dir: Path | None = None):
        data_dir = data_dir or DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.url = url or env("DATABASE_URL") or f"sqlite:///{data_dir / 'contracts.db'}"
        self.engine = create_engine(self.url, future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)

    # ── opportunities ────────────────────────────────────────────────
    def get(self, uid: str) -> OpportunityRow | None:
        return self.session.scalar(select(OpportunityRow).where(OpportunityRow.uid == uid))

    def find(self, ref: str) -> OpportunityRow | None:
        ref = (ref or "").strip()
        return self.session.scalar(select(OpportunityRow).where(or_(
            OpportunityRow.uid == ref, OpportunityRow.short_id == ref.upper(),
            OpportunityRow.solicitation_number == ref)))

    def upsert(self, opp: Opportunity) -> tuple[OpportunityRow, bool, list[str]]:
        """Insert or update. Returns (row, is_new, list_of_change_notes)."""
        ts = now()
        row = self.get(opp.uid)
        if row is None:
            row = OpportunityRow(uid=opp.uid, short_id=opp.short_id, source=opp.source, source_id=opp.source_id,
                                 date_discovered=_utc(ts), date_last_checked=_utc(ts), fingerprint=opp.fingerprint(),
                                 status="open", changes=[], analysis={}, raw={}, award_info={})
            self._copy_in(row, opp)
            self.session.add(row)
            return row, True, []

        changes = []
        old_deadline = _aware(row.response_deadline)
        if opp.response_deadline and old_deadline and abs((_utc(opp.response_deadline) - old_deadline).total_seconds()) > 60:
            changes.append(f"Deadline moved from {old_deadline.astimezone(opp.response_deadline.tzinfo):%b %d, %I:%M %p} "
                           f"to {opp.response_deadline:%b %d, %I:%M %p}")
        old_names = {a.get("name") for a in (row.attachments or [])}
        new_names = {a.get("name") for a in opp.attachments}
        added = [n for n in new_names - old_names if n]
        if added and old_names:
            changes.append(f"New attachment(s): {', '.join(sorted(added)[:4])}")
        if opp.title and opp.title.strip() != (row.title or "").strip():
            changes.append("Title changed")
        if opp.set_aside != (row.set_aside or "") and row.set_aside:
            changes.append(f"Set-aside changed to {opp.set_aside or 'none'}")

        new_fp = opp.fingerprint()
        self._copy_in(row, opp, keep_longer_text=True)
        row.date_last_checked = _utc(ts)
        if changes or (new_fp != row.fingerprint and row.fingerprint):
            if not changes:
                changes.append("Solicitation details were updated")
            row.changes = list(row.changes or []) + [{"date": ts.isoformat(), "notes": changes}]
            row.date_last_changed = _utc(ts)
            row.fully_reviewed = False               # re-run the agents on the new version
        row.fingerprint = new_fp
        if row.status == "expired" and opp.response_deadline and opp.response_deadline > ts:
            row.status = "open"
        return row, False, changes

    def _copy_in(self, row: OpportunityRow, opp: Opportunity, keep_longer_text: bool = False):
        for f in UPDATABLE:
            value = getattr(opp, f)
            if value in (None, "") and keep_longer_text:
                continue
            if f in ("posted_date", "response_deadline"):
                value = _utc(value)
            setattr(row, f, value if value is not None else getattr(row, f))
        if not keep_longer_text or len(opp.description or "") > len(row.description or ""):
            row.description = opp.description or row.description or ""
        if opp.doc_text and len(opp.doc_text) > len(row.doc_text or ""):
            row.doc_text = opp.doc_text
        if opp.attachments:
            row.attachments = opp.attachments
        if opp.estimated_value:
            row.estimated_value = opp.estimated_value
            row.value_basis = opp.value_basis or row.value_basis
        row.certifications = opp.certifications or row.certifications or []
        row.raw = {**(row.raw or {}), **(opp.raw or {})}

    def to_opp(self, row: OpportunityRow) -> Opportunity:
        return Opportunity(
            source=row.source, source_id=row.source_id, title=row.title, url=row.url,
            solicitation_number=row.solicitation_number, agency=row.agency, office=row.office, city=row.city,
            state=row.state, zip=row.zip, market=row.market, jurisdiction=row.jurisdiction,
            service_category=row.service_category, naics=row.naics, psc=row.psc, description=row.description,
            doc_text=row.doc_text, estimated_value=row.estimated_value, value_basis=row.value_basis,
            contract_length=row.contract_length, posted_date=_aware(row.posted_date),
            response_deadline=_aware(row.response_deadline), set_aside=row.set_aside,
            set_aside_code=row.set_aside_code, certifications=row.certifications or [],
            place_of_performance=row.place_of_performance, attachments=row.attachments or [],
            procurement_type=row.procurement_type, notice_type=row.notice_type, raw=row.raw or {},
            analysis=dict(row.analysis or {}), score=row.score, category=row.category,
            filter_reason=row.filter_reason,
        )

    def save_results(self, opp: Opportunity, fully_reviewed: bool | None = None):
        row = self.get(opp.uid)
        if row is None:
            row, _, _ = self.upsert(opp)
        self._copy_in(row, opp, keep_longer_text=True)
        row.service_category = opp.service_category
        row.analysis = opp.analysis
        row.score = opp.score
        row.category = opp.category
        row.filter_reason = opp.filter_reason
        awards = opp.analysis.get("award_history") or {}
        row.award_info = awards
        row.winner = awards.get("previous_winner") or row.winner or ""
        if fully_reviewed is not None:
            row.fully_reviewed = fully_reviewed
            if fully_reviewed:
                row.reviewed_at = _utc(now())
        return row

    def expire_past_deadlines(self) -> int:
        count = 0
        cutoff = _utc(now())
        for row in self.session.scalars(select(OpportunityRow).where(OpportunityRow.status == "open")):
            deadline = _aware(row.response_deadline)
            if deadline and deadline < cutoff:
                row.status = "expired"
                row.category = "expired"
                count += 1
        return count

    def prune(self, keep_days: int = 120) -> int:
        """Keep the database small (it's committed to git every run): drop document text we no longer need
        and delete long-closed items you never made a decision on."""
        cutoff = _utc(now()) - timedelta(days=keep_days)
        removed = 0
        for row in list(self.session.scalars(select(OpportunityRow))):
            if row.category == "filtered" or row.status == "expired":
                if row.doc_text:
                    row.doc_text = ""
                if row.raw and len(str(row.raw)) > 4000:
                    row.raw = {k: v for k, v in row.raw.items() if k in ("demo", "award_history_seed", "pop_missing")}
            deadline = _aware(row.response_deadline) or _aware(row.date_last_checked)
            if row.status == "expired" and not row.decision and deadline and deadline < cutoff:
                self.session.delete(row)
                removed += 1
        self.session.commit()
        if self.url.startswith("sqlite"):
            with self.engine.connect() as conn:
                conn.exec_driver_sql("VACUUM")
        return removed

    def open_rows(self) -> list[OpportunityRow]:
        return list(self.session.scalars(select(OpportunityRow).where(OpportunityRow.status == "open")))

    def all_rows(self) -> list[OpportunityRow]:
        return list(self.session.scalars(select(OpportunityRow)))

    def mark_emailed(self, uids: list[str]):
        ts = _utc(now())
        for uid in uids:
            row = self.get(uid)
            if row:
                row.last_emailed_at = ts

    def clear_changes(self, uids: list[str]):
        for uid in uids:
            row = self.get(uid)
            if row:
                row.changes = []

    # ── decisions (PURSUE / WATCH / PASS) ────────────────────────────
    @property
    def decisions_path(self) -> Path:
        return self.data_dir / "decisions.yaml"

    def load_decisions(self) -> list[dict]:
        if not self.decisions_path.exists():
            return []
        data = yaml.safe_load(self.decisions_path.read_text(encoding="utf-8")) or {}
        return data.get("decisions") or []

    def write_decision(self, ref: str, decision: str, reason: str = "") -> OpportunityRow:
        decision = decision.upper().strip()
        if decision not in ("PURSUE", "WATCH", "PASS"):
            raise ValueError("Decision must be PURSUE, WATCH, or PASS")
        row = self.find(ref)
        if row is None:
            raise LookupError(f"No opportunity matches '{ref}'. Use the TX-XXXXXX code from your email or dashboard.")
        entries = [d for d in self.load_decisions() if d.get("id") not in (row.uid, row.short_id)]
        entries.append({"id": row.short_id, "uid": row.uid, "decision": decision, "reason": reason,
                        "date": now().date().isoformat(), "title": row.title[:120]})
        header = ("# Your decisions. Edit by hand, run `python main.py mark TX-XXXXXX pursue`,\n"
                  "# or tap Pursue / Watch / Pass on the dashboard (owner mode). See README §9.\n")
        self.decisions_path.write_text(header + yaml.safe_dump({"decisions": entries}, sort_keys=False,
                                                               allow_unicode=True), encoding="utf-8")
        self._apply(row, decision, reason, now())
        return row

    def sync_decisions(self) -> int:
        n = 0
        for d in self.load_decisions():
            row = self.find(d.get("uid") or "") or self.find(d.get("id") or "")
            if row:
                self._apply(row, d.get("decision", ""), d.get("reason", ""), d.get("date"))
                n += 1
        return n

    @staticmethod
    def _apply(row: OpportunityRow, decision: str, reason: str, when):
        row.decision = (decision or "").upper()
        row.decision_reason = reason or ""
        if isinstance(when, str):
            try:
                when = datetime.fromisoformat(when)
                if len(str(when)) and when.tzinfo is None and when.hour == 0 and when.minute == 0:
                    when = when.replace(hour=12, tzinfo=now().tzinfo)   # a bare date means that day, Central time
            except ValueError:
                when = None
        row.decision_date = _utc(when) if isinstance(when, datetime) else _utc(now())

    # ── runs ─────────────────────────────────────────────────────────
    def last_full_run(self) -> RunRow | None:
        return self.session.scalar(select(RunRow).where(RunRow.finished_at.is_not(None),
                                                        RunRow.kind != "demo")
                                   .order_by(RunRow.started_at.desc()))

    def due(self, every_days: float) -> bool:
        last = self.last_full_run()
        if not last:
            return True
        # 2-hour grace so a daily cron at the same hour doesn't miss the 3-day mark by minutes
        return _utc(now()) - _aware(last.started_at) >= timedelta(days=every_days) - timedelta(hours=2)

    def start_run(self, kind: str) -> RunRow:
        run = RunRow(started_at=_utc(now()), kind=kind, stats={}, source_health=[], discovery={})
        self.session.add(run)
        self.commit()
        return run

    def recent_runs(self, n: int = 10) -> list[RunRow]:
        return list(self.session.scalars(select(RunRow).order_by(RunRow.started_at.desc()).limit(n)))

    def commit(self):
        self.session.commit()
