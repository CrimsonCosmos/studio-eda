"""Anthropic Messages API backend (JSON-only tasks like planning; optional dependency)."""
from __future__ import annotations

import os

from .base import LLM, LLMResult, LLMUnavailable, extract_json


class AnthropicLLM(LLM):
    name = "anthropic"

    def __init__(self, model: str | None = None):
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise LLMUnavailable("pip install anthropic to use the API backend") from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("ANTHROPIC_API_KEY not set")
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("FORGE_MODEL", "claude-sonnet-5-5")

    def run(self, prompt, *, system="", tools=None, cwd=None, mcp_config=None,
            json_output=False, timeout=900, budget_usd=2.0) -> LLMResult:
        kwargs = dict(model=self.model, max_tokens=16000, system=system or "You are an expert electrical engineer.",
                      messages=[{"role": "user", "content": prompt}])
        if tools and any(t.startswith("Web") for t in tools):
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        msg = self.client.messages.create(**kwargs)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return LLMResult(text, extract_json(text) if json_output else None, 0.0, msg)
