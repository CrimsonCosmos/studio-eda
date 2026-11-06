"""Fetch + verify official documentation for catalog parts."""
from __future__ import annotations

from . import docs
from ..model import PartDef


def fetch_docs(lib, part: PartDef) -> tuple[bool, str]:
    """Download the datasheet into the cache (deterministic path). Blocked sites => (False, reason),
    and the caller can hand the part to the research agent."""
    if not part.datasheet_url:
        return False, "no datasheet_url on record"
    pdf = lib.doc_dir(part.mpn) / "datasheet.pdf"
    if pdf.exists() and docs.is_pdf(pdf):
        ok, msg = True, "cached"
    else:
        ok, msg = docs.download_pdf(part.datasheet_url, pdf)
    if ok:
        rel = str(pdf.relative_to(lib.root))
        if rel not in part.docs:
            part.docs.append(rel)
        (pdf.parent / "sha256.txt").write_text(docs.sha256(pdf) + "  datasheet.pdf\n")
        lib.put(part)
    return ok, msg


def verify_part(lib, part: PartDef, threshold: float = 0.95) -> dict:
    """Cross-check the catalog pinout against the cached datasheet text."""
    ok, msg = fetch_docs(lib, part)
    if not ok:
        return {"mpn": part.mpn, "status": "no-datasheet", "detail": msg}
    pdf = lib.doc_dir(part.mpn) / "datasheet.pdf"
    txt = docs.pdf_to_text(pdf)
    if not txt:
        return {"mpn": part.mpn, "status": "no-text", "detail": "pdftotext failed (scanned PDF?)"}
    text = txt.read_text(errors="ignore")
    ev = docs.pin_evidence(text, [(p.number, p.name) for p in part.pins])
    bad = sorted(k for k, v in ev.items() if not v)
    frac = (len(ev) - len(bad)) / max(1, len(ev))
    part.verified = frac >= threshold
    part.notes = (part.notes + f" [pinout check vs cached datasheet: {len(ev) - len(bad)}/{len(ev)} pins matched]").strip()
    lib.put(part)
    return {"mpn": part.mpn, "status": "verified" if part.verified else "MISMATCH", "matched": len(ev) - len(bad),
            "total": len(ev), "unmatched_pins": bad}
