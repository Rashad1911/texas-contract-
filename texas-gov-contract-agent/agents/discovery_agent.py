"""AGENT 9 — Opportunity Discovery.

Learns from what the scout sees so the search doesn't stay limited to the starting list:
  1 Related lines — when a category shows up a lot (e.g. janitorial), it points to adjacent services from
    services.yaml → related (window cleaning, pressure washing, waste hauling…).
  2 New categories — service contracts that matched none of your categories are mined for recurring
    service phrases. Known adjacent service lines (below) are recognized directly; other phrases that recur
    across different solicitations are proposed as candidates.
  3 Optional AI pass — with an LLM key, it also proposes business-model ideas from the unmatched titles.

Discovered categories are saved to data/discovered_services.yaml. With discovery.auto_add: true, a
category seen in 2+ different solicitations is added to the next run's search automatically (with a
medium/medium/medium profile). Set `approved: false` on any entry to switch it off, or copy it into
config/services.yaml to make it permanent (README §7).
"""
from __future__ import annotations

import re
from collections import Counter

import yaml

from core.config import Config
from core.extract import now
from core.llm import LLM
from core.opportunity import Opportunity

# Adjacent service lines small firms commonly manage through subcontractors. Profiles are starting
# assumptions (startup / outsource / margin), refined by the agents on each real solicitation.
KNOWN_LINES = {
    "irrigation": ("Irrigation maintenance", ["irrigation"], "low", "high", "medium",
                   "Licensed irrigator (TCEQ) for repairs"),
    "gutter_roof_cleaning": ("Gutter & roof cleaning", ["gutter cleaning", "roof cleaning", "gutter"], "low", "high", "medium", ""),
    "duct_hood_cleaning": ("Duct & kitchen hood cleaning", ["duct cleaning", "hood cleaning", "kitchen exhaust",
                                                           "exhaust hood"], "medium", "high", "medium", ""),
    "grease_trap": ("Grease trap / interceptor service", ["grease trap", "grease interceptor"], "medium", "high", "medium", ""),
    "portable_toilets": ("Portable toilet rental & service", ["portable toilet", "portable restroom", "porta"],
                         "medium", "high", "medium", ""),
    "fence_repair": ("Fence repair & installation", ["fence repair", "fencing", "fence"], "medium", "high", "medium", ""),
    "litter_abatement": ("Litter pickup & right-of-way cleanup", ["litter", "right-of-way cleanup", "illegal dumping",
                                                                  "encampment cleanup"], "low", "high", "medium", ""),
    "weed_lot_mowing": ("Vacant lot mowing & weed abatement", ["weed abatement", "vacant lot", "lot mowing",
                                                               "high weeds", "nuisance abatement"], "low", "high", "medium", ""),
    "board_up": ("Board-up & property securing", ["board-up", "board up", "securing vacant", "boarding"],
                 "low", "high", "medium", ""),
    "towing": ("Towing & vehicle removal", ["towing", "wrecker", "vehicle removal"], "high", "medium", "medium",
               "TDLR tow license"),
    "furniture_install": ("Furniture moving & installation", ["furniture installation", "furniture moving",
                                                              "office furniture", "cubicle"], "low", "high", "medium", ""),
    "signage": ("Sign installation & maintenance", ["sign installation", "signage", "sign maintenance"],
                "medium", "high", "medium", ""),
    "traffic_control": ("Traffic control & flagging", ["traffic control", "flagging", "flagger"], "medium", "medium",
                        "medium", ""),
    "dead_animal": ("Dead animal pickup", ["dead animal", "animal carcass"], "low", "high", "medium", ""),
    "uniform_linen": ("Uniform & linen service", ["uniform rental", "linen service", "mat service", "floor mats"],
                      "medium", "high", "low", ""),
    "mold_remediation": ("Mold remediation", ["mold remediation", "mold abatement"], "medium", "high", "high",
                         "Texas mold remediation license (TDLR)"),
    "elevator": ("Elevator maintenance", ["elevator"], "high", "low", "medium", "TDLR elevator contractor"),
    "fire_extinguisher": ("Fire extinguisher & safety inspections", ["fire extinguisher", "fire alarm inspection",
                                                                    "sprinkler inspection"], "medium", "medium", "medium",
                          "Texas State Fire Marshal license"),
    "vending": ("Vending & micro-market", ["vending"], "medium", "high", "medium", ""),
    "interpretation": ("Interpretation & translation", ["interpretation", "translation", "interpreter"],
                       "low", "high", "medium", ""),
    "mailroom": ("Mailroom & reception support", ["mailroom", "reception services", "front desk"], "low", "medium",
                 "low", ""),
}
STOP = set("""a an and the of for to in on at by with from or as is be are this that services service contract
contracts rfp rfq ifb bid bids solicitation annual term city county texas tx various citywide countywide
department dept program project provide providing request proposal proposals quote quotes requirements
maintenance repair repairs support supply supplies year years multi option renewal new""".split())


class DiscoveryAgent:
    def __init__(self, config: Config, llm: LLM | None = None):
        self.config = config
        self.llm = llm
        self.path = config.data_dir / "discovered_services.yaml"
        self.categories = config.service_categories(include_discovered=False)

    def run(self, opps: list[Opportunity]) -> dict:
        if not self.config.get("discovery.enabled", True):
            return {"enabled": False}
        counts = Counter(o.service_category for o in opps if o.service_category)
        related = self._related(counts)
        unmatched = [o for o in opps if o.analysis.get("unclassified_service")]
        new = self._known_lines(unmatched) + self._phrases(unmatched)
        ideas = self._llm_ideas(unmatched)
        saved = self._save(new)
        return {
            "enabled": True,
            "category_counts": dict(counts.most_common()),
            "related_suggestions": related,
            "new_categories": [{k: v for k, v in n.items() if k != "all_titles"} for n in new[:10]],
            "ideas": ideas,
            "auto_added": saved,
            "unmatched_service_count": len(unmatched),
        }

    # ── 1 related lines ──────────────────────────────────────────────
    def _related(self, counts: Counter) -> list[dict]:
        out = []
        for key, n in counts.most_common(4):
            if n < 3:
                break
            cat = self.categories.get(key, {})
            rel = [r for r in cat.get("related", []) if r in self.categories]
            if not rel:
                continue
            labels = [f"{self.categories[r].get('label', r)} ({counts.get(r, 0)} found)" for r in rel]
            out.append({"because": f"{cat.get('label', key)} came up {n} times",
                        "suggest": labels,
                        "tip": "Line up one subcontractor per adjacent service so you can bid bundles."})
        return out

    # ── 2 new categories ─────────────────────────────────────────────
    def _known_lines(self, unmatched: list[Opportunity]) -> list[dict]:
        hits: dict[str, list[str]] = {}
        for o in unmatched:
            title = (o.title or "").lower()
            for key, (label, kws, *_rest) in KNOWN_LINES.items():
                if key in self.categories:
                    continue
                if any(re.search(rf"\b{re.escape(k)}", title) for k in kws):
                    hits.setdefault(key, []).append(o.title)
        out = []
        for key, titles in sorted(hits.items(), key=lambda kv: -len(kv[1])):
            label, kws, startup, outsource, margin, lic = KNOWN_LINES[key]
            out.append({"key": key, "label": label, "keywords": kws, "count": len(set(titles)),
                        "examples": list(dict.fromkeys(titles))[:3], "all_titles": list(dict.fromkeys(titles)),
                        "startup": startup, "outsource": outsource,
                        "margin": margin, "licenses": [lic] if lic else [], "source": "known adjacent line"})
        return out

    def _phrases(self, unmatched: list[Opportunity]) -> list[dict]:
        grams: dict[str, set[str]] = {}
        for o in unmatched:
            words = [w for w in re.findall(r"[a-z][a-z\-]{2,}", (o.title or "").lower())]
            for i in range(len(words) - 1):
                a, b = words[i], words[i + 1]
                if a in STOP or b in STOP:
                    continue
                grams.setdefault(f"{a} {b}", set()).add(o.title)
        known_kw = {k.lower() for c in self.categories.values() for k in c.get("keywords", [])}
        known_kw |= {k for _, (_, kws, *_r) in KNOWN_LINES.items() for k in kws}
        out = []
        for phrase, titles in sorted(grams.items(), key=lambda kv: -len(kv[1])):
            if len(titles) < 2 or any(phrase in k or k in phrase for k in known_kw):
                continue
            key = re.sub(r"[^a-z]+", "_", phrase).strip("_")
            out.append({"key": key, "label": phrase.title(), "keywords": [phrase], "count": len(titles),
                        "examples": sorted(titles)[:3], "all_titles": sorted(titles), "startup": "medium",
                        "outsource": "medium",
                        "margin": "medium", "licenses": [], "source": "recurring phrase"})
            if len(out) >= 5:
                break
        return out

    # ── 3 optional AI ideas ──────────────────────────────────────────
    def _llm_ideas(self, unmatched: list[Opportunity]) -> list[str]:
        if not self.llm or not self.llm.enabled or len(unmatched) < 3:
            return []
        titles = "\n".join(sorted({o.title for o in unmatched})[:80])
        task = ("These Texas government service solicitations did not match the owner's current categories. "
                "Suggest up to 4 service business lines a small LLC could manage through subcontractors, based only "
                "on these titles. Return JSON {\"ideas\": [\"one sentence each, name the line and why\"]}.")
        out = self.llm.json(task, titles, max_tokens=400) or {}
        return [str(i)[:240] for i in out.get("ideas", [])[:4]]

    # ── persistence ──────────────────────────────────────────────────
    def _save(self, new: list[dict]) -> list[str]:
        data = {}
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        cats = data.get("categories") or {}
        added = []
        today = now().date().isoformat()
        auto = self.config.get("discovery.auto_add", True)
        need = int(self.config.get("discovery.min_occurrences", 2))
        for item in new:
            key = item["key"]
            entry = cats.get(key) or {"label": item["label"], "keywords": item["keywords"], "naics": [],
                                      "startup": item["startup"], "outsource": item["outsource"],
                                      "margin": item["margin"], "licenses": item["licenses"],
                                      "subcontractor": f"Qualified {item['label'].lower()} provider",
                                      "related": [], "first_seen": today, "times_seen": 0, "examples": [],
                                      "approved": False, "source": item["source"]}
            # count DISTINCT solicitations (the same open notice is re-seen every run)
            seen = list(dict.fromkeys((entry.get("seen_titles") or entry.get("examples") or []) + item["all_titles"]))
            entry["seen_titles"] = seen[-40:]
            entry["times_seen"] = len(seen)
            entry["examples"] = seen[:5]
            entry["last_seen"] = today
            if auto and not entry.get("approved") and entry["times_seen"] >= need and not entry.get("rejected"):
                entry["approved"] = True
                added.append(entry["label"])
            cats[key] = entry
        if new or not self.path.exists():
            header = ("# Service categories the Discovery agent found. approved: true = searched on the next run.\n"
                      "# Set approved: false (and rejected: true) to switch one off, or copy it into config/services.yaml.\n")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(header + yaml.safe_dump({"categories": cats}, sort_keys=False, allow_unicode=True),
                                 encoding="utf-8")
        return added
