"""Tiny S-expression reader/writer (enough for KiCad files)."""
from __future__ import annotations
import re

_TOK = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def parse(text: str):
    """Parse text to nested lists. Quoted strings become `str` marked via Q subclass."""
    pos, n = 0, len(text)
    stack: list[list] = [[]]
    while pos < n:
        m = _TOK.match(text, pos)
        if not m:
            break
        pos = m.end()
        if m.group(1):
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif m.group(2):
            if len(stack) > 1:
                stack.pop()
        elif m.group(3) is not None:
            stack[-1].append(Q(m.group(3).replace('\\"', '"').replace("\\\\", "\\")))
        else:
            stack[-1].append(m.group(4))
    return stack[0][0] if stack[0] else []


class Q(str):
    """A quoted string atom."""


def find_all(node: list, head: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def find(node: list, head: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == head:
            return c
    return None


def fnum(x) -> float:
    return float(x)


def dump(node, indent: int = 0, width: int = 100) -> str:
    """Pretty-print; short lists on one line, long ones broken across lines."""
    if isinstance(node, Q):
        return '"' + str(node).replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float)):
        return _num(node)
    flat = "(" + " ".join(dump(c, 0) for c in node) + ")"
    if len(flat) + indent <= width and "\n" not in flat:
        return flat
    pad = "  " * (indent + 1)
    head = []
    rest = list(node)
    while rest and not isinstance(rest[0], list):
        head.append(dump(rest.pop(0)))
    out = "(" + " ".join(head)
    for c in rest:
        out += "\n" + pad + dump(c, indent + 1)
    return out + ")"


def _num(v) -> str:
    if isinstance(v, int):
        return str(v)
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s
