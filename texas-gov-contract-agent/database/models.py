"""Database tables. SQLite by default (data/contracts.db, committed back to the repo by the workflow).
Set DATABASE_URL to a Postgres URL if you'd rather keep data in a hosted database."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OpportunityRow(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    short_id: Mapped[str] = mapped_column(String(12), index=True)
    source: Mapped[str] = mapped_column(String(60), index=True)
    source_id: Mapped[str] = mapped_column(String(240))
    solicitation_number: Mapped[str] = mapped_column(String(160), default="", index=True)
    title: Mapped[str] = mapped_column(Text)
    agency: Mapped[str] = mapped_column(String(300), default="")
    office: Mapped[str] = mapped_column(String(300), default="")
    city: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(8), default="TX")
    zip: Mapped[str] = mapped_column(String(12), default="")
    market: Mapped[str] = mapped_column(String(40), default="", index=True)
    jurisdiction: Mapped[str] = mapped_column(String(20), default="")
    service_category: Mapped[str] = mapped_column(String(80), default="", index=True)
    naics: Mapped[str] = mapped_column(String(12), default="")
    psc: Mapped[str] = mapped_column(String(12), default="")
    notice_type: Mapped[str] = mapped_column(String(30), default="solicitation")
    procurement_type: Mapped[str] = mapped_column(String(120), default="")
    set_aside: Mapped[str] = mapped_column(String(200), default="")
    set_aside_code: Mapped[str] = mapped_column(String(20), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    doc_text: Mapped[str] = mapped_column(Text, default="")
    estimated_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_basis: Mapped[str] = mapped_column(String(200), default="")
    contract_length: Mapped[str] = mapped_column(String(120), default="")
    posted_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    place_of_performance: Mapped[str] = mapped_column(String(300), default="")
    certifications: Mapped[list] = mapped_column(JSON, default=list)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    # results
    award_info: Mapped[dict] = mapped_column(JSON, default=dict)
    winner: Mapped[str] = mapped_column(String(300), default="")
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    category: Mapped[str] = mapped_column(String(20), default="", index=True)
    filter_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)   # open | expired
    fully_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)

    # your decisions
    decision: Mapped[str] = mapped_column(String(10), default="")                 # PURSUE | WATCH | PASS
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    decision_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # bookkeeping
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    date_discovered: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    date_last_checked: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    date_last_changed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    changes: Mapped[list] = mapped_column(JSON, default=list)                     # pending change notes


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="scheduled")   # scheduled | manual | demo
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    source_health: Mapped[list] = mapped_column(JSON, default=list)
    discovery: Mapped[dict] = mapped_column(JSON, default=dict)
    emailed: Mapped[bool] = mapped_column(Boolean, default=False)
