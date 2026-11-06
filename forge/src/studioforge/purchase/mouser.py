"""Live price/stock refresh via the Mouser Search API (needs MOUSER_API_KEY; written from the public
API description, unit-tested with a mocked response, not exercised against the live service here)."""
from __future__ import annotations

import os
import re

from ..model import Offer, PartDef


def parse_part_response(js: dict, mpn: str) -> Offer | None:
    parts = (js.get("SearchResults") or {}).get("Parts") or []
    for p in parts:
        if (p.get("ManufacturerPartNumber") or "").lower() == mpn.lower():
            breaks = []
            for pb in p.get("PriceBreaks", []):
                m = re.search(r"[\d.,]+", pb.get("Price", "").replace(",", ""))
                if m:
                    breaks.append((int(pb["Quantity"]), float(m.group(0))))
            stock = re.search(r"\d+", (p.get("Availability") or "").replace(",", ""))
            return Offer("mouser", p.get("MouserPartNumber", ""), p.get("ProductDetailUrl", ""), breaks[0][1] if breaks else None, breaks,
                         int(p.get("Min", 1) or 1), int(stock.group(0)) if stock else None, False, "")
    return None


def refresh(part: PartDef, key: str | None = None, session=None) -> Offer | None:
    key = key or os.environ.get("MOUSER_API_KEY")
    if not key:
        return None
    import requests
    s = session or requests
    r = s.post(f"https://api.mouser.com/api/v1/search/partnumber?apiKey={key}",
               json={"SearchByPartRequest": {"mouserPartNumber": part.mpn, "partSearchOptions": "Exact"}}, timeout=30)
    r.raise_for_status()
    offer = parse_part_response(r.json(), part.mpn)
    if offer:
        part.offers = [o for o in part.offers if o.supplier != "mouser"] + [offer]
    return offer
