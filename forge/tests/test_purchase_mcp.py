import json
import subprocess
import sys
from pathlib import Path

import pytest

from studioforge.llm.base import MockLLM
from studioforge.model import Offer
from studioforge.purchase import mouser
from studioforge.purchase.agent import CONFIRM, auto_buy
from studioforge.purchase.plan import _spare, build_purchase_files
from studioforge.system.build import build_system
from test_system import joint_plan


@pytest.fixture
def sysbuild(lib, tmp_path):
    return build_system(joint_plan(), lib, tmp_path / "s", log=None, build_pcbs=False)


def test_purchase_files_every_line_has_a_link(sysbuild, lib):
    out = build_purchase_files(sysbuild, lib)
    plan = out["plan"]
    assert plan["parts"] and all(p["url"].startswith("http") for p in plan["parts"])
    assert all(p["qty_order"] >= p["qty_needed"] for p in plan["parts"])
    assert (sysbuild.out / "purchase" / "shopping_list.md").read_text().count("](http") >= len(plan["parts"])
    assert any(f.name.startswith("cart_") for f in out["files"].values())
    assert any(i["needs_manual_sourcing"] for i in plan["items"])                       # no URL known -> flagged, not hidden


def test_spares_policy():
    assert _spare(100, "resistor") == 110 and _spare(3, "capacitor.mlcc") == 8 and _spare(1, "ic.mcu") == 2 and _spare(4, "mounting") == 4


def test_agent_never_checks_out_without_confirmation(sysbuild, lib):
    build_purchase_files(sysbuild, lib)
    seen = {}

    def handler(prompt, kw):
        seen["prompt"] = prompt
        return {"added": [], "manual": [], "checkout_done": False}
    res = auto_buy(sysbuild.out / "purchase" / "purchase_plan.json", MockLLM(handler))
    assert "CART FILL ONLY" in seen["prompt"] and res["checkout_authorized"] is False
    assert res["manual"]                                                                 # unreported lines come back as a manual list
    res2 = auto_buy(sysbuild.out / "purchase" / "purchase_plan.json", MockLLM(handler), allow_checkout=True, confirm="yes")
    assert res2["checkout_authorized"] is False
    res3 = auto_buy(sysbuild.out / "purchase" / "purchase_plan.json", MockLLM(handler), allow_checkout=True, confirm=CONFIRM)
    assert res3["checkout_authorized"] is True


def test_agent_failure_degrades_to_manual_list(sysbuild, lib):
    from studioforge.llm.base import OfflineLLM
    build_purchase_files(sysbuild, lib)
    res = auto_buy(sysbuild.out / "purchase" / "purchase_plan.json", OfflineLLM())
    assert res["manual"] and all(m["url"] for m in res["manual"])


def test_mouser_response_parsing():
    js = {"SearchResults": {"Parts": [{"ManufacturerPartNumber": "TPS563201DDCR", "MouserPartNumber": "595-TPS563201DDCR", "ProductDetailUrl": "https://www.mouser.com/ProductDetail/x",
                                       "Availability": "12,345 In Stock", "Min": "1", "PriceBreaks": [{"Quantity": 1, "Price": "$0.75"}, {"Quantity": 100, "Price": "$0.52"}]}]}}
    o = mouser.parse_part_response(js, "TPS563201DDCR")
    assert isinstance(o, Offer) and o.sku == "595-TPS563201DDCR" and o.price_at(100) == 0.52 and o.stock == 12345 and not o.estimated


def _rpc(proc, id_, method, params=None):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def test_mcp_server_protocol(tmp_path):
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "FORGE_HOME": str(tmp_path / "h"), "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen([sys.executable, "-m", "studioforge.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    try:
        init = _rpc(proc, 1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "studioforge"
        tools = _rpc(proc, 2, "tools/list")["result"]["tools"]
        assert {"forge_list_blocks", "forge_validate_plan", "forge_build_system", "forge_research_part"} <= {t["name"] for t in tools}
        blocks = _rpc(proc, 3, "tools/call", {"name": "forge_list_blocks", "arguments": {}})["result"]
        assert any(b["name"] == "buck_converter" for b in json.loads(blocks["content"][0]["text"]))
        val = _rpc(proc, 4, "tools/call", {"name": "forge_validate_plan", "arguments": {"plan": joint_plan().to_dict()}})["result"]
        out = json.loads(val["content"][0]["text"])
        assert out["valid"] and out["block_params"]["joint.drv"]["phase_current_a"] == 8.0
        bad = _rpc(proc, 5, "tools/call", {"name": "forge_get_part", "arguments": {"mpn": "NOPE"}})["result"]
        assert "not in catalog" in bad["content"][0]["text"]
    finally:
        proc.terminate()


def test_claude_code_backend_builds_a_safe_command(monkeypatch):
    from studioforge.llm import claude_code
    captured = {}

    class R:
        returncode, stdout, stderr = 0, json.dumps({"result": '{"ok": 1}', "total_cost_usd": 0.01}), ""
    monkeypatch.setattr(claude_code.subprocess, "run", lambda cmd, **kw: (captured.update(cmd=cmd), R)[1])
    monkeypatch.setattr(claude_code.shutil, "which", lambda n: "/usr/bin/claude")
    llm = claude_code.ClaudeCodeLLM()
    r = llm.run("hi", system="sys", tools=["WebSearch"], json_output=True, budget_usd=1.5)
    cmd = captured["cmd"]
    assert cmd[1] == "-p" and "--max-budget-usd" in cmd and "1.5" in cmd and "--allowedTools" in cmd and r.data == {"ok": 1}
