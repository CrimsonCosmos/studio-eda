"""forge command line."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _lib(args, research=True):
    from .llm.base import OfflineLLM, get_llm
    from .parts.db import PartsLibrary
    llm = None
    if research and not getattr(args, "offline", False):
        try:
            llm = get_llm(getattr(args, "llm", None))
            if isinstance(llm, OfflineLLM):
                llm = None
        except Exception:
            llm = None
    return PartsLibrary(getattr(args, "home", None), llm=llm, auto_research=llm is not None), llm


def cmd_blocks(a):
    from .blocks.registry import describe_blocks
    for b in describe_blocks():
        print(f"{b['name']:<20} {b['summary'][:110]}")
        for p in b["params"]:
            print(f"    {p['name']:<18} {p['type']:<7} default={p['default']!r:<10} {p['doc']}")


def cmd_parts(a):
    lib, llm = _lib(a)
    if a.action == "search":
        for p in lib.search(a.arg, 30):
            o = p.best_offer()
            print(f"{p.mpn:<22} {p.category:<22} {p.package:<12} {p.provenance:<10} {'verified' if p.verified else 'unverified':<10} {p.description[:60]}")
    elif a.action == "show":
        p = lib.get(a.arg)
        print(json.dumps(p.to_dict(), indent=1) if p else "not found")
    elif a.action == "research":
        part = lib.research(a.arg)
        print(f"researched {part.mpn}: verified={part.verified} docs={part.docs}\n{part.notes}")
    elif a.action == "verify":
        from .parts.verify import verify_part
        targets = [lib.get(a.arg)] if a.arg else [p for p in lib.all() if p.datasheet_url]
        for p in targets:
            print(verify_part(lib, p))
    elif a.action == "stats":
        print(f"{lib.count()} parts in catalog at {lib.root}; pending research misses: {lib.pending_misses()}")
    elif a.action == "import-jlcpcb":
        from .parts.importers import import_jlcpcb_csv
        print(f"imported {import_jlcpcb_csv(lib, a.arg)} parts")


def cmd_board(a):
    from .blocks.registry import build_block
    from .export.package import write_package
    from .pcb.flow import BoardSpec, build_board
    lib, _ = _lib(a)
    c = build_block(lib, a.block, json.loads(a.params or "{}"))
    res = build_board(c, BoardSpec(layers=a.layers, copper_oz=a.copper_oz))
    out = Path(a.out)
    files = write_package(res, out, a.block)
    print(f"{'OK' if res.success else 'INCOMPLETE'}: {res.board.width}x{res.board.height} mm, {res.board.rules.layers}L, DRC {res.drc.summary()}; files in {out}")
    for k, v in c.calcs.items():
        print("  calc", k, ":", v)


def cmd_plan(a):
    from .blocks.registry import REGISTRY, _load
    from .system.planner import plan_request
    lib, llm = _lib(a)
    plan = plan_request(a.request, a.budget, lib, llm)
    Path(a.out).write_text(plan.to_json())
    print(f"plan written to {a.out} ({len(plan.boards)} board types, {len(plan.purchased)} purchased items, {len(plan.links)} links)")


def _build(plan, a):
    from .purchase.plan import build_purchase_files
    from .system.build import build_system
    lib, _ = _lib(a)
    sb = build_system(plan, lib, a.out, build_pcbs=not a.no_pcb, pcb_time_s=a.pcb_time, pitch=a.pitch)
    pf = build_purchase_files(sb, lib)
    c = sb.cost
    print("\n=== SYSTEM BUILD COMPLETE ===")
    print(f"output: {a.out}")
    print(f"cost: boards ${c['boards_usd']:,.0f} + purchased ${c['purchased_usd']:,.0f} + harness ${c['harness_usd']:,.0f} + contingency ${c['contingency_usd']:,.0f} = ${c['total_usd']:,.0f}"
          + (f" vs budget ${c['budget_usd']:,.0f} -> {'WITHIN' if c['within_budget'] else 'OVER'} budget" if c.get('budget_usd') else ""))
    print(f"read: {a.out}/SYSTEM.md, {a.out}/purchase/shopping_list.md, per-board packages under {a.out}/boards/")
    return sb


def cmd_build(a):
    from .system.planner import plan_request
    lib, llm = _lib(a)
    plan = plan_request(a.request, a.budget, lib, llm)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "plan.json").write_text(plan.to_json())
    _build(plan, a)


def cmd_build_plan(a):
    from .system.plan import SystemPlan
    plan = SystemPlan.from_dict(json.loads(Path(a.plan).read_text()))
    if a.budget:
        plan.budget_usd = a.budget
    _build(plan, a)


def cmd_buy(a):
    from .llm.base import get_llm
    from .purchase.agent import CONFIRM, auto_buy
    llm = get_llm()
    res = auto_buy(Path(a.out) / "purchase" / "purchase_plan.json", llm, a.supplier, a.checkout, a.confirm)
    print(json.dumps(res, indent=1))
    if not a.checkout:
        print(f"\n(cart-fill only. To authorize checkout: --checkout --confirm \"{CONFIRM}\")")


def cmd_mcp(a):
    from .mcp_server import serve
    serve()


def cmd_doctor(a):
    import shutil
    from .parts.db import PartsLibrary
    print("python OK;", "claude CLI:", shutil.which("claude") or "MISSING", "| pdftotext:", shutil.which("pdftotext") or "missing", "| java (freerouting):", shutil.which("java") or "missing")
    lib = PartsLibrary(getattr(a, "home", None), auto_research=False)
    print(f"catalog: {lib.count()} parts at {lib.root}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="forge", description="Claude Code + deterministic EDA: request -> boards -> fab files -> purchasing")
    ap.add_argument("--home", help="cache dir (default ~/.studioforge)")
    ap.add_argument("--offline", action="store_true", help="never call an LLM (templates + catalog only)")
    ap.add_argument("--llm", choices=["claude-code", "anthropic", "offline"], help="LLM backend")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("blocks"); s.set_defaults(fn=cmd_blocks)
    s = sub.add_parser("parts"); s.add_argument("action", choices=["search", "show", "research", "verify", "stats", "import-jlcpcb"]); s.add_argument("arg", nargs="?", default=""); s.set_defaults(fn=cmd_parts)
    s = sub.add_parser("board", help="build one block into a board")
    s.add_argument("block"); s.add_argument("--params"); s.add_argument("--layers", type=int); s.add_argument("--copper-oz", type=float, default=1.0); s.add_argument("--out", default="forge_out/board"); s.set_defaults(fn=cmd_board)
    s = sub.add_parser("plan"); s.add_argument("request"); s.add_argument("--budget", type=float); s.add_argument("--out", default="plan.json"); s.set_defaults(fn=cmd_plan)
    for name, fn in (("build", cmd_build), ("build-plan", cmd_build_plan)):
        s = sub.add_parser(name)
        s.add_argument("request" if name == "build" else "plan")
        s.add_argument("--budget", type=float); s.add_argument("--out", default="forge_out/system")
        s.add_argument("--no-pcb", action="store_true", help="resolve/cost only; skip placement+routing"); s.add_argument("--pcb-time", type=float, default=900.0)
        s.add_argument("--pitch", type=float, help="router grid pitch mm (default auto)"); s.set_defaults(fn=fn)
    s = sub.add_parser("buy", help="agent cart-fill from a built system's purchase plan")
    s.add_argument("--out", default="forge_out/system"); s.add_argument("--supplier"); s.add_argument("--checkout", action="store_true"); s.add_argument("--confirm", default="")
    s.set_defaults(fn=cmd_buy)
    s = sub.add_parser("mcp"); s.set_defaults(fn=cmd_mcp)
    s = sub.add_parser("doctor"); s.set_defaults(fn=cmd_doctor)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
