"""Turns an analyzed opportunity into the short 'card' (email) and the full record (dashboard).

One place for labels and wording so the email and the dashboard always agree.
"""
from __future__ import annotations

from core import vehicles as veh
from core.config import Config
from core.extract import LOCAL_TZ, days_until, fmt_money

CATEGORY_LABELS = {"top": "🔥 Top opportunity", "review": "🟡 Worth reviewing", "watch": "⚪ Watchlist",
                   "filtered": "❌ Filtered out", "expired": "Closed"}
NOTICE_LABELS = {"solicitation": "Solicitation (biddable)", "presolicitation": "Pre-solicitation",
                 "sources_sought": "Sources sought", "special": "Special notice", "forecast": "Forecast",
                 "award": "Award notice"}
SUB_LABELS = {"SUPPORTED": "Supported by the text", "APPROVAL_REQUIRED": "Allowed with written approval",
              "LIMITED": "Limited (percentage cap)", "RESTRICTED": "Restricted — self-perform",
              "UNKNOWN": "Not addressed — ask"}


def deadline_info(dt) -> dict:
    days = days_until(dt)
    if dt is None:
        return {"flag": "none", "emoji": "⚪", "label": "No deadline listed", "date": "Not listed", "days": None}
    local = dt.astimezone(LOCAL_TZ)
    hour = local.strftime("%I:%M %p").lstrip("0")
    date = f"{local:%a %b} {local.day}, {local.year}, {hour} CT"
    if days < 0:
        return {"flag": "expired", "emoji": "⚫", "label": "Closed", "date": date, "days": round(days, 1)}
    whole = int(days)
    label = "Due today" if whole == 0 else f"{whole} day{'s' if whole != 1 else ''} left"
    flag, emoji = ("red", "🔴") if days <= 7 else ("orange", "🟠") if days <= 14 else ("green", "🟢")
    return {"flag": flag, "emoji": emoji, "label": label, "date": date, "days": round(days, 1)}


def location(opp, config: Config) -> str:
    if opp.city:
        return f"{opp.city.title() if opp.city.isupper() else opp.city}, {opp.state or 'TX'}"
    if opp.place_of_performance:
        return opp.place_of_performance
    return config.market_label(opp.market) or "Not listed"


def award_labels(awards: dict) -> tuple[str, str]:
    winner = awards.get("previous_winner") or ""
    if awards.get("previous_award"):
        amount = fmt_money(awards["previous_award"])
    elif awards.get("median_similar_award"):
        amount = f"≈ {fmt_money(awards['median_similar_award'])} (median of similar awards)"
    else:
        amount = "Not found"
    if not winner:
        if awards.get("local_matches"):
            winner = "See the past solicitation (link in full analysis)"
        elif awards.get("awards_url"):
            winner = "Not published online — check bid tabulations"
        else:
            winner = "Not found"
    return winner, amount


def value_label(opp, awards: dict) -> str:
    vehicle = (opp.analysis or {}).get("vehicle") or {}
    if vehicle:
        label = veh.value_label(vehicle, fmt_money)
        if label:
            return label
    if opp.estimated_value:
        return fmt_money(opp.estimated_value)
    if awards.get("previous_award"):
        return f"Not listed (last award {fmt_money(awards['previous_award'])})"
    return "Not listed"


def card(opp, config: Config, row=None) -> dict:
    """Short fields for the email."""
    a = opp.analysis or {}
    strat = a.get("bid_strategy", {})
    awards = a.get("award_history", {})
    winner, amount = award_labels(awards)
    dl = deadline_info(opp.response_deadline)
    return {
        "id": opp.short_id,
        "title": opp.title,
        "service": config.category_label(opp.service_category),
        "location": location(opp, config),
        "government": opp.agency or "Not listed",
        "value": value_label(opp, awards),
        "deadline": dl,
        "startup": strat.get("startup_level", "—"),
        "outsource": strat.get("outsource_level", "—"),
        "why": strat.get("why_it_made_the_list", ""),
        "risk": strat.get("biggest_risk") or a.get("red_team", {}).get("biggest_risk", ""),
        "todo": (strat.get("todo_top3") or [])[:3],
        "previous_winner": winner,
        "previous_award": amount,
        "link": opp.url,
        "category": opp.category,
        "score": opp.score,
        "notice": NOTICE_LABELS.get(opp.notice_type, opp.notice_type),
        "vehicle": ((a.get("vehicle") or {}).get("label") or ""),
        "changes": [n for c in (getattr(row, "changes", None) or []) for n in c.get("notes", [])],
        "decision": getattr(row, "decision", "") or "",
        "filter_reason": opp.filter_reason,
    }


def record(opp, row, config: Config, include_drafts: bool = True, light: bool = False) -> dict:
    """Everything the dashboard shows for one opportunity."""
    a = opp.analysis or {}
    c = card(opp, config, row)
    src = config.source(opp.source)
    rec = {
        **{k: c[k] for k in ("id", "title", "service", "location", "government", "value", "startup", "outsource",
                             "why", "risk", "todo", "previous_winner", "previous_award", "category", "score",
                             "notice", "changes", "decision", "filter_reason", "vehicle")},
        "deadline": c["deadline"],
        "deadline_iso": opp.response_deadline.isoformat() if opp.response_deadline else "",
        "url": opp.url,
        "service_key": opp.service_category or "",
        "market": opp.market or "",
        "market_label": config.market_label(opp.market) if opp.market else "Outside target markets",
        "jurisdiction": opp.jurisdiction,
        "office": opp.office,
        "solicitation_number": opp.solicitation_number,
        "source_name": src.get("name", opp.source),
        "naics": opp.naics,
        "set_aside": opp.set_aside,
        "procurement_type": opp.procurement_type,
        "value_basis": opp.value_basis,
        "contract_length": opp.contract_length,
        "place_of_performance": opp.place_of_performance,
        "posted": opp.posted_date.astimezone(LOCAL_TZ).strftime("%b %d, %Y") if opp.posted_date else "",
        "discovered": row.date_discovered.astimezone(LOCAL_TZ).strftime("%b %d, %Y") if row and row.date_discovered else "",
        "last_checked": row.date_last_checked.astimezone(LOCAL_TZ).strftime("%b %d, %Y") if row and row.date_last_checked else "",
        "status": getattr(row, "status", "open"),
        "decision_reason": getattr(row, "decision_reason", "") or "",
        "decision_date": row.decision_date.astimezone(LOCAL_TZ).strftime("%b %d, %Y") if row and row.decision_date else "",
        "fully_reviewed": bool(getattr(row, "fully_reviewed", False)),
        "discovered_iso": row.date_discovered.isoformat() if row and row.date_discovered else "",
        "value_num": opp.estimated_value or (a.get("award_history") or {}).get("previous_award") or None,
        "summary": _summary(opp),
    }
    if light:
        return rec
    awards = a.get("award_history", {})
    sub = a.get("subcontracting", {})
    red = a.get("red_team", {})
    req = a.get("requirements", {})
    from agents.scoring import FACTOR_LABELS
    rec.update({
        "attachments": (opp.attachments or [])[:12],
        "requirements": {k: req.get(k, []) for k in ("must_have", "nice_to_have", "potential_problems", "to_confirm")},
        "subcontracting": {"status": sub.get("status", ""), "status_label": SUB_LABELS.get(sub.get("status", ""), ""),
                           "explanation": sub.get("explanation", ""), "model": sub.get("model", ""),
                           "findings": sub.get("findings", []), "questions": sub.get("questions", []),
                           "references": [r for r in sub.get("references", []) if r]},
        "strategy": {k: v for k, v in (a.get("bid_strategy") or {}).items()
                     if k not in ("why_it_made_the_list", "biggest_risk", "todo_top3")},
        "red_team": {"verdict": red.get("verdict", ""), "findings": red.get("findings", [])},
        "awards": {"summary": awards.get("summary", ""), "exact": (awards.get("exact") or [])[:3],
                   "similar": (awards.get("similar") or [])[:6], "local_matches": (awards.get("local_matches") or [])[:4],
                   "awards_url": awards.get("awards_url", ""), "sources": awards.get("sources", []),
                   "contract_period": awards.get("contract_period", ""),
                   "distinct_winners": awards.get("distinct_winners", [])},
        "factors": [{"key": k, "label": FACTOR_LABELS.get(k, k), "value": v}
                    for k, v in sorted((a.get("factors") or {}).items(), key=lambda kv: -kv[1])],
        "references": (a.get("proposal") or {}).get("references", []),
    })
    if include_drafts:
        rec["drafts"] = [{"key": k, **v} for k, v in ((a.get("proposal") or {}).get("drafts") or {}).items()]
    return rec


def _summary(opp) -> str:
    text = (opp.description or "").strip()
    if not text:
        return ""
    cut = text[:420]
    if len(text) > 420:
        cut = cut.rsplit(" ", 1)[0] + "…"
    return cut
