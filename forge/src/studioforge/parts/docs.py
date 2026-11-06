"""Documentation cache: download official datasheets, extract text, cross-check pin names."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) studioforge/0.1 (+datasheet-cache)"
MAX_PDF = 120 * 1024 * 1024


def safe_name(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "_", mpn)


def is_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_pdf(url: str, dest: Path, timeout: int = 60) -> tuple[bool, str]:
    """Download `url` to `dest`; only accepts real PDFs. Returns (ok, message)."""
    import requests
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"},
                         timeout=timeout, stream=True, allow_redirects=True)
    except Exception as e:  # network errors, proxy denials
        return False, f"request failed: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code} (site may block automated clients; agent fallback needed)"
    tmp = dest.with_suffix(".part")
    size = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            size += len(chunk)
            if size > MAX_PDF:
                tmp.unlink(missing_ok=True)
                return False, "file too large"
            f.write(chunk)
    if not is_pdf(tmp):
        tmp.unlink(missing_ok=True)
        return False, "response was not a PDF"
    os.replace(tmp, dest)
    return True, f"{size} bytes"


def pdf_to_text(pdf: Path) -> Path | None:
    txt = pdf.with_suffix(".txt")
    if txt.exists() and txt.stat().st_mtime >= pdf.stat().st_mtime:
        return txt
    exe = shutil.which("pdftotext")
    if exe:
        try:
            subprocess.run([exe, "-layout", str(pdf), str(txt)], check=True, capture_output=True, timeout=120)
            return txt
        except Exception:
            pass
    try:
        from pypdf import PdfReader  # type: ignore
        text = "\n".join((p.extract_text() or "") for p in PdfReader(str(pdf)).pages)
        txt.write_text(text)
        return txt
    except Exception:
        return None


def check_pin_names(text: str, names: list[str]) -> tuple[list[str], list[str]]:
    """Which pin names appear in datasheet text (case-insensitive, tolerant of ~ / _ and overbars)."""
    hay = re.sub(r"[\s_~\\/\-]", "", text).lower()
    found, missing = [], []
    for n in dict.fromkeys(names):
        key = re.sub(r"[\s_~\\/\-]", "", n).lower()
        if not key or key in {"nc", "pad", "ep", "1", "2", "3", "4", "5", "6", "7", "8", "+", "-"}:
            continue
        (found if key in hay else missing).append(n)
    return found, missing


def pin_evidence(text: str, pins: list[tuple[str, str]]) -> dict[str, bool]:
    """Heuristic number<->name check: in the pin table the pin number and name sit close together
    (either order). Returns {pin_number: found}. Weak for tiny numbers, strong for names."""
    flat = re.sub(r"\s+", " ", text.replace("\u2013", "-").replace("\u2212", "-").replace("\u2014", "-"))
    syn = {"V-": ("GND", "VSS", "VEE", "V-"), "V+": ("VCC", "VDD", "VS", "VCC+", "V+"), "GND": ("GND", "VSS", "AGND", "PGND")}
    res: dict[str, bool] = {}
    for num, name in pins:
        n = re.sub(r"[~\\]", "", name).strip().replace("\u2212", "-")
        if not n or n.upper() in {"NC", "PAD", "EP"}:
            continue
        ok = False
        for cand in syn.get(n, (n,)):
            nm = re.escape(cand).replace(r"\ ", r"\s*")
            pat = rf"(?:\b{re.escape(num)}\b.{{0,40}}{nm}|{nm}.{{0,40}}\b{re.escape(num)}\b)"
            if re.search(pat, flat, re.I):
                ok = True
                break
        res[num] = ok
    return res
