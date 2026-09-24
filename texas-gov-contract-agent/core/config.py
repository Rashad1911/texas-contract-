"""Configuration loading.

All tunable settings live in /config/*.yaml. Secrets (API keys, email password) come only
from environment variables — locally from a .env file, in GitHub from Actions secrets.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "docs"
OUTPUT_DIR = ROOT / "output"

try:  # optional: load a local .env when running on your own machine
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover
    pass


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


class Config:
    """Read-only view over the YAML config files."""

    def __init__(self, config_dir: Path | None = None, data_dir: Path | None = None):
        cdir = config_dir or CONFIG_DIR
        self.data_dir = data_dir or DATA_DIR
        self.settings = load_yaml(cdir / "settings.yaml")
        self.locations = load_yaml(cdir / "locations.yaml")
        self.services = load_yaml(cdir / "services.yaml")
        self.sources = load_yaml(cdir / "sources.yaml")
        self.discovered = load_yaml(self.data_dir / "discovered_services.yaml")

    # ── generic access ────────────────────────────────────────────────
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.settings
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def company(self) -> dict:
        return self.settings.get("company", {}) or {}

    # ── markets ───────────────────────────────────────────────────────
    @property
    def markets(self) -> dict:
        return {k: v for k, v in (self.locations.get("markets") or {}).items() if v.get("enabled", True)}

    def market_label(self, key: str) -> str:
        if key == "statewide":
            return "Texas (statewide)"
        if key == "unknown":
            return "Location not listed"
        return (self.markets.get(key) or {}).get("label", key or "")

    # ── services ──────────────────────────────────────────────────────
    def service_categories(self, include_discovered: bool = True) -> dict:
        cats = dict(self.services.get("categories") or {})
        if include_discovered and self.get("discovery.auto_add", True):
            for key, cat in (self.discovered.get("categories") or {}).items():
                if key not in cats and cat.get("approved", True):
                    cat = dict(cat)
                    cat.setdefault("startup", "medium")
                    cat.setdefault("outsource", "medium")
                    cat.setdefault("margin", "medium")
                    cat["discovered"] = True
                    cats[key] = cat
        return cats

    def category_label(self, key: str) -> str:
        cat = self.service_categories().get(key) or {}
        return cat.get("label", key.replace("_", " ").title() if key else "Unclassified")

    # ── sources ───────────────────────────────────────────────────────
    def source_entries(self) -> list[dict]:
        entries = []
        for group in ("federal", "local"):
            for item in self.sources.get(group) or []:
                if item.get("enabled", True):
                    entries.append(item)
        return entries

    def source(self, key: str) -> dict:
        for item in self.source_entries():
            if item.get("key") == key:
                return item
        return {}

    @property
    def regulations(self) -> dict:
        return self.sources.get("regulations") or {}
