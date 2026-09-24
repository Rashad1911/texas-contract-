"""Thin LLM wrapper.

Uses OpenAI (OPENAI_API_KEY) by default, or Anthropic (ANTHROPIC_API_KEY) if that's the key you
have. Every agent works without an LLM — rule-based extraction runs first and the LLM only adds
plain-English summaries. If a call fails, the agent keeps its rule-based result.
"""
from __future__ import annotations

import json
import logging
import re

from . import http
from .config import Config, env

log = logging.getLogger(__name__)

GUARDRAIL = (
    "You are a careful government-contracting research assistant for a small Texas LLC. "
    "Use ONLY the solicitation text and data you are given. Never invent requirements, clause numbers, "
    "dollar amounts, dates, incumbents, or legal conclusions. If the text does not say something, write "
    "'not stated in the text reviewed'. Never say subcontracting is permitted unless the text supports it. "
    "Flag anything that needs review by an attorney, accountant, or procurement professional. "
    "Write in plain, short sentences for a busy owner reading on a phone. Respond with a single JSON object only."
)


class LLM:
    def __init__(self, config: Config):
        self.config = config
        provider = (config.get("llm.provider", "auto") or "auto").lower()
        self.openai_key = env("OPENAI_API_KEY")
        self.anthropic_key = env("ANTHROPIC_API_KEY")
        if provider == "auto":
            provider = "openai" if self.openai_key else "anthropic" if self.anthropic_key else "none"
        if provider == "openai" and not self.openai_key:
            provider = "none"
        if provider == "anthropic" and not self.anthropic_key:
            provider = "none"
        self.provider = provider
        self.max_calls = int(config.get("llm.max_calls_per_run", 60))
        self.calls = 0
        self.failures = 0

    @property
    def enabled(self) -> bool:
        return self.provider != "none" and self.calls < self.max_calls and self.failures < 5

    def json(self, task: str, payload: str, max_tokens: int = 1400) -> dict | None:
        """Ask for a JSON object. Returns None when disabled or on any failure."""
        if not self.enabled:
            return None
        self.calls += 1
        try:
            if self.provider == "openai":
                text = self._openai(task, payload, max_tokens)
            else:
                text = self._anthropic(task, payload, max_tokens)
            return _parse_json(text)
        except Exception as exc:  # network, quota, bad JSON — keep the rule-based result
            self.failures += 1
            log.warning("LLM call failed (%s): %s", self.provider, exc)
            return None

    def _openai(self, task: str, payload: str, max_tokens: int) -> str:
        model = env("OPENAI_MODEL") or self.config.get("llm.openai_model", "gpt-4.1-mini")
        resp = http.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.openai_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": GUARDRAIL},
                    {"role": "user", "content": f"{task}\n\n---\n{payload}"},
                ],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": max_tokens,
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def _anthropic(self, task: str, payload: str, max_tokens: int) -> str:
        model = env("ANTHROPIC_MODEL") or self.config.get("llm.anthropic_model", "claude-sonnet-5")
        resp = http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.anthropic_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "system": GUARDRAIL,
                "messages": [{"role": "user", "content": f"{task}\n\n---\n{payload}"}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
    return None
