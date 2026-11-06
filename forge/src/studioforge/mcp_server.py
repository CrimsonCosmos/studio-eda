"""MCP server (stdio, JSON-RPC 2.0, no third-party dependency).

Lets Claude Code act as the supervisor: it decomposes the request itself (following the
instructions in CLAUDE.md / planner.SUPERVISOR_PROMPT) and calls these deterministic tools.
Register with:  claude mcp add studioforge -- python -m studioforge.mcp_server
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

PROTOCOL = "2024-11-05"


def _lib(auto_research: bool = True):
    from .llm.base import get_llm
    from .parts.db import PartsLibrary
    llm = None
    if auto_research:
        try:
            llm = get_llm()
            if llm.name == "offline":
                llm = None
        except Exception:
            llm = None
    return PartsLibrary(llm=llm, auto_research=llm is not None)


def _outdir(name: str) -> Path:
    base = Path(os.environ.get("FORGE_OUT") or Path.cwd() / "forge_out")
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def tool_list_blocks(args: dict):
    from .blocks.registry import describe_blocks
    return describe_blocks()


def tool_search_parts(args: dict):
    lib = _lib(False)
    q = args["query"]
    hits = lib.search(q, int(args.get("limit", 15)))
    return [{"mpn": p.mpn, "category": p.category, "package": p.package, "description": p.description, "params": p.params,
             "verified": p.verified, "datasheet_cached": bool(p.docs), "offers": [{"supplier": o.supplier, "url": o.url, "price": o.unit_price} for o in p.offers]}
            for p in hits]


def tool_get_part(args: dict):
    lib = _lib(False)
    p = lib.get(args["mpn"])
    return p.to_dict() if p else {"error": "not in catalog; call forge_research_part"}


def tool_research_part(args: dict):
    lib = _lib(True)
    if lib.llm is None:
        return {"error": "no LLM backend for research; this tool needs the `claude` CLI"}
    part = lib.research(args["spec"], category_hint=args.get("category", ""))
    return {"mpn": part.mpn, "verified": part.verified, "docs": part.docs, "offers": [o.to_dict() for o in part.offers], "notes": part.notes}


def tool_validate_plan(args: dict):
    from .blocks.registry import REGISTRY, _load
    from .system.plan import SystemPlan
    from .system.resolver import Resolver
    _load()
    plan = SystemPlan.from_dict(args["plan"] if isinstance(args["plan"], dict) else json.loads(args["plan"]))
    errs = plan.validate(set(REGISTRY))
    if errs:
        return {"valid": False, "errors": errs}
    lib = _lib(args.get("research", False))
    try:
        res = Resolver(lib, plan, log=None).resolve()
    except Exception as e:
        return {"valid": False, "errors": [f"resolution failed: {e}"]}
    return {"valid": not res.errors(), "issues": [{"severity": i.severity, "msg": i.msg} for i in res.issues], "demand_a": res.demand,
            "block_params": {f"{k[0]}.{k[1]}": {a: b for a, b in v.items() if a != "requests"} for k, v in res.params.items()}}


def tool_build_system(args: dict):
    from .system.plan import SystemPlan
    from .system.build import build_system
    plan = SystemPlan.from_dict(args["plan"] if isinstance(args["plan"], dict) else json.loads(args["plan"]))
    lib = _lib(args.get("research", True))
    out = _outdir(plan.name)
    sb = build_system(plan, lib, out, log=lambda s: print(s, file=sys.stderr), build_pcbs=bool(args.get("build_pcbs", True)),
                      pcb_time_s=float(args.get("pcb_time_s", 900)))
    from .purchase.plan import build_purchase_files
    pf = build_purchase_files(sb, lib)
    return {"output_dir": str(out), "cost": sb.cost, "decisions": sb.resolved.decisions,
            "issues": [{"severity": i.severity, "msg": i.msg} for i in sb.resolved.issues + sb.issues],
            "boards": {b: {"files": sorted(str(p) for p in (i.get("files") or {}).values())[:6], "cost": i["cost"],
                           "drc": (i["result"].drc.summary() if "result" in i else "not built")} for b, i in sb.boards.items()},
            "reports": [str(out / "SYSTEM.md"), str(out / "purchase" / "shopping_list.md")]}


def tool_build_block_board(args: dict):
    from .blocks.registry import build_block
    from .pcb.flow import BoardSpec, build_board
    from .export.package import write_package
    lib = _lib(args.get("research", True))
    c = build_block(lib, args["block"], args.get("params") or {})
    res = build_board(c, BoardSpec(layers=args.get("layers"), copper_oz=float(args.get("copper_oz", 1.0))), log=lambda s: print(s, file=sys.stderr))
    out = _outdir(f"block_{args['block']}")
    files = write_package(res, out, args["block"])
    return {"output_dir": str(out), "success": res.success, "drc": res.drc.summary(), "board_mm": [res.board.width, res.board.height],
            "layers": res.board.rules.layers, "calcs": c.calcs, "warnings": list(dict.fromkeys(res.board.warnings + c.warnings))[:15],
            "fab_zip": str(files["fab.zip"])}


def tool_purchase_list(args: dict):
    return {"hint": "forge_build_system already writes <output_dir>/purchase/shopping_list.md, cart_*.csv and purchase_plan.json"}


TOOLS = {
    "forge_list_blocks": (tool_list_blocks, "List the deterministic subassembly builders (blocks) with parameters. Call first when planning.",
                          {"type": "object", "properties": {}}),
    "forge_search_parts": (tool_search_parts, "Search the cached parts catalog (MPN/description/category text).",
                           {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}),
    "forge_get_part": (tool_get_part, "Full catalog record for an MPN (pins, params, docs, offers).",
                       {"type": "object", "properties": {"mpn": {"type": "string"}}, "required": ["mpn"]}),
    "forge_research_part": (tool_research_part, "Find a part NOT in the catalog on the web, download its official datasheet, cache it, record where to buy it.",
                            {"type": "object", "properties": {"spec": {"type": "string", "description": "requirement or MPN"}, "category": {"type": "string"}}, "required": ["spec"]}),
    "forge_validate_plan": (tool_validate_plan, "Validate a SystemPlan JSON and run the interface resolver (no PCB work). Returns issues, current demand, derived block params.",
                            {"type": "object", "properties": {"plan": {"type": ["object", "string"]}, "research": {"type": "boolean"}}, "required": ["plan"]}),
    "forge_build_system": (tool_build_system, "Resolve + build every board (place, route, DRC, Gerber/BOM/KiCad), cost roll-up, buy-vs-build, purchase lists. Slow (minutes).",
                           {"type": "object", "properties": {"plan": {"type": ["object", "string"]}, "build_pcbs": {"type": "boolean"}, "research": {"type": "boolean"}, "pcb_time_s": {"type": "number"}}, "required": ["plan"]}),
    "forge_build_block_board": (tool_build_block_board, "Build ONE block (e.g. buck_converter with params) into a finished fab-ready board.",
                                {"type": "object", "properties": {"block": {"type": "string"}, "params": {"type": "object"}, "layers": {"type": "integer"}, "copper_oz": {"type": "number"}, "research": {"type": "boolean"}}, "required": ["block"]}),
    "forge_purchase_list": (tool_purchase_list, "Where purchase lists are written.", {"type": "object", "properties": {}}),
}


def _reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req: dict):
    m, id_, p = req.get("method"), req.get("id"), req.get("params") or {}
    if m == "initialize":
        return {"protocolVersion": p.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                "serverInfo": {"name": "studioforge", "version": "0.1.0"},
                "instructions": "Deterministic EDA pipelines. Decompose requests into a SystemPlan (see CLAUDE.md), validate with forge_validate_plan, build with forge_build_system."}
    if m == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (_, d, s) in TOOLS.items()]}
    if m == "tools/call":
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in TOOLS:
            raise KeyError(f"unknown tool {name}")
        try:
            out = TOOLS[name][0](args)
            return {"content": [{"type": "text", "text": json.dumps(out, indent=1, default=str)}]}
        except Exception as e:
            return {"isError": True, "content": [{"type": "text", "text": f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}"}]}
    if m in ("ping",):
        return {}
    return None


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" not in req:                       # notification
            continue
        try:
            res = handle(req)
            if res is None:
                _reply(req["id"], error={"code": -32601, "message": f"method not found: {req.get('method')}"})
            else:
                _reply(req["id"], res)
        except Exception as e:
            _reply(req["id"], error={"code": -32000, "message": str(e)})


if __name__ == "__main__":
    serve()
