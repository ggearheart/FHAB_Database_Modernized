"""Best-effort coordinate extraction from chain-of-custody PDFs.

The CoC forms that arrive with lab email folders are usually *scanned images* (no text layer),
so pulling the sampling coordinates off them needs OCR. `extract_coords` (pure text -> lat/lon)
is always available and unit-tested; `ocr_pdf_coords` adds the image->text step and degrades
gracefully (raises OcrUnavailable) when the OCR toolchain — Tesseract + Poppler — is not
installed, so the manual lat/long entry path still works everywhere.
"""

from __future__ import annotations

import re
from pathlib import Path


class OcrUnavailable(RuntimeError):
    """Raised when the OCR toolchain (pytesseract / pdf2image / tesseract) is not available."""


# Decimal degrees, optionally signed or with a N/S/E/W hemisphere suffix or lat/long labels.
_DEC = r"[-+]?\d{1,3}(?:\.\d+)"
_DECIMAL_RE = re.compile(
    rf"(?:lat(?:itude)?\D{{0,4}})?(?P<lat>{_DEC})\s*(?P<lath>[NnSs])?"
    rf"[,;/\s]+(?:lon(?:g(?:itude)?)?\D{{0,4}})?(?P<lon>{_DEC})\s*(?P<lonh>[EeWw])?", re.I)
# Degrees-minutes-seconds, e.g. 38°03'08"N 122°52'02"W
_DMS = r"(\d{1,3})[°d ]\s*(\d{1,2})['m ]\s*(\d{1,2}(?:\.\d+)?)[\"s ]*\s*([NSEWnsew])"
_DMS_PAIR_RE = re.compile(_DMS + r"[,;/\s]+" + _DMS, re.I)


def _dms_to_dd(deg, minute, sec, hemi) -> float:
    dd = float(deg) + float(minute) / 60 + float(sec) / 3600
    return -dd if hemi.upper() in ("S", "W") else dd


def extract_coords(text: str) -> dict | None:
    """Find the first plausible (latitude, longitude) in `text`. Returns {lat, lon, raw} or None.

    Handles decimal degrees (signed, or with N/S/E/W or lat/long labels) and DMS. California
    freshwater is lat 32..42, lon -114..-124, so we sanity-check the ranges and only accept a
    match whose latitude/longitude are geographically consistent (lon negative in the US west).
    """
    if not text:
        return None
    text = text.replace("\n", " ")

    for m in _DMS_PAIR_RE.finditer(text):
        lat = _dms_to_dd(*m.group(1, 2, 3, 4))
        lon = _dms_to_dd(*m.group(5, 6, 7, 8))
        if _plausible(lat, lon):
            return {"lat": round(lat, 6), "lon": round(lon, 6), "raw": m.group(0).strip()}

    for m in _DECIMAL_RE.finditer(text):
        lat, lon = float(m.group("lat")), float(m.group("lon"))
        if m.group("lath") and m.group("lath").upper() == "S":
            lat = -abs(lat)
        if m.group("lonh") and m.group("lonh").upper() == "W":
            lon = -abs(lon)
        # A bare western-US longitude is written positive on many forms; make it negative.
        if m.group("lonh") is None and lon > 0 and 114 <= lon <= 125:
            lon = -lon
        if _plausible(lat, lon):
            return {"lat": round(lat, 6), "lon": round(lon, 6), "raw": m.group(0).strip()}
    return None


def _plausible(lat: float, lon: float) -> bool:
    # Continental US-ish bounds, with California comfortably inside.
    return 24 <= lat <= 49 and -125 <= lon <= -66


def ocr_pdf_text(path, *, max_pages: int = 3, dpi: int = 200) -> str:
    """OCR the first `max_pages` pages of a (scanned) PDF to text. Raises OcrUnavailable."""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except Exception as exc:  # library not installed
        raise OcrUnavailable(f"OCR libraries not installed: {exc}") from exc
    try:
        images = convert_from_path(str(Path(path)), dpi=dpi, first_page=1, last_page=max_pages)
        return "\n".join(pytesseract.image_to_string(im) for im in images)
    except Exception as exc:  # tesseract/poppler binary missing, or unreadable file
        raise OcrUnavailable(f"OCR failed (is Tesseract + Poppler installed?): {exc}") from exc


def ocr_pdf_coords(path) -> dict | None:
    """OCR a CoC PDF and extract coordinates. Returns {lat, lon, raw} or None. Raises OcrUnavailable."""
    return extract_coords(ocr_pdf_text(path))
