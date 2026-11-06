"""Supplier link/offer helpers. Search URLs are deterministic and never claim a SKU."""
from __future__ import annotations

from urllib.parse import quote_plus

from ..model import Offer

SEARCH_URLS = {
    "digikey": "https://www.digikey.com/en/products/result?keywords={q}",
    "mouser": "https://www.mouser.com/c/?q={q}",
    "lcsc": "https://www.lcsc.com/search?q={q}",
    "jlcpcb": "https://jlcpcb.com/parts/componentSearch?searchTxt={q}",
    "arrow": "https://www.arrow.com/en/products/search?q={q}",
    "newark": "https://www.newark.com/search?st={q}",
    "tme": "https://www.tme.eu/en/katalog/?search={q}",
}

# order of preference when choosing where to buy prototype quantities
DEFAULT_SUPPLIERS = ("digikey", "mouser", "lcsc")


def search_url(supplier: str, query: str) -> str:
    return SEARCH_URLS[supplier].format(q=quote_plus(query))


def search_offers(query: str, price: float | None = None, suppliers=DEFAULT_SUPPLIERS,
                  breaks: list[tuple[int, float]] | None = None) -> list[Offer]:
    """Offers that are just parametric/MPN searches with an *estimated* price."""
    return [Offer(s, "", search_url(s, query), price, breaks or [], 1, None, True, "") for s in suppliers]
