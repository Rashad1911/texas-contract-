"""Builds the shareable dashboard: one self-contained HTML page (docs/index.html) plus docs/data.json.

GitHub Pages serves /docs from the main branch, so after every run the workflow commits the rebuilt page
and your link (https://<you>.github.io/<repo>/) is always current. Share it with anyone — it's read-only.
Owner mode (add ?owner=1 to the URL once on your own phone) shows Pursue / Watch / Pass buttons that
record your decision through a GitHub issue (README §9).
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from core.config import ROOT, SITE_DIR, Config, env
from core.extract import LOCAL_TZ, now

from .cards import record

TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


def site_url(config: Config) -> str:
    url = env("SITE_URL") or config.get("site.url", "")
    if url:
        return url.rstrip("/") + "/"
    repo = repo_slug(config)
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def repo_slug(config: Config) -> str:
    return env("GITHUB_REPOSITORY") or config.get("site.repo", "") or ""


class SiteBuilder:
    def __init__(self, config: Config, repo):
        self.config = config
        self.repo = repo

    def build(self, out_dir: Path | None = None, is_demo: bool = False) -> Path:
        out_dir = Path(out_dir or SITE_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        data = self.data(is_demo=is_demo)
        payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
        safe = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
        page = TEMPLATE.read_text(encoding="utf-8")
        page = page.replace("__PAGE_TITLE__", _html(data["meta"]["title"]))
        robots = "index, follow" if self.config.get("site.allow_search_engines", False) else "noindex, nofollow"
        page = page.replace("__ROBOTS__", robots)
        page = page.replace("/*__DATA__*/null", safe)
        (out_dir / "index.html").write_text(page, encoding="utf-8")
        (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, default=str, indent=1), encoding="utf-8")
        (out_dir / ".nojekyll").write_text("", encoding="utf-8")
        return out_dir / "index.html"

    def data(self, is_demo: bool = False) -> dict:
        include_filtered = self.config.get("site.include_filtered", True)
        include_drafts = self.config.get("site.include_drafts", True)
        show_decisions = self.config.get("site.show_decisions", True)
        cutoff = now() - timedelta(days=60)
        records = []
        for row in self.repo.all_rows():
            opp = self.repo.to_opp(row)
            cat = row.category or ("expired" if row.status == "expired" else "")
            if row.status == "expired":
                deadline = opp.response_deadline
                if not row.decision and (deadline is None or deadline < cutoff):
                    continue
                opp.category = "expired"
                rec = record(opp, row, self.config, include_drafts, light=not row.decision)
            elif cat in ("top", "review", "watch"):
                rec = record(opp, row, self.config, include_drafts)
            elif cat == "filtered" and include_filtered:
                rec = record(opp, row, self.config, False, light=not row.analysis.get("red_team"))
            else:
                continue
            if not show_decisions:
                rec["decision"], rec["decision_reason"] = "", ""
            records.append(rec)

        runs = [r for r in self.repo.recent_runs(20) if r.finished_at]
        last = runs[0] if runs else None
        every = float(self.config.get("schedule.run_every_days", 3))
        next_run = ""
        if last and not is_demo:
            nxt = last.started_at.astimezone(LOCAL_TZ) + timedelta(days=every)
            next_run = f"{nxt:%b} {nxt.day}"
        generated = now()
        company = self.config.company
        return {
            "meta": {
                "title": self.config.get("site.title", "Texas Contract Scout"),
                "company": company.get("dba") or company.get("legal_name", ""),
                "legal_name": company.get("legal_name", ""),
                "generated_at": generated.isoformat(),
                "generated_label": f"{generated:%b} {generated.day}, {generated.year}",
                "site_url": site_url(self.config),
                "repo": repo_slug(self.config),
                "is_demo": is_demo,
                "run_every_days": every,
                "next_run": next_run,
                "markets": {k: self.config.market_label(k) for k in list(self.config.markets) + ["statewide", "unknown"]},
                "services": {k: v.get("label", k) for k, v in self.config.service_categories().items()},
                "source_count": len(self.config.source_entries()),
                "thresholds": self.config.get("scoring.thresholds", {}),
            },
            "stats": (last.stats if last else {}) or {},
            "history": [{"date": f"{r.started_at.astimezone(LOCAL_TZ):%b} {r.started_at.astimezone(LOCAL_TZ).day}",
                         **{k: (r.stats or {}).get(k, 0) for k in ("found", "top", "review", "watch")}}
                        for r in reversed(runs[:10])],
            "sources": (last.source_health if last else []) or [],
            "discovery": (last.discovery if last else {}) or {},
            "opportunities": records,
            "vehicles": self._vehicles(),
        }

    def _vehicles(self) -> dict:
        path = getattr(self.repo, "data_dir", None)
        path = (path / "vehicles.json") if path else None
        if not path or not path.exists():
            return {"leads": []}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {"leads": []}


def _html(text: str) -> str:
    import html
    return html.escape(text or "", quote=True)


__all__ = ["SiteBuilder", "site_url", "repo_slug", "ROOT"]
