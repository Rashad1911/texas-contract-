"""The normalized opportunity record every connector produces and every agent reads."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .extract import parse_date


@dataclass
class Opportunity:
    source: str
    source_id: str
    title: str
    url: str = ""
    solicitation_number: str = ""
    agency: str = ""
    office: str = ""
    city: str = ""
    state: str = "TX"
    zip: str = ""
    county: str = ""
    market: str = ""
    jurisdiction: str = ""            # federal | state | county | city | other
    service_category: str = ""
    naics: str = ""
    psc: str = ""
    description: str = ""
    doc_text: str = ""                # text pulled from attachments (truncated)
    estimated_value: float | None = None
    value_basis: str = ""             # where the value came from
    contract_length: str = ""
    posted_date: datetime | None = None
    response_deadline: datetime | None = None
    set_aside: str = ""
    set_aside_code: str = ""
    certifications: list = field(default_factory=list)
    place_of_performance: str = ""
    attachments: list = field(default_factory=list)   # [{"name": str, "url": str}]
    procurement_type: str = ""        # IFB, RFP, RFQ, Solicitation …
    notice_type: str = "solicitation" # solicitation | presolicitation | sources_sought | special | forecast | award
    raw: dict = field(default_factory=dict)
    # results written by the agents
    analysis: dict = field(default_factory=dict)
    score: float | None = None
    category: str = ""                # top | review | watch | filtered | expired
    filter_reason: str = ""

    # ── identity ──────────────────────────────────────────────────────
    @property
    def uid(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def short_id(self) -> str:
        return "TX-" + hashlib.sha1(self.uid.encode()).hexdigest()[:6].upper()

    def text(self, include_docs: bool = True) -> str:
        parts = [self.title, self.description]
        if include_docs and self.doc_text:
            parts.append(self.doc_text)
        return "\n".join(p for p in parts if p)

    def fingerprint(self) -> str:
        """Changes when something a bidder cares about changes (deadline, scope, attachments)."""
        deadline = self.response_deadline.isoformat() if self.response_deadline else ""
        payload = "|".join([
            self.title.strip().lower(),
            deadline,
            self.set_aside,
            hashlib.sha1((self.description or "").encode()).hexdigest()[:12],
            ",".join(sorted(a.get("name", "") for a in self.attachments)),
            str(self.estimated_value or ""),
        ])
        return hashlib.sha1(payload.encode()).hexdigest()

    # ── serialization ────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("posted_date", "response_deadline"):
            if data[key]:
                data[key] = data[key].isoformat()
        data["uid"] = self.uid
        data["short_id"] = self.short_id
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Opportunity":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known}
        for key in ("posted_date", "response_deadline"):
            if isinstance(clean.get(key), str):
                clean[key] = parse_date(clean[key])
        return cls(**clean)
