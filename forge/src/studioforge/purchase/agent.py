"""Agent-driven purchasing (computer use / browser).

Claude Code, given a browser tool, walks the purchase plan and fills supplier carts. This module is
deliberately conservative: it ADDS TO CART ONLY and stops before payment unless the caller sets
allow_checkout=True *and* supplies the exact confirmation phrase. Anything the agent cannot buy
(captcha, out of stock, login wall, ambiguous part) is returned in `manual` so the user still
gets a complete list of links. This path needs a logged-in browser session and is not exercised in CI.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..llm.base import LLM, LLMUnavailable

CONFIRM = "I authorize placing this order"
BROWSER_TOOLS = ["mcp__claude-in-chrome", "mcp__Claude_Browser", "WebFetch", "WebSearch", "Read"]

SYSTEM = (
    "You are a careful procurement assistant with browser control. For each line in the purchase plan, open the supplier page, "
    "confirm the manufacturer part number matches EXACTLY (do not substitute), check package/stock, and add the stated quantity "
    "to the cart. NEVER enter payment details, never click a final 'place order'/'pay' button unless the user message contains the exact "
    "authorization phrase given below. Do not create accounts. If a page needs login, a captcha, or the part is unavailable, skip it and "
    "record why. Finish with ONE JSON object: {\"added\":[{mpn,supplier,qty,unit_price,cart_url?}],\"manual\":[{mpn,supplier,reason,url}],"
    "\"cart_totals\":{supplier:usd},\"checkout_done\":false}.")


def auto_buy(plan_json: str | Path, llm: LLM, supplier: str | None = None, allow_checkout: bool = False, confirm: str = "") -> dict:
    plan = json.loads(Path(plan_json).read_text())
    parts = [p for p in plan["parts"] if supplier in (None, p["supplier"])]
    if not parts:
        return {"added": [], "manual": [], "note": "nothing to buy for that supplier"}
    do_checkout = allow_checkout and confirm.strip() == CONFIRM
    prompt = (f"Purchase plan (JSON lines):\n" + "\n".join(json.dumps({k: p[k] for k in ("mpn", "supplier", "url", "sku", "qty_order", "unit_price_usd")}) for p in parts)
              + f"\n\nMode: {'CHECKOUT AUTHORIZED - authorization phrase: ' + CONFIRM if do_checkout else 'CART FILL ONLY - do not check out.'}\n"
              "Work supplier by supplier. Return the JSON described in your instructions.")
    try:
        res = llm.run(prompt, system=SYSTEM, tools=BROWSER_TOOLS, json_output=True, timeout=3600, budget_usd=8.0)
    except LLMUnavailable as e:
        return {"added": [], "manual": [{"mpn": p["mpn"], "supplier": p["supplier"], "reason": f"agent unavailable: {e}", "url": p["url"]} for p in parts],
                "note": "agent purchasing unavailable; use shopping_list.md"}
    data = res.data if isinstance(res.data, dict) else {}
    # anything the agent did not report is returned to the user as manual
    done = {a.get("mpn") for a in data.get("added", [])} | {m.get("mpn") for m in data.get("manual", [])}
    data.setdefault("manual", [])
    for p in parts:
        if p["mpn"] not in done:
            data["manual"].append({"mpn": p["mpn"], "supplier": p["supplier"], "reason": "not reported by agent", "url": p["url"]})
    data["checkout_authorized"] = do_checkout
    return data
