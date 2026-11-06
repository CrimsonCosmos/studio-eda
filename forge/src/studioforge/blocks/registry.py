"""Block registry: deterministic, parameterised subassembly builders.

A block turns *requirements* (voltages, currents, interface needs) into a concrete Circuit
using engineering calculations and the parts library. Blocks expose Ports; the system resolver
reads those ports to decide what its neighbours must provide - that is how one subassembly's
needs fix the exact inputs of another.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..model import Circuit
from ..parts.db import PartsLibrary


@dataclass
class Param:
    name: str
    type: str = "number"
    default: Any = None
    doc: str = ""
    required: bool = False


@dataclass
class BlockDef:
    name: str
    summary: str
    params: list[Param]
    fn: Callable[..., Circuit]
    category: str = "general"
    auto_params: tuple = ()              # params the system resolver may fill from neighbours

    def describe(self) -> dict:
        return {"name": self.name, "summary": self.summary, "category": self.category, "auto_params": list(self.auto_params),
                "params": [{"name": p.name, "type": p.type, "default": p.default, "doc": p.doc, "required": p.required} for p in self.params]}


REGISTRY: dict[str, BlockDef] = {}


def block(name: str, summary: str, params: list[Param], category: str = "general", auto: tuple = ()):
    def deco(fn):
        REGISTRY[name] = BlockDef(name, summary, params, fn, category, auto)
        return fn
    return deco


def describe_blocks() -> list[dict]:
    _load()
    return [b.describe() for b in REGISTRY.values()]


def _load() -> None:
    import importlib
    for mod in ("power", "mcu", "comms", "motor", "misc"):
        try:
            importlib.import_module(f"{__package__}.{mod}")
        except ModuleNotFoundError as e:           # module not shipped yet
            if e.name != f"{__package__}.{mod}":
                raise


def build_block(lib: PartsLibrary, name: str, params: dict | None = None, instance: str | None = None) -> Circuit:
    _load()
    if name not in REGISTRY:
        raise KeyError(f"unknown block '{name}'. Known: {sorted(REGISTRY)}")
    bd = REGISTRY[name]
    kw: dict[str, Any] = {}
    given = dict(params or {})
    for p in bd.params:
        if p.name in given and given[p.name] not in (None, "auto"):
            kw[p.name] = given.pop(p.name)
        elif p.required and p.default is None:
            raise ValueError(f"block {name}: missing required param '{p.name}'")
        else:
            kw[p.name] = p.default
            given.pop(p.name, None)
    if given:
        raise ValueError(f"block {name}: unknown params {sorted(given)}")
    c = bd.fn(lib, **kw)
    c.name = instance or c.name or name
    return c
