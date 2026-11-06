"""Headless Claude Code backend: `claude -p ... --output-format json`.

This is how the program "brings in" Claude for open-ended work. Claude Code gets
WebSearch/WebFetch, `curl` for downloading official datasheets, file tools, and
(optionally) this project's MCP server so it can call the deterministic engines.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import LLM, LLMResult, LLMUnavailable, extract_json

RESEARCH_TOOLS = ["WebSearch", "WebFetch", "Read", "Write", "Bash(curl:*)", "Bash(pdftotext:*)", "Bash(ls:*)"]


class ClaudeCodeLLM(LLM):
    name = "claude-code"

    def __init__(self, binary: str | None = None, model: str | None = None):
        self.binary = binary or shutil.which("claude")
        self.model = model or os.environ.get("FORGE_MODEL")
        if not self.binary:
            raise LLMUnavailable("`claude` CLI not found on PATH")

    def run(self, prompt: str, *, system: str = "", tools: list[str] | None = None,
            cwd: str | Path | None = None, mcp_config: dict | None = None,
            json_output: bool = False, timeout: int = 900, budget_usd: float = 2.0) -> LLMResult:
        cmd = [self.binary, "-p", prompt, "--output-format", "json", "--no-session-persistence",
               "--max-budget-usd", f"{budget_usd:g}", "--permission-mode", "acceptEdits"]
        if self.model:
            cmd += ["--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        tmp = None
        if mcp_config:
            tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(mcp_config, tmp)
            tmp.close()
            cmd += ["--mcp-config", tmp.name]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  cwd=str(cwd) if cwd else None)
        except subprocess.TimeoutExpired as e:
            raise LLMUnavailable(f"claude timed out after {timeout}s") from e
        finally:
            if tmp:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        if proc.returncode != 0 and not proc.stdout.strip():
            raise LLMUnavailable(f"claude exited {proc.returncode}: {proc.stderr[:400]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {"result": proc.stdout}
        text = payload.get("result", "") if isinstance(payload, dict) else str(payload)
        if isinstance(payload, dict) and payload.get("is_error"):
            raise LLMUnavailable(f"claude error: {text[:400]}")
        data = None
        if json_output:
            data = extract_json(text)
        cost = float(payload.get("total_cost_usd", 0.0)) if isinstance(payload, dict) else 0.0
        return LLMResult(text, data, cost, payload)
