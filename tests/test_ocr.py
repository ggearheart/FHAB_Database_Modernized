"""Coordinate extraction from CoC text (the OCR image step degrades gracefully elsewhere)."""

import pytest

from fhab.ocr import OcrUnavailable, extract_coords, ocr_pdf_coords


def test_signed_decimal():
    c = extract_coords("Sampling point: 38.052323, -122.867276 collected 6/2")
    assert c["lat"] == 38.052323 and c["lon"] == -122.867276


def test_labeled_decimal_with_hemispheres():
    c = extract_coords("Lat: 38.9 N  Long: 122.7 W")
    assert c["lat"] == 38.9 and c["lon"] == -122.7


def test_bare_western_longitude_made_negative():
    # Many CoC forms write the western longitude as a positive number.
    c = extract_coords("Latitude 37.95451 Longitude 122.718")
    assert c["lat"] == 37.95451 and c["lon"] == -122.718


def test_dms():
    c = extract_coords('Location 38°03\'08"N 122°52\'02"W')
    assert abs(c["lat"] - 38.0522) < 0.01 and abs(c["lon"] + 122.8672) < 0.01


def test_none_when_absent_or_implausible():
    assert extract_coords("no coordinates here, batch 943") is None
    assert extract_coords("") is None
    # latitude out of continental-US range -> rejected
    assert extract_coords("12.3, -200.0") is None


def test_ocr_pdf_coords_degrades_without_toolchain(tmp_path, monkeypatch):
    # Simulate a missing OCR toolchain -> OcrUnavailable (so the route can 503 gracefully).
    import fhab.ocr as ocr
    monkeypatch.setattr(ocr, "ocr_pdf_text",
                        lambda *a, **k: (_ for _ in ()).throw(OcrUnavailable("no tesseract")))
    with pytest.raises(OcrUnavailable):
        ocr_pdf_coords(tmp_path / "coc.pdf")
