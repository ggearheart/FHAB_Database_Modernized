"""Ingest folders of Bend Genetics / partner lab email attachments.

Each folder is one lab batch: a wide-format results CSV (analytes across columns) plus the
supporting PDFs that arrived with the email — a chain-of-custody form, a transmittal letter,
and a sample-receipt form. This module

  1. converts the wide CSV into CEDEN WaterChemistry long rows (one row per analyte value),
  2. materializes unlinked samples + results (geocoded from the CEDEN station registry) via the
     shared CEDEN loader, so they land in the lab-data workboard for reconciliation, and
  3. keeps the original files on the batch (`lab_batch_file`) for full provenance on the
     batch workboard.

The CEDEN station code sits in *either* the Location or the Customer Sample column depending on
the submitter, so we pick whichever one the CEDEN station registry knows (falling back to
Location). Rows whose station is not in the registry stay ungeocoded and surface as unlinked
work for staff — coordinates for those live on the (often scanned) chain-of-custody form.
"""

from __future__ import annotations

import csv
import io
import mimetypes
import re
import tempfile
import uuid
from pathlib import Path

import psycopg

from .ceden import load_ceden_output
from .parsing import clean

# --- analyte column parsing -------------------------------------------------------------

# Normalize Bend's toxin/pigment column names to the analyte taxonomy (note Bend's
# "Chloropyhll" typo). qPCR gene columns are handled separately below.
_NAME_FIX = {
    "microcystin/nod.": "Microcystins",
    "microcystin": "Microcystins",
    "anatoxin-a": "Anatoxin-a",
    "cylindrospermopsin": "Cylindrospermopsin",
    "saxitoxin": "Saxitoxin",
    "chloropyhll-a": "Chlorophyll a",
    "chlorophyll-a": "Chlorophyll a",
    "pheophytin-a": "Pheophytin a",
}
_QPCR_GENE = {
    "anac": "anaC gene", "mcye": "mcyE gene", "cyra": "cyrA gene",
    "sxta": "sxtA gene", "cy16s": "Cyanobacteria 16S rRNA gene",
}
_COL_RE = re.compile(r"^\s*(?P<name>.*?)\s*\((?P<unit>[^)]*)\)\s*$")


def parse_analyte_columns(header: list[str]) -> dict[str, dict]:
    """Map each analyte column header to {analyte, method, unit, fraction, matrix}.

    Recognizes ELISA toxins (ug/L or toxins/g), qPCR gene targets (copies/mL or /g), and
    chlorophyll/pheophytin pigments. A "/g" unit means a dry-weight tissue/mat result.
    """
    out: dict[str, dict] = {}
    for col in header:
        m = _COL_RE.match(col or "")
        if not m:
            continue
        name, unit = m.group("name").strip(), m.group("unit").strip()
        low = name.lower()
        dry = unit.endswith("/g")
        if low.startswith("qpcr"):
            gene = re.sub(r"^qpcr[-\s]*", "", low)
            analyte = _QPCR_GENE.get(gene, f"{name.split('-', 1)[-1]} gene")
            method = "qPCR"
        elif low in _NAME_FIX and ("chloro" in low or "pheophytin" in low):
            analyte, method = _NAME_FIX[low], "Spectrophotometry"
        elif low in _NAME_FIX:
            analyte, method = _NAME_FIX[low], "ELISA"
        else:
            continue
        out[col] = {
            "analyte": analyte, "method": method, "unit": unit,
            "fraction": "Dry Weight" if dry else "Total",
            "matrix": "sampletissue" if dry else "samplewater",
        }
    return out


def _value(raw: str | None) -> tuple[str, str] | None:
    """Parse a Bend result cell -> (result, res_qual_code), or None to skip (not analyzed)."""
    v = (raw or "").strip()
    if v == "" or v in {"-", "--", "NA", "N/A"}:
        return None
    up = v.upper()
    if up == "ND":
        return ("", "ND")
    if up in {"DNQ", "BDL"}:
        return ("", up)
    if v[0] in "<>":
        return (v[1:].strip(), v[0])
    return (v, "")


def bend_wide_to_ceden(rows: list[dict], header: list[str], pick_station) -> list[dict]:
    """Convert wide Bend result rows into CEDEN WaterChemistry long-format dict rows.

    `pick_station(row)` returns (station_code, station_name) for the row.
    """
    cols = parse_analyte_columns(header)
    out: list[dict] = []
    for row in rows:
        code, name = pick_station(row)
        base = {
            "StationCode": clean(code), "StationName": clean(name),
            "SampleDate": clean(row.get("Collected")), "SampleTime": clean(row.get("Time")),
            "ProjectCode": clean(row.get("Project")), "LabBatch": clean(row.get("Batch")),
            "BG_ID": clean(row.get("BG_ID")), "SampleTypeCode": clean(row.get("Sample Type")),
            "LabSampleID": clean(row.get("Sample ID")),
        }
        for col, meta in cols.items():
            parsed = _value(row.get(col))
            if parsed is None:
                continue
            result, rqc = parsed
            out.append({**base, "Analyte": meta["analyte"], "MethodName": meta["method"],
                        "Result": result, "ResQualCode": rqc, "Units": meta["unit"],
                        "Fraction": meta["fraction"], "MatrixName": meta["matrix"]})
    return out


# --- Bend "analytical report" layout (2022–2023 exports) --------------------------------
#
# Older Bend results files are the printed lab report saved as CSV/XLSX rather than a flat results
# table: a letterhead block (Project / Project # / Reported) repeated on every page, then titled
# sections — ANALYTICAL REPORT FOR SAMPLES (one row per sample: Location, Date Collected, Matrix,
# BG_ID), SAMPLE RESULTS (already long: Sample ID, Method, Target, Result, Quantitation Limit,
# Units, Notes), MICROSCOPY RESULTS (Dominant / Sub-dominant / Also present + a narrative note),
# then QUALITY CONTROL and the qualifier legend, which are not sample data.

_REPORT_SECTIONS = (("ANALYTICAL REPORT FOR SAMPLES", "samples"), ("SAMPLE RESULTS", "results"),
                    ("MICROSCOPY RESULTS - ENUMERATION", "enumeration"),   # before the generic one
                    ("MICROSCOPY RESULTS", "microscopy"), ("QUALITY CONTROL", None),
                    ("QUALIFIERS", None))
# In the report, a qPCR "Target" names the toxin its gene marker codes for.
_QPCR_TARGET_GENE = {"anatoxin-a": "anaC gene", "anatoxin": "anaC gene",
                     "cylindrospermopsin": "cyrA gene", "microcystin": "mcyE gene",
                     "microcystin/nod.": "mcyE gene", "saxitoxin": "sxtA gene",
                     "16s": "Cyanobacteria 16S rRNA gene",
                     "cyanobacteria 16s": "Cyanobacteria 16S rRNA gene"}
_MICRO_ROLES = (("Dominant", "dominant"), ("Sub-dominant", "sub-dominant"),
                ("Also present", "also present"))
_STATION_CODE_RE = re.compile(r"^\d{3}[A-Z0-9][A-Z0-9_\-]{2,}$", re.I)   # e.g. 204LME025, 909CCCSPX
# Report header wording varies by year ("Target Analyte", "Quantification Limit" …).
_HEADER_ALIAS = {"target analyte": "Target", "analyte": "Target",
                 "quantification limit": "Quantitation Limit", "quantitation limit": "Quantitation Limit",
                 "reporting limit": "Quantitation Limit", "ql": "Quantitation Limit"}
# Some (2022) reports give the sampling point as "lat lon" in the Location column.
_LATLON_RE = re.compile(r"^\s*(-?\d{1,3}\.\d{3,})\s*[,\s]\s*(-?\d{1,3}\.\d{3,})\s*$")
_NUTRIENT_RE = re.compile(r"nitrogen|phosph|ammonia|nitrate|nitrite|\bTKN\b", re.I)
_PIGMENT_RE = re.compile(r"chlorophyll|pheophytin|phycocyanin", re.I)


def _latlon(v: str | None):
    """'38.32465 -122.34848' -> (lat, lon) when it's a plausible California point, else None."""
    m = _LATLON_RE.match(v or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    return (lat, lon) if 32.0 <= lat <= 42.2 and -124.7 <= lon <= -113.9 else None


def _analysis_type(method: str, analyte: str) -> str | None:
    """Analysis type for a report row: by analyte for pigments/nutrients (both are measured by
    spectrophotometry/fluorometry), else by method; None lets the loader's method default apply."""
    if _PIGMENT_RE.search(analyte or ""):
        return "Pigment"
    if _NUTRIENT_RE.search(analyte or ""):
        return "Nutrient"
    return {"qPCR": "Genetic", "ELISA": "Cyanotoxin", "Microscopy": "Microscopy",
            "Strip test": "Cyanotoxin"}.get(method)


def is_bend_report(text: str) -> bool:
    """True when a results file is the sectioned Bend lab-report layout (not a flat table)."""
    head = (text or "")[:20000].upper()
    return "ANALYTICAL REPORT FOR SAMPLES" in head or "SAMPLE RESULTS" in head


def _is_header(first_label: str, section: str | None) -> bool:
    """A table's header row. Some reports label the results/microscopy tables' first column
    'Location' though it holds the Sample ID ("Vial 1"); the sample table itself says Sample ID."""
    low = (first_label or "").strip().lower()
    return low == "sample id" or (low == "location" and section not in (None, "samples"))


def parse_bend_report(text: str) -> dict:
    """Split a Bend lab-report export into {meta, samples, results, microscopy}.

    Page letterheads/footers start with an empty first cell and are skipped; a section title
    switches the active table; a 'Sample ID' row (re)sets its column header, so tables that
    continue across pages keep parsing."""
    meta: dict = {}
    samples: dict[str, dict] = {}
    results: list[dict] = []
    micro: list[dict] = []
    enum: list[dict] = []
    section, header = None, None
    for raw in csv.reader(io.StringIO(text)):
        cells = [(c or "").strip() for c in raw]
        for i, c in enumerate(cells[:-1]):            # letterhead: "Project:", "Reported:" …
            if c.endswith(":") and cells[i + 1] and c[:-1] not in meta:
                meta[c[:-1]] = cells[i + 1]
        first = cells[0] if cells else ""
        if not first:
            continue
        title = next((sec for key, sec in _REPORT_SECTIONS if first.upper().startswith(key)), "")
        if title != "":
            section, header = title, None
            continue
        if section is None:
            continue
        if _is_header(first, section):
            header = ["Sample ID"] + [_HEADER_ALIAS.get(c.lower(), c) for c in cells[1:]]
            continue
        if header is None:
            continue
        rec: dict = {}
        for k, v in zip(header, cells):
            if k and (k not in rec or not rec[k]):
                rec[k] = v
        if section == "samples":
            samples[first] = rec
        elif section == "results":
            results.append(rec)
        elif section == "enumeration":
            enum.append(rec)
        else:   # microscopy: the narrative sits after the taxon columns (often under a blank header)
            rec["_notes"] = " ".join(c for c in cells[4:] if c)
            micro.append(rec)
    return {"meta": meta, "samples": samples, "results": results, "microscopy": micro,
            "enumeration": enum}


def _num(v: str | None) -> str:
    """'46,075' -> '46075' (thousands separators); other text unchanged."""
    v = (v or "").strip()
    return v.replace(",", "") if re.fullmatch(r"[<>]?[\d,]+(\.\d+)?", v) else v


def _split_collected(v: str | None) -> tuple[str | None, str | None]:
    """'5/4/2023 11:00' -> ('5/4/2023', '11:00'); a 0:00 time means 'not recorded'."""
    parts = (v or "").strip().split()
    if not parts:
        return None, None
    m = re.match(r"(\d{1,2}):(\d{2})", parts[1]) if len(parts) > 1 else None
    if not m:
        return parts[0], None
    h, mi = int(m.group(1)), int(m.group(2))
    ampm = parts[2].upper() if len(parts) > 2 else ""
    if ampm == "PM" and h < 12:
        h += 12
    elif ampm == "AM" and h == 12:
        h = 0
    return parts[0], (None if h == 0 and mi == 0 else f"{h:02d}:{mi:02d}")


def _report_station(sample_id: str, location: str, in_registry) -> tuple[str | None, str | None]:
    """Pick (station_code, station_name) from a report sample row. The CEDEN code sits in either
    Sample ID or Location depending on the submitter: prefer one the registry knows, then one that
    looks like a CEDEN code, then the Location text (a place name to geocode from the CoC)."""
    sid, loc = (sample_id or "").strip(), (location or "").strip()
    label = None if sid.isdigit() else (sid or None)   # a bare running number ("1") isn't an ID
    candidates = ((sid, loc or None), (loc, label))
    for test in (in_registry, _STATION_CODE_RE.match):
        for code, name in candidates:
            if code and test(code):
                return code, name
    if loc:
        return loc, loc
    return (sid or None), None


def bend_report_to_ceden(text: str, in_registry=lambda code: False) -> list[dict]:
    """Convert a Bend lab-report export into CEDEN WaterChemistry long rows (same shape as
    `bend_wide_to_ceden`), including microscopy taxa. QC rows are not sample data and are skipped."""
    rep = parse_bend_report(text)
    project = clean(rep["meta"].get("Project"))
    base_for: dict[str, dict] = {}

    listed = set(rep["samples"])

    def known(sample_id: str) -> bool:
        """A result row must name a sample from the report's sample table (when it has one)."""
        return bool(sample_id) and (not listed or sample_id in listed)

    def base(sample_id: str) -> dict:
        if sample_id not in base_for:
            s = rep["samples"].get(sample_id, {})
            loc = s.get("Location", "")
            ll = _latlon(loc)          # a coordinate pair, not a place: the Sample ID names the site
            code, name = _report_station(sample_id, "" if ll else loc, in_registry)
            d, t = _split_collected(s.get("Date Collected"))
            base_for[sample_id] = {
                "StationCode": clean(code), "StationName": clean(name), "SampleDate": d,
                "SampleTime": t, "ProjectCode": project, "LabBatch": None,
                "BG_ID": clean(s.get("BG_ID")), "SampleTypeCode": clean(s.get("Matrix")),
                "LabSampleID": clean(sample_id),
                "TargetLatitude": ll[0] if ll else None, "TargetLongitude": ll[1] if ll else None}
        return base_for[sample_id]

    out: list[dict] = []
    for r in rep["results"]:
        sid = r.get("Sample ID", "")
        method_raw, target = (r.get("Method") or "").strip(), (r.get("Target") or "").strip()
        if not known(sid) or not target:
            continue
        if method_raw.upper() in ("QPCR", "PCR"):
            method = "qPCR"
            analyte = _QPCR_TARGET_GENE.get(target.lower()) or _QPCR_GENE.get(target.lower(), f"{target} gene")
        else:
            method = "ELISA" if method_raw.upper() == "ELISA" else method_raw
            analyte = _NAME_FIX.get(target.lower(), target)
        parsed = _value(_num(r.get("Result")))
        if parsed is None:
            continue
        result, rqc = parsed
        notes = (r.get("Notes") or "").strip()
        if not rqc and re.search(r"\bC1\b", notes):
            rqc = "DNQ"                                   # reported below the quantitation limit
        unit = (r.get("Units") or "").strip()
        dry = "/g" in unit.replace(" ", "")
        out.append({**base(sid), "Analyte": analyte, "MethodName": method, "Result": result,
                    "AnalysisType": _analysis_type(method, analyte),
                    "ResQualCode": rqc, "Units": unit, "RL": _num(r.get("Quantitation Limit")),
                    "QACode": notes if notes and notes != "U" else None,
                    "Fraction": "Dry Weight" if dry else "Total",
                    "MatrixName": "sampletissue" if dry else "samplewater"})
    for m in rep["microscopy"]:
        sid = m.get("Sample ID", "")
        if not known(sid):
            continue
        found = False
        for col, role in _MICRO_ROLES:
            taxa = (m.get(col) or "").strip()
            if taxa and taxa.upper() not in ("NA", "N/A", "NONE", "-"):
                found = True
                out.append({**base(sid), "Analyte": f"Cyanobacteria taxa ({role})",
                            "MethodName": "Microscopy", "AnalysisType": "Microscopy",
                            "Result": "", "ResQualCode": "",
                            "ResultText": taxa, "Taxa": taxa, "QACode": None,
                            "Units": None, "Fraction": None, "MatrixName": None})
        if not found:   # examined, no cyanobacteria observed
            out.append({**base(sid), "Analyte": "Cyanobacteria taxa (dominant)",
                        "MethodName": "Microscopy", "AnalysisType": "Microscopy",
                        "Result": "", "ResQualCode": "ND",
                        "ResultText": None, "Taxa": None, "QACode": None,
                        "Units": None, "Fraction": None, "MatrixName": None})
    for e in rep["enumeration"]:   # cell counts per genus: density + total biovolume
        sid, genus = e.get("Sample ID", ""), (e.get("Genus") or e.get("Taxa") or "").strip()
        if not known(sid) or not genus:
            continue
        for want, analyte, unit in (("cells/ml", "Cyanobacteria cell density", "cells/mL"),
                                    ("m3/ml", "Cyanobacteria total biovolume", "µm3/mL")):
            col = next((k for k in e if want in k.lower().replace("µ", "u")), None)
            parsed = _value(_num(e.get(col))) if col else None
            if parsed is None:
                continue
            out.append({**base(sid), "Analyte": analyte, "MethodName": "Microscopy",
                        "AnalysisType": "Microscopy", "Result": parsed[0], "ResQualCode": parsed[1],
                        "ResultText": genus, "Taxa": genus, "Units": unit,
                        "ResultKey": f"{analyte}:{genus}", "Fraction": None, "MatrixName": None})
    return out


# --- PDF lab reports ---------------------------------------------------------------------

_PDF_FURNITURE = re.compile(r"Bend Genetics, LLC|Scripps Drive|Sacramento, CA \d|Tel: \(\d|"
                            r"Page \d+ of \d+|Analysis for Tox", re.I)
_PDF_META = re.compile(r"(Project #|Project|Reported):\s+(\S.*?)(?=\s{2,}|$)")
_SPAN = re.compile(r"\S+(?: \S+)*")          # text runs separated by 2+ spaces = table cells


def _spans(line: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group()) for m in _SPAN.finditer(line)]


def _assign(spans, cols) -> list[str]:
    """Place a row's text runs under the header columns: in order, one run per column, choosing the
    left-to-right assignment that best fits the header positions. An empty cell (no Location) leaves
    its column blank, and drift in long rows can't push two values into one column."""
    n, m = len(spans), len(cols)
    cells = [""] * m
    if not n:
        return cells

    def cost(sp, col):
        (s, e), (cs, ce) = sp[:2], col[:2]
        return max(0, max(s, cs) - min(e, ce)) + 0.01 * abs((s + e) - (cs + ce)) / 2

    if n > m:   # more runs than columns (shouldn't happen): fall back to nearest column
        for sp in spans:
            i = min(range(m), key=lambda j: cost(sp, cols[j]))
            cells[i] = f"{cells[i]} {sp[2]}".strip()
        return cells
    inf = float("inf")
    best = [[inf] * m for _ in range(n)]
    back = [[-1] * m for _ in range(n)]
    for j in range(m):
        best[0][j] = cost(spans[0], cols[j])
    for i in range(1, n):
        run_min, run_arg = inf, -1
        for j in range(m):
            if j >= 1 and best[i - 1][j - 1] < run_min:
                run_min, run_arg = best[i - 1][j - 1], j - 1
            if run_arg >= 0:
                best[i][j] = run_min + cost(spans[i], cols[j])
                back[i][j] = run_arg
    j = min(range(m), key=lambda k: best[n - 1][k])
    for i in range(n - 1, -1, -1):
        cells[j] = spans[i][2]
        j = back[i][j]
    return cells


def bend_pdf_to_csv(text: str) -> str:
    """Re-lay a Bend PDF report's text (pypdf layout mode) as the CSV export's rows, so one parser
    serves both: letterhead -> meta, section titles, 'Sample ID' headers (merging a wrapped label
    such as 'Quantitation' / 'Limit'), and data rows placed by column position. Wrapped lines
    (long notes) join the row above; page furniture and the cover letter are dropped."""
    rows: list[list[str]] = []
    section, cols, above, last = None, None, [], None
    for line in text.splitlines():
        for k, v in _PDF_META.findall(line):
            rows.append(["", f"{k}:", v.strip()])
        if not line.strip():
            above, last = [], None
            continue
        if _PDF_FURNITURE.search(line):
            continue
        stripped = line.strip()
        title = next((sec for key, sec in _REPORT_SECTIONS if stripped.upper().startswith(key)), "")
        if title != "":
            section, cols, above, last = title, None, [], None
            rows.append([stripped])
            continue
        if section is None:
            continue
        sp = _spans(line)
        if sp and _is_header(sp[0][2], section):
            cols = [list(x) for x in sp]
            cols[0][2] = "Sample ID"
            for s, e, t in above:                   # a header label wrapped onto the line above
                labels = [c[2].lower() for c in cols]
                if t.lower().startswith(("quantita", "quantifica", "reporting")) and "limit" in labels:
                    i = labels.index("limit")       # "Quantitation" / "Limit"
                else:
                    i = min(range(len(cols)), key=lambda j: abs((s + e) / 2 - (cols[j][0] + cols[j][1]) / 2))
                cols[i][2] = f"{t} {cols[i][2]}"
            rows.append([c[2] for c in cols])
            above, last = [], None
            continue
        if cols is None:
            above = sp
            continue
        notes_i = len(cols) - 1 if cols[-1][2].lower().startswith("notes") and len(cols) > 1 else None
        if notes_i is not None:     # free text: everything right of the previous column is the note
            edge = cols[notes_i - 1][1] + 3
            head = [x for x in sp if x[0] <= edge]
            note = " ".join(x[2] for x in sp if x[0] > edge)
            cells = _assign(head, cols[:notes_i]) + [note]
        else:
            cells, note = _assign(sp, cols), ""
        if cells[0] and sum(1 for c in cells if c) >= 2:   # a lone word (a photo caption) isn't a row
            rows.append(cells)
            last = cells
        elif last is not None and not cells[0]:   # a wrapped line: only ever extends the note
            if notes_i is not None and note:
                last[notes_i] = f"{last[notes_i]} {note}".strip()
        else:
            above = sp
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


_PAGE_FIELD = re.compile(r"(Received|Matrix|BG ID|Customer|Project|Location|Collected|Sample ID):"
                         r"\s+(\S.*?)(?=\s{2,}|$)")


def is_bend_page_report(text: str) -> bool:
    """The 2025 Bend PDF layout: one sample per page (key/value header + a results table)."""
    return "BG ID:" in (text or "") and "ANALYTICAL REPORT FOR SAMPLES" not in (text or "")


def bend_pages_to_csv(text: str) -> str:
    """Re-lay a one-sample-per-page Bend PDF (2025) as the sectioned report's rows: each page's
    header fields become a sample-table row, its results table (Method / Analyte / Result /
    Reporting Limit / Units / Qualifiers) the result rows — so the shared parser handles it."""
    samples, results = [], []
    page: dict = {}
    cols, sid = None, None
    for line in text.splitlines():
        if re.search(r"Page \d+ of \d+", line):
            cols = None
            continue
        fields = _PAGE_FIELD.findall(line)
        if fields:
            if "Received" in dict(fields):
                page = {}                               # a new sample page starts
            page.update({k: v.strip() for k, v in fields})
            continue
        if line.strip().upper().startswith("SAMPLE RESULTS"):
            sid = page.get("Sample ID") or page.get("BG ID")
            if sid and sid not in {r[0] for r in samples}:
                samples.append([sid, page.get("Location", ""), page.get("Collected", ""),
                                page.get("Received", ""), page.get("Matrix", ""), "",
                                page.get("BG ID", "")])
            cols = []
            continue
        if cols is None or not line.strip():
            continue
        sp = _spans(line)
        if cols == []:                                  # the results table's header line
            cols = [list(x) for x in sp]
            continue
        rec = dict(zip([c[2].lower() for c in cols], _assign(sp, cols)))
        if rec.get("method") and rec.get("analyte"):
            results.append([sid, rec["method"], rec["analyte"], rec.get("result", ""),
                            rec.get("reporting limit", ""), rec.get("units", ""),
                            rec.get("qualifiers", "")])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["", "Project:", page.get("Project", "")])
    w.writerow(["ANALYTICAL REPORT FOR SAMPLES"])
    w.writerow(["Sample ID", "Location", "Date Collected", "Date Received", "Matrix", "Preserved", "BG_ID"])
    w.writerows(samples)
    w.writerow(["SAMPLE RESULTS"])
    w.writerow(["Sample ID", "Method", "Target", "Result", "Quantitation Limit", "Units", "Notes"])
    w.writerows(results)
    return buf.getvalue()


def is_watsonville_report(text: str) -> bool:
    head = (text or "")[:4000]
    return "Watsonville" in head and "Laboratory ID No" in head


def watsonville_to_ceden(text: str) -> list[dict]:
    """City of Watsonville Public Utilities lab report (Pinto Lake HABID series): one sample per
    PDF — key/value header, a small results table (e.g. an Abraxis microcystin strip test), and a
    microscopy observation. Sampler names / contact details on the form are not carried over."""
    def field(rx):
        m = re.search(rx, text)
        return m.group(1).strip() if m else None

    lab_id = field(r"Laboratory ID No:\s*(\S+)")
    name = field(r"Sample Name:\s*(.+?)(?:\s{2,}|\n)")
    loc = field(r"\n\s*Location:\s*(.+?)(?:\s{2,}|\n)")
    habid = field(r"HABID:\s*(\d+)")
    stype = field(r"Sample Type:\s*(\S+)")
    m = re.search(r"Collection Date/Time:\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*(\d{1,2}:\d{2}\s*[AP]M)?", text)
    if not m:
        return []
    year = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
    date, time = _split_collected(f"{int(m.group(1))}/{int(m.group(2))}/{year} {m.group(4) or ''}")
    base = {"StationCode": clean(loc or name), "StationName": clean(name), "SampleDate": date,
            "SampleTime": time, "ProjectCode": f"HABID {habid}" if habid else None,
            "LabBatch": None, "BG_ID": f"WPUL-{lab_id}" if lab_id else None,
            "SampleTypeCode": clean(stype), "LabSampleID": clean(lab_id)}
    out: list[dict] = []
    lines = text.splitlines()
    hdr = next((i for i, l in enumerate(lines)
                if re.match(r"\s*Analyte\s{2,}Method\s{2,}", l) and "Result" in l), None)
    if hdr is not None:
        cols = [list(x) for x in _spans(lines[hdr])]
        labels = [c[2] for c in cols]
        for l in lines[hdr + 1:]:
            if not l.strip() or "Report Approved" in l:
                break
            rec = dict(zip(labels, _assign(_spans(l), cols)))
            raw = (rec.get("Analyte") or "").lstrip("*").strip()
            low = raw.lower()
            analyte = ("Microcystins" if "microcystin" in low else "Anatoxin-a" if "anatoxin" in low
                       else "Cylindrospermopsin" if "cylindro" in low
                       else "Saxitoxin" if "saxitoxin" in low else raw)
            method = "Strip test" if "strip" in (rec.get("Method") or "").lower() else rec.get("Method")
            parsed = _value(rec.get("Result"))
            if not analyte or parsed is None:
                continue
            result, rqc = parsed
            out.append({**base, "Analyte": analyte, "MethodName": method,
                        "AnalysisType": _analysis_type(method or "", analyte) or "Cyanotoxin",
                        "Result": result, "ResQualCode": rqc or (rec.get("Qual") or ""),
                        "Units": rec.get("Units"), "RL": _num(rec.get("PQL")),
                        "Fraction": "Total", "MatrixName": "samplewater"})
    mm = re.search(r"Microscopic\s+(.*?)\n\s*Results:", text, re.S)
    if mm:
        obs = re.sub(r"\s+", " ", mm.group(1).replace("Analysis:", " ")).strip()
        none_seen = bool(re.search(r"no cyanobacteria", obs, re.I))
        out.append({**base, "Analyte": "Cyanobacteria (microscopy observation)",
                    "MethodName": "Microscopy", "AnalysisType": "Microscopy", "Result": "",
                    "ResQualCode": "ND" if none_seen else "", "ResultText": None if none_seen else obs[:300],
                    "Taxa": None, "Units": None, "Fraction": None, "MatrixName": None})
    return out


def pdf_results(text: str, in_registry=lambda code: False) -> list[dict]:
    """CEDEN rows from a lab-report PDF's text: Bend's analytical report or a Watsonville report;
    [] for anything else (cover letters, invoices, scanned charts)."""
    if is_bend_page_report(text):
        return bend_report_to_ceden(bend_pages_to_csv(text), in_registry)
    if is_bend_report(text):
        return bend_report_to_ceden(bend_pdf_to_csv(text), in_registry)
    if is_watsonville_report(text):
        return watsonville_to_ceden(text)
    return []


def results_text(path: Path) -> str:
    """Text of a results file: CSV decoded (UTF-8, falling back to Windows-1252), or the first
    worksheet of an .xlsx rendered as CSV so both layouts share one parser."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            buf = io.StringIO()
            w = csv.writer(buf)
            for row in wb.worksheets[0].iter_rows(values_only=True):
                w.writerow(["" if v is None else _cell_text(v) for v in row])
            return buf.getvalue()
        finally:
            wb.close()
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def pdf_text(data) -> str:
    """Text of a PDF (bytes or path), one page after another, with table columns kept apart by
    runs of spaces (pypdf layout mode). Scanned PDFs with no text layer return ''."""
    from pypdf import PdfReader
    src = io.BytesIO(data) if isinstance(data, (bytes, bytearray, memoryview)) else str(data)
    out = []
    for page in PdfReader(src).pages:
        try:
            out.append(page.extract_text(extraction_mode="layout") or "")
        except Exception:  # noqa: BLE001 — one unreadable page must not sink the document
            out.append("")
    return "\n".join(out)


def _cell_text(v) -> str:
    """Render an xlsx cell like the CSV export would (dates as M/D/YYYY H:MM, no trailing .0)."""
    from datetime import datetime
    if isinstance(v, datetime):
        return f"{v.month}/{v.day}/{v.year} {v.hour}:{v.minute:02d}"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# --- folder ingest ----------------------------------------------------------------------

_CEDEN_COLS = ["StationCode", "StationName", "SampleDate", "SampleTime", "ProjectCode",
               "LabBatch", "BG_ID", "Analyte", "MethodName", "Result", "ResQualCode",
               "Units", "Fraction", "MatrixName", "SampleTypeCode", "LabSampleID",
               "RL", "QACode", "ResultText", "Taxa", "AnalysisType", "ResultKey",
               "TargetLatitude", "TargetLongitude"]

_REGION_RE = re.compile(r"\bRB\s*([1-9])\b", re.I)
_CATEGORY = [(re.compile(r"(results|report).*\.(csv|xlsx|xlsm)$", re.I), "data"),
             (re.compile(r"^coc", re.I), "coc"),
             (re.compile(r"testing_results.*\.pdf$", re.I), "transmittal"),
             (re.compile(r"receipt", re.I), "receipt"),
             (re.compile(r"-email\.txt$", re.I), "email"),
             (re.compile(r"\.(msg|eml)$", re.I), "email")]

_EMAIL_EXTS = {".eml", ".msg"}


def _categorize(name: str) -> str:
    for rx, cat in _CATEGORY:
        if rx.search(name):
            return cat
    return "other"


def region_from(text: str) -> str | None:
    m = _REGION_RE.search(text or "")
    return f"Region {m.group(1)}" if m else None


def _in_registry(conn, code: str | None) -> bool:
    if not code:
        return False
    return bool(conn.execute(
        "SELECT 1 FROM station_registry WHERE station_code=%s AND latitude IS NOT NULL",
        (code.strip(),)).fetchone())


def _write_unique(folder: Path, prefix: str, name: str, data) -> Path | None:
    """Write bytes into `folder` under a collision-safe basename. Returns the path (or None)."""
    base = Path((name or "").replace("\\", "/")).name.strip()
    if not base or data is None:
        return None
    dest = folder / base
    if dest.exists():
        dest = folder / f"{prefix}__{base}"
    dest.write_bytes(data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8"))
    return dest


def _extract_eml(path: Path):
    """Attachments + body text from a .eml (RFC 822) via the standard library.

    Skips inline images (signature logos) so only real attachments — the results spreadsheet,
    the CoC/transmittal/receipt PDFs — are unpacked. Returns (attachments, body, subject).
    """
    import email
    from email import policy
    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    atts = []
    for part in msg.iter_attachments():
        fn = part.get_filename()
        if not fn:
            continue
        ctype = (part.get_content_type() or "").lower()
        inline = part.get("Content-ID") or part.get_content_disposition() == "inline"
        if ctype.startswith("image/") and inline:
            continue
        data = part.get_payload(decode=True)
        if data:
            atts.append((fn, data))
    body = None
    try:
        b = msg.get_body(preferencelist=("plain", "html"))
        if b is not None:
            body = b.get_content()
    except Exception:  # noqa: BLE001
        pass
    return atts, body, (msg.get("Subject") or "").strip() or None


def _extract_msg(path: Path):
    """Attachments + body/subject from an Outlook .msg (OLE compound file) via olefile.

    Reads the MAPI attachment storages (`__attach_version1.0_*`): long/short filename streams
    (0x3707 / 0x3704) and the binary data stream (0x37010102). Embedded-message attachments are
    skipped. Returns (attachments, body, subject).
    """
    import olefile

    def _stream(ole, parts):
        for tag, enc in parts:
            if ole.exists(tag):
                raw = ole.openstream(tag).read()
                try:
                    return raw.decode(enc, "ignore").replace("\x00", "").strip() if enc else raw
                except Exception:  # noqa: BLE001
                    return raw
        return None

    atts, body, subject = [], None, None
    with olefile.OleFileIO(str(path)) as ole:
        entries = ole.listdir(streams=True, storages=True)
        storages = sorted({e[0] for e in entries if e and e[0].startswith("__attach_version1.0")})
        for d in storages:
            fn = _stream(ole, [([d, "__substg1.0_3707001F"], "utf-16-le"),
                               ([d, "__substg1.0_3707001E"], "latin-1"),
                               ([d, "__substg1.0_3704001F"], "utf-16-le"),
                               ([d, "__substg1.0_3704001E"], "latin-1")])
            data = _stream(ole, [([d, "__substg1.0_37010102"], None)])
            if fn and isinstance(data, (bytes, bytearray)):
                atts.append((fn, bytes(data)))
        body = _stream(ole, [(["__substg1.0_1000001F"], "utf-16-le"),
                             (["__substg1.0_1000001E"], "latin-1")])
        subject = _stream(ole, [(["__substg1.0_0037001F"], "utf-16-le"),
                                (["__substg1.0_0037001E"], "latin-1")])
    return atts, body, (subject or None)


def expand_email_files(folder) -> dict:
    """Unpack any .eml/.msg in `folder` into their attachments (results CSV, CoC/transmittal PDFs)
    so the normal folder ingest picks them up. The email body is saved as `<stem>-email.txt` and
    the original message is left in place — both stored on the batch for provenance. The first
    email's subject is returned as a suggested batch label. Best-effort: a message that can't be
    parsed is left as-is and simply attached whole."""
    folder = Path(folder)
    emails = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in _EMAIL_EXTS)
    extracted, subject = 0, None
    for p in emails:
        try:
            atts, body, subj = _extract_eml(p) if p.suffix.lower() == ".eml" else _extract_msg(p)
        except Exception:  # noqa: BLE001 — unparseable email: keep the raw file, skip extraction
            continue
        subject = subject or subj
        for name, data in atts:
            if _write_unique(folder, p.stem, name, data):
                extracted += 1
        if body:
            _write_unique(folder, p.stem, f"{p.stem}-email.txt", body)
    return {"emails": len(emails), "attachments": extracted, "subject": subject}


def attach_batch_file(conn, batch_id: int, path: Path, category: str) -> int:
    """Store one source file's bytes on the batch. Returns lab_batch_file.id."""
    data = Path(path).read_bytes()
    ctype = mimetypes.guess_type(str(path))[0]
    return conn.execute(
        """INSERT INTO lab_batch_file (batch_id, category, filename, content_type, byte_size, data)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
        (batch_id, category, Path(path).name, ctype, len(data), data)).fetchone()["id"]


def ingest_bend_folder(conn: psycopg.Connection, folder, *, source: str | None = None,
                       region: str | None = None, user_id: int | None = None,
                       session_id: str | None = None) -> dict:
    """Ingest one Bend/partner folder: convert + materialize chemistry, store the source files.

    `session_id` groups folders ingested together (a multi-folder upload passes one shared id);
    a single ingest mints its own. Returns a stats dict (incl. the session). Uses the owner
    connection (bypasses RLS) like the CEDEN batch loader.
    """
    session_id = session_id or uuid.uuid4().hex
    folder = Path(folder)
    # Unpack any raw lab emails (.eml/.msg) into their attachments first, so a staffer can drop the
    # whole email in and the results spreadsheet + CoC PDFs get ingested like any other folder.
    unpacked = expand_email_files(folder)
    source = source or unpacked.get("subject") or folder.name
    region = region or region_from(source)
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    sources = _results_sources(files)
    data_file = sources[0] if sources else None

    max_before = conn.execute("SELECT COALESCE(max(id),0) AS m FROM sample").fetchone()["m"]
    for src in sources:
        _load_results_file(conn, src)

    batch_id = conn.execute(
        """INSERT INTO lab_batch (filename, kind, source, region, status, uploaded_by, ingest_session)
           VALUES (%s,'ingested',%s,%s,'open',%s,%s) RETURNING id""",
        (data_file.name if data_file else None, source, region, user_id,
         session_id)).fetchone()["id"]
    counts = {"samples": 0, "geocoded": 0, "results": 0}
    if data_file is not None:
        conn.execute("UPDATE sample SET lab_batch_id=%s WHERE id > %s AND lab_batch_id IS NULL",
                     (batch_id, max_before))
        counts = _refresh_batch_counts(conn, batch_id)

    n_files = sum(1 for p in files if attach_batch_file(conn, batch_id, p, _categorize(p.name)))
    conn.commit()
    return {"batch_id": batch_id, "source": source, "region": region, "session": session_id,
            **counts, "files": n_files}


def _data_files(paths) -> list:
    """The results file(s) among a folder's files — CSV before XLSX (an xlsx is often a copy)."""
    data = [Path(p) for p in paths if _categorize(Path(p).name) == "data"]
    return sorted(data, key=lambda p: (p.suffix.lower() != ".csv", p.name.lower()))


# PDFs worth trying for results (lab reports/memos); CoCs, receipts, invoices and advisory signs aren't.
_RESULTS_PDF_RE = re.compile(r"result|report|HABID|memo", re.I)


def _results_sources(paths) -> list:
    """What to parse for results: the folder's results spreadsheet if it has one (its PDF is the
    same report), otherwise every lab-report PDF in it (a folder can hold several reports)."""
    data = _data_files(paths)
    if data:
        return data[:1]
    return sorted((Path(p) for p in paths if Path(p).suffix.lower() == ".pdf"
                   and _RESULTS_PDF_RE.search(Path(p).name)), key=lambda p: p.name.lower())


def _load_results_file(conn, path: Path) -> int:
    """Parse one results file — the flat Bend table or the sectioned lab report, as CSV or XLSX —
    and load it via the shared CEDEN loader. Returns result rows loaded. New samples are left
    unattached; the caller assigns them to a batch."""
    in_reg = lambda code: _in_registry(conn, code)  # noqa: E731
    if Path(path).suffix.lower() == ".pdf":
        ceden_rows = pdf_results(pdf_text(path), in_reg)
    elif is_bend_report(text := results_text(path)):
        ceden_rows = bend_report_to_ceden(text, in_reg)
    else:
        reader = csv.DictReader(io.StringIO(text))
        header = reader.fieldnames or []
        rows = list(reader)

        def pick(row):
            loc, cust = row.get("Location"), row.get("Customer Sample")
            if _in_registry(conn, loc):
                return loc, cust
            if _in_registry(conn, cust):
                return cust, loc
            return loc, cust  # ungeocoded; Location is the better human/station guess

        ceden_rows = bend_wide_to_ceden(rows, header, pick)
    if not ceden_rows:
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", delete=False) as tmp:
        w = csv.DictWriter(tmp, fieldnames=_CEDEN_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ceden_rows)
        tmp_path = tmp.name
    try:
        n = load_ceden_output(conn, None, Path(tmp_path), link=False).counts.get("results", 0)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    _apply_report_coords(conn, ceden_rows)
    return n


def _apply_report_coords(conn, ceden_rows) -> int:
    """Give a station the coordinates its lab report stated (lat/lon in the Location column) when
    it has none yet — a registry location, or one a staffer set, is never overwritten."""
    seen, n = set(), 0
    for r in ceden_rows:
        code, lat, lon = r.get("StationCode"), r.get("TargetLatitude"), r.get("TargetLongitude")
        if code and lat is not None and lon is not None and code not in seen:
            seen.add(code)
            n += conn.execute(
                "UPDATE station SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326) "
                "WHERE station_code = %s AND geom IS NULL", (float(lon), float(lat), code)).rowcount
    conn.commit()
    return n


def _refresh_batch_counts(conn, batch_id: int) -> dict:
    """Recompute a batch's sample / geocoded / result counts from what it actually holds."""
    agg = conn.execute(
        """SELECT count(*) AS n, count(*) FILTER (WHERE st.geom IS NOT NULL) AS g,
                  (SELECT count(*) FROM result r JOIN sample s2 ON s2.id = r.sample_id
                    WHERE s2.lab_batch_id = %(b)s) AS r
           FROM sample s LEFT JOIN station st ON st.id = s.station_id
           WHERE s.lab_batch_id = %(b)s""", {"b": batch_id}).fetchone()
    conn.execute("UPDATE lab_batch SET n_samples=%s, n_geocoded=%s, n_results=%s WHERE id=%s",
                 (agg["n"], agg["g"], agg["r"], batch_id))
    return {"samples": agg["n"], "geocoded": agg["g"], "results": agg["r"]}


def reparse_batches(conn, batch_ids, *, replace=False, user_id=None) -> dict:
    """Re-run results parsing on batches already in the database, from their stored files — for
    folders that came in before the parser understood their layout (they hold the files but 0
    samples). No re-upload needed; uploader, session and files are kept. A batch that already has
    samples is skipped, so re-running is safe — unless `replace`, which first removes the batch's
    previously parsed samples (audited as `user_id`), but only if no one has worked on them yet
    (linked, assigned, QA'd or tagged routine). Returns {parsed: [...], skipped: [...]}."""
    from .cleanup import purge_samples
    ids = [int(x) for x in batch_ids if str(x).isdigit()]
    parsed, skipped = [], []
    if replace:
        conn.execute("SELECT set_config('fhab.user_id', %s, false)", (str(user_id or ""),))
    for bid in ids:
        existing = [r["id"] for r in conn.execute(
            "SELECT id FROM sample WHERE lab_batch_id=%s", (bid,)).fetchall()]
        if existing and not replace:
            skipped.append({"id": bid, "reason": "already has samples"})
            continue
        if existing:
            worked = conn.execute(
                """SELECT count(*) AS c FROM sample WHERE id = ANY(%s)
                     AND (bloom_report_id IS NOT NULL OR case_id IS NOT NULL OR assigned_to IS NOT NULL
                          OR qa_status IS NOT NULL OR sampling_type = 'routine')""",
                (existing,)).fetchone()["c"]
            if worked:
                skipped.append({"id": bid, "reason": "has samples staff already worked on "
                                "(linked / assigned / QA'd) — not replaced"})
                continue
            purge_samples(conn, existing)
            conn.commit()
        stored = conn.execute("SELECT id, filename, data FROM lab_batch_file WHERE batch_id=%s",
                              (bid,)).fetchall()
        cands = {Path(f["filename"]).name: f for f in stored}
        sources = [cands[p.name] for p in _results_sources(cands)]
        if not sources:
            skipped.append({"id": bid, "reason": "no results spreadsheet or lab-report PDF on file"})
            continue
        label = sources[0]["filename"] + (f" (+{len(sources) - 1} more)" if len(sources) > 1 else "")
        max_before = conn.execute("SELECT COALESCE(max(id),0) AS m FROM sample").fetchone()["m"]
        loaded, failed = 0, None
        with tempfile.TemporaryDirectory() as tmpdir:
            for f in sources:
                path = Path(tmpdir) / Path(f["filename"]).name
                path.write_bytes(bytes(f["data"]))
                try:
                    loaded += _load_results_file(conn, path)
                except Exception as exc:  # noqa: BLE001 — one bad file must not stop the rest
                    conn.rollback()
                    failed = f"could not parse {f['filename']}: " + str(exc).splitlines()[0][:120]
                    break
        if failed:
            skipped.append({"id": bid, "reason": failed})
            continue
        conn.execute("UPDATE sample SET lab_batch_id=%s WHERE id > %s AND lab_batch_id IS NULL",
                     (bid, max_before))
        if Path(sources[0]["filename"]).suffix.lower() != ".pdf":
            conn.execute("UPDATE lab_batch_file SET category='data' WHERE id=%s", (sources[0]["id"],))
        conn.execute("UPDATE lab_batch SET filename=coalesce(filename, %s) WHERE id=%s",
                     (sources[0]["filename"], bid))
        counts = _refresh_batch_counts(conn, bid)
        conn.commit()
        if counts["samples"]:
            parsed.append({"id": bid, **counts})
        elif loaded:   # rows parsed, but every BG_ID already belongs to another folder
            skipped.append({"id": bid, "reason": "duplicate copy: its samples are already loaded "
                            f"from another folder ({label})"})
        else:
            skipped.append({"id": bid, "reason": f"no sample rows recognized in {label} "
                            "(scanned image or an unfamiliar layout)"})
    return {"parsed": parsed, "skipped": skipped}


def batch_files(conn, batch_id: int) -> list[dict]:
    """File metadata (no bytes) for a batch, for listing/download links."""
    return conn.execute(
        """SELECT id, category, filename, content_type, byte_size
           FROM lab_batch_file WHERE batch_id=%s ORDER BY category, filename""",
        (batch_id,)).fetchall()


def batch_file(conn, file_id: int) -> dict | None:
    """One file's bytes + metadata for download."""
    return conn.execute(
        "SELECT filename, content_type, data FROM lab_batch_file WHERE id=%s",
        (file_id,)).fetchone()


def ingested_batches(conn) -> list[dict]:
    """Ingested folder batches for the ingest listing, newest first."""
    return conn.execute(
        """SELECT b.id, b.source, b.region, b.filename, b.uploaded_at,
                  b.n_samples, b.n_geocoded, b.n_results,
                  (SELECT count(*) FROM lab_batch_file f WHERE f.batch_id=b.id) AS n_files
           FROM lab_batch b WHERE b.kind='ingested' ORDER BY b.uploaded_at DESC, b.id DESC"""
    ).fetchall()


def delete_batches(conn, user_id, batch_ids, *, force_linked=False) -> dict:
    """Delete whole ingestions — a folder/batch and everything it brought in (its samples, their
    results, and the stored source files). Pass several `batch_ids` to remove more than one folder
    (or an entire upload session) at once.

    Guard: a batch whose samples have been linked to a report/case is left untouched (and reported
    as skipped) unless `force_linked` — deleting it would drop lab data now attached to a case.
    Sample deletions are attributed to `user_id` in the audit log; a batch's files, staging rows,
    and station links fall away via ON DELETE CASCADE once the batch row is gone.
    """
    from .cleanup import purge_samples
    ids = [int(x) for x in batch_ids if str(x).isdigit()]
    if not ids:
        return {"deleted_batches": 0, "deleted_samples": 0, "skipped": []}
    conn.execute("SELECT set_config('fhab.user_id', %s, false)", (str(user_id or ""),))
    deleted_batches = deleted_samples = 0
    skipped = []
    for bid in ids:
        if not conn.execute("SELECT 1 FROM lab_batch WHERE id=%s", (bid,)).fetchone():
            skipped.append({"id": bid, "reason": "not found"})
            continue
        sample_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM sample WHERE lab_batch_id=%s", (bid,)).fetchall()]
        linked = conn.execute(
            "SELECT count(*) AS c FROM sample WHERE lab_batch_id=%s "
            "AND (bloom_report_id IS NOT NULL OR case_id IS NOT NULL)", (bid,)).fetchone()["c"]
        if linked and not force_linked:
            skipped.append({"id": bid, "reason": f"{linked} sample(s) linked to a report/case"})
            continue
        deleted_samples += purge_samples(conn, sample_ids)
        conn.execute("DELETE FROM lab_batch WHERE id=%s", (bid,))   # cascades files/staging/links
        deleted_batches += 1
    conn.commit()
    return {"deleted_batches": deleted_batches, "deleted_samples": deleted_samples, "skipped": skipped}


def ingestion_report(conn, *, kind=None, region=None, q=None, date_from=None, date_to=None) -> dict:
    """Filterable report over every ingestion (lab_batch): when it was ingested, its kind, source,
    region, sample / geocoded / result counts, files, status and who ran it, plus totals.

    'How many folders' = the number of batches (each email folder / prepared file is one batch).
    Geocoded % is the headline ingestion-success signal.
    """
    cond, p = ["TRUE"], {}
    if kind in ("ingested", "staged"):
        cond.append("b.kind = %(kind)s"); p["kind"] = kind
    if region:
        cond.append("b.region = %(region)s"); p["region"] = region
    if q:
        cond.append("(b.source ILIKE %(q)s OR b.filename ILIKE %(q)s OR CAST(b.id AS text) = %(qx)s)")
        p["q"] = f"%{q}%"; p["qx"] = q.strip()
    if date_from:
        cond.append("b.uploaded_at >= %(df)s"); p["df"] = date_from
    if date_to:
        cond.append("b.uploaded_at < (%(dt)s::date + 1)"); p["dt"] = date_to
    where = " AND ".join(cond)
    batches = conn.execute(
        f"""SELECT b.id, b.uploaded_at, b.kind, b.source, b.region, b.status, b.filename,
                   b.ingest_session, b.n_samples, b.n_geocoded, b.n_results, u.email AS uploaded_by,
                   (SELECT count(*) FROM lab_batch_file f WHERE f.batch_id = b.id) AS n_files,
                   sd.first_sample, sd.last_sample, sd.n_actual
            FROM lab_batch b
            LEFT JOIN app_user u ON u.id = b.uploaded_by
            LEFT JOIN LATERAL (
                SELECT min(s.sample_date) AS first_sample, max(s.sample_date) AS last_sample,
                       count(*) AS n_actual
                FROM sample s WHERE s.lab_batch_id = b.id) sd ON true
            WHERE {where} ORDER BY b.uploaded_at DESC, b.id DESC LIMIT 2000""", p).fetchall()
    tot = conn.execute(
        f"""SELECT count(*) AS batches, coalesce(sum(b.n_samples),0) AS samples,
                   coalesce(sum(b.n_geocoded),0) AS geocoded, coalesce(sum(b.n_results),0) AS results
            FROM lab_batch b WHERE {where}""", p).fetchone()
    totals = dict(tot)
    totals["geocoded_pct"] = round(100 * totals["geocoded"] / totals["samples"]) if totals["samples"] else None
    return {"batches": batches, "totals": totals, "sessions": sessionize(batches)}


def sessionize(batches: list) -> list[dict]:
    """Roll the report's batch rows up into upload sessions (batches ingested together share an
    ingest_session; a batch with none is its own session). Each session summarises who/when, how
    many folders, the sample/geocoded/result/file totals, and the overall lab-result date range,
    and keeps its batches for the expandable detail."""
    from collections import OrderedDict
    sess: "OrderedDict[str, dict]" = OrderedDict()
    for b in batches:
        key = b["ingest_session"] or f"b{b['id']}"
        s = sess.get(key)
        if s is None:
            s = sess[key] = {"session": key, "started": b["uploaded_at"], "finished": b["uploaded_at"],
                             "uploaded_by": b["uploaded_by"], "n_batches": 0, "samples": 0,
                             "geocoded": 0, "results": 0, "files": 0, "first_sample": None,
                             "last_sample": None, "batches": []}
        s["batches"].append(b)
        s["n_batches"] += 1
        s["samples"] += b["n_samples"] or 0
        s["geocoded"] += b["n_geocoded"] or 0
        s["results"] += b["n_results"] or 0
        s["files"] += b["n_files"] or 0
        s["uploaded_by"] = s["uploaded_by"] or b["uploaded_by"]
        if b["uploaded_at"]:
            s["started"] = min(s["started"] or b["uploaded_at"], b["uploaded_at"])
            s["finished"] = max(s["finished"] or b["uploaded_at"], b["uploaded_at"])
        for k in ("first_sample", "last_sample"):
            if b[k] is not None:
                if k == "first_sample":
                    s[k] = b[k] if s[k] is None else min(s[k], b[k])
                else:
                    s[k] = b[k] if s[k] is None else max(s[k], b[k])
    for s in sess.values():
        s["geocoded_pct"] = round(100 * s["geocoded"] / s["samples"]) if s["samples"] else None
    return list(sess.values())
