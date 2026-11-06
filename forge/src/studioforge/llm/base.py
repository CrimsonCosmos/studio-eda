"""LLM abstraction.

Claude is used for what only a language model is good at: reading the open web
and datasheets, decomposing a vague request into subsystems, and judgement calls.
Everything geometric / numeric stays in deterministic code. Backends:

  ClaudeCodeLLM  - headless `claude -p` (web search/fetch, file tools, MCP). Primary.
  AnthropicLLM   - Messages API (optional `anthropic` package; JSON-only tasks).
  MockLLM        - scripted responses for tests / offline runs.
  OfflineLLM     - raises, so callers fall back to deterministic templates.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    data: Any = None
    cost_usd: float = 0.0
    raw: Any = None


def extract_json(text: str) -> Any:
    """Pull the last JSON object/array out of an LLM reply (fenced or bare)."""
    if not text:
        raise ValueError("empty LLM reply")
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidates = list(reversed(fences)) + [text]
    for cand in candidates:
        cand = cand.strip()
        try:
            return json.loads(cand)
        except Exception:
            pass
        # scan for a balanced {...} or [...]
        for opener, closer in (("{", "}"), ("[", "]")):
            end = cand.rfind(closer)
            while end != -1:
                start = cand.find(opener)
                while start != -1 and start < end:
                    try:
                        return json.loads(cand[start:end + 1])
                    except Exception:
                        start = cand.find(opener, start + 1)
                end = cand.rfind(closer, 0, end)
    raise ValueError(f"no JSON found in reply: {text[:200]!r}")


class LLM:
    name = "base"

    def run(self, prompt: str, *, system: str = "", tools: list[str] | None = None,
            cwd: str | Path | None = None, mcp_config: dict | None = None,
            json_output: bool = False, timeout: int = 900, budget_usd: float = 2.0) -> LLMResult:
        raise NotImplementedError


class OfflineLLM(LLM):
    name = "offline"

    def run(self, *a, **kw) -> LLMResult:
        raise LLMUnavailable("no LLM backend configured (set FORGE_LLM=claude-code or anthropic)")


class MockLLM(LLM):
    """handler(prompt, kwargs) -> str | dict. Records calls for assertions."""
    name = "mock"

    def __init__(self, handler: Callable[[str, dict], Any]):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def run(self, prompt, **kw) -> LLMResult:
        self.calls.append((prompt, kw))
        out = self.handler(prompt, kw)
        if isinstance(out, (dict, list)):
            return LLMResult(json.dumps(out), out)
        data = None
        if kw.get("json_output"):
            data = extract_json(out)
        return LLMResult(out, data)


def get_llm(kind: str | None = None) -> LLM:
    import os
    kind = (kind or os.environ.get("FORGE_LLM") or "auto").lower()
    if kind in ("claude-code", "claude", "cc"):
        from .claude_code import ClaudeCodeLLM
        return ClaudeCodeLLM()
    if kind == "anthropic":
        from .anthropic_api import AnthropicLLM
        return AnthropicLLM()
    if kind == "offline":
        return OfflineLLM()
    # auto: prefer Claude Code if installed, then API key, else offline
    import shutil
    if shutil.which("claude"):
        from .claude_code import ClaudeCodeLLM
        return ClaudeCodeLLM()
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from .anthropic_api import AnthropicLLM
            return AnthropicLLM()
        except Exception:
            pass
    return OfflineLLM()
