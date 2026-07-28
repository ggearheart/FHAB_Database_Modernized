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
import mimetypes
import re
import tempfile
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


# --- folder ingest ----------------------------------------------------------------------

_CEDEN_COLS = ["StationCode", "StationName", "SampleDate", "SampleTime", "ProjectCode",
               "LabBatch", "BG_ID", "Analyte", "MethodName", "Result", "ResQualCode",
               "Units", "Fraction", "MatrixName", "SampleTypeCode", "LabSampleID"]

_REGION_RE = re.compile(r"\bRB\s*([1-9])\b", re.I)
_CATEGORY = [(re.compile(r"results.*\.csv$", re.I), "data"),
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
                       region: str | None = None) -> dict:
    """Ingest one Bend/partner folder: convert + materialize chemistry, store the source files.

    Returns a stats dict. Uses the owner connection (bypasses RLS) like the CEDEN batch loader.
    """
    folder = Path(folder)
    # Unpack any raw lab emails (.eml/.msg) into their attachments first, so a staffer can drop the
    # whole email in and the results spreadsheet + CoC PDFs get ingested like any other folder.
    unpacked = expand_email_files(folder)
    source = source or unpacked.get("subject") or folder.name
    region = region or region_from(source)
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))
    data_csv = next((p for p in files if _categorize(p.name) == "data"), None)

    n_samples = n_geocoded = n_results = 0
    if data_csv is not None:
        with data_csv.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
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
        max_before = conn.execute("SELECT COALESCE(max(id),0) AS m FROM sample").fetchone()["m"]
        with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", delete=False) as tmp:
            w = csv.DictWriter(tmp, fieldnames=_CEDEN_COLS)
            w.writeheader()
            w.writerows(ceden_rows)
            tmp_path = tmp.name
        try:
            rep = load_ceden_output(conn, None, Path(tmp_path), link=False).counts
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        n_results = rep.get("results", 0)

    batch_id = conn.execute(
        """INSERT INTO lab_batch (filename, kind, source, region, status, n_results)
           VALUES (%s,'ingested',%s,%s,'open',%s) RETURNING id""",
        (data_csv.name if data_csv else None, source, region, n_results)).fetchone()["id"]

    if data_csv is not None:
        conn.execute("UPDATE sample SET lab_batch_id=%s WHERE id > %s AND lab_batch_id IS NULL",
                     (batch_id, max_before))
        agg = conn.execute(
            """SELECT count(*) AS n,
                      count(*) FILTER (WHERE st.geom IS NOT NULL) AS g
               FROM sample s LEFT JOIN station st ON st.id = s.station_id
               WHERE s.lab_batch_id = %s""", (batch_id,)).fetchone()
        n_samples, n_geocoded = agg["n"], agg["g"]
        conn.execute("UPDATE lab_batch SET n_samples=%s, n_geocoded=%s WHERE id=%s",
                     (n_samples, n_geocoded, batch_id))

    n_files = sum(1 for p in files if attach_batch_file(conn, batch_id, p, _categorize(p.name)))
    conn.commit()
    return {"batch_id": batch_id, "source": source, "region": region,
            "samples": n_samples, "geocoded": n_geocoded, "results": n_results, "files": n_files}


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
