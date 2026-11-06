"""System plan: the contract between the supervisor LLM (or a template) and the deterministic pipeline.

A plan lists *boards* (each a set of block instances), *purchased* items (anything bought rather
than built: modules, motors, batteries, power supplies, structure) and *links* between ports.
The plan deliberately holds NO netlists and NO coordinates - those are produced by the
deterministic engines from the constraints the links imply.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass
class BuyAlternative:
    """A purchasable substitute for a block (e.g. a pre-built DC-DC module). Ports are inherited from the block."""
    title: str
    unit_cost_usd: float
    url: str = ""
    supplier: str = ""
    spec: str = ""


@dataclass
class BlockInst:
    id: str
    block: str
    params: dict[str, Any] = field(default_factory=dict)
    buy_alternative: BuyAlternative | None = None
    prefer: str = "auto"                 # auto | build | buy


@dataclass
class BoardNode:
    id: str
    title: str = ""
    qty: int = 1
    blocks: list[BlockInst] = field(default_factory=list)
    layers: int | None = None
    copper_oz: float = 1.0
    power_duty: float = 1.0              # fraction of rated bus current drawn simultaneously across the fleet
    max_width_mm: float | None = None
    max_height_mm: float | None = None
    notes: str = ""


@dataclass
class PurchasedItem:
    id: str
    title: str
    category: str = "module"             # power | actuator | battery | compute | sensor | mechanical | harness | module
    qty: int = 1
    unit_cost_usd: float = 0.0
    spec: str = ""
    url: str = ""
    supplier: str = ""
    ports: list[dict] = field(default_factory=list)      # {name, kind, direction, voltage, current_a, signals?, vmin?, vmax?}
    cost_is_estimate: bool = True
    notes: str = ""


@dataclass
class Link:
    a: str                               # "board.block.port" or "item.port"
    b: str
    note: str = ""


@dataclass
class SystemPlan:
    name: str
    request: str = ""
    budget_usd: float | None = None
    boards: list[BoardNode] = field(default_factory=list)
    purchased: list[PurchasedItem] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    # ---- (de)serialisation ------------------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1)

    @staticmethod
    def from_dict(d: dict) -> "SystemPlan":
        boards = []
        for b in d.get("boards", []):
            blocks = []
            for bi in b.get("blocks", []):
                alt = bi.get("buy_alternative")
                blocks.append(BlockInst(bi["id"], bi["block"], dict(bi.get("params") or {}),
                                        BuyAlternative(**alt) if alt else None, bi.get("prefer", "auto")))
            kw = {k: v for k, v in b.items() if k != "blocks" and k in BoardNode.__dataclass_fields__}
            boards.append(BoardNode(blocks=blocks, **kw))
        items = [PurchasedItem(**{k: v for k, v in p.items() if k in PurchasedItem.__dataclass_fields__}) for p in d.get("purchased", [])]
        links = [Link(**{k: v for k, v in l.items() if k in Link.__dataclass_fields__}) for l in d.get("links", [])]
        return SystemPlan(d.get("name", "system"), d.get("request", ""), d.get("budget_usd"), boards, items, links, list(d.get("assumptions", [])))

    # ---- validation -----------------------------------------------------------------------------
    def validate(self, known_blocks: set[str]) -> list[str]:
        errs: list[str] = []
        ids: set[str] = set()
        for b in self.boards:
            if not _ID.match(b.id):
                errs.append(f"board id '{b.id}' must match [A-Za-z][A-Za-z0-9_]*")
            if b.id in ids:
                errs.append(f"duplicate id '{b.id}'")
            ids.add(b.id)
            bids: set[str] = set()
            for bi in b.blocks:
                if not _ID.match(bi.id):
                    errs.append(f"{b.id}: block id '{bi.id}' invalid")
                if bi.id in bids:
                    errs.append(f"{b.id}: duplicate block id '{bi.id}'")
                bids.add(bi.id)
                if bi.block not in known_blocks:
                    errs.append(f"{b.id}.{bi.id}: unknown block '{bi.block}' (known: {sorted(known_blocks)})")
            if b.qty < 1:
                errs.append(f"{b.id}: qty must be >= 1")
        for p in self.purchased:
            if p.id in ids:
                errs.append(f"duplicate id '{p.id}'")
            ids.add(p.id)
            if not _ID.match(p.id):
                errs.append(f"purchased id '{p.id}' invalid")
            if p.unit_cost_usd < 0:
                errs.append(f"{p.id}: negative cost")
        ports: set[str] = set()
        for p in self.purchased:
            for pt in p.ports:
                ports.add(f"{p.id}.{pt['name']}")
        blocks = {f"{b.id}.{bi.id}" for b in self.boards for bi in b.blocks}
        for l in self.links:
            for end in (l.a, l.b):
                parts = end.split(".")
                if len(parts) == 3:
                    if f"{parts[0]}.{parts[1]}" not in blocks:
                        errs.append(f"link endpoint '{end}': no such board block")
                elif len(parts) == 2:
                    if end not in ports:
                        errs.append(f"link endpoint '{end}': no such purchased-item port")
                else:
                    errs.append(f"link endpoint '{end}' must be board.block.port or item.port")
        return errs
