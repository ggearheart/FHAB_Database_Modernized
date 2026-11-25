"""Ingest of Bend/partner lab email folders: wide->CEDEN conversion + batch file storage."""

from pathlib import Path

import pytest

from fhab.bendlab import (batch_file, batch_files, bend_wide_to_ceden, ingest_bend_folder,
                          ingested_batches, parse_analyte_columns, region_from)

HEADER = ["Sample ID", "Batch", "Project", "Location", "Sample Type", "Collected", "Time",
          "Customer", "Customer Sample", "BG_ID",
          "Anatoxin-a (ug/L)", "Microcystin/Nod. (ug/L)", "qPCR-mcyE (copies/mL)",
          "Microcystin (toxins/g)", "Chloropyhll-a (ug/L)"]


def _row(**kw):
    r = {h: "" for h in HEADER}
    r.update(kw)
    return r


def test_parse_analyte_columns_methods_and_units():
    cols = parse_analyte_columns(HEADER)
    assert cols["Anatoxin-a (ug/L)"] == {
        "analyte": "Anatoxin-a", "method": "ELISA", "unit": "ug/L",
        "fraction": "Total", "matrix": "samplewater"}
    assert cols["Microcystin/Nod. (ug/L)"]["analyte"] == "Microcystins"
    assert cols["qPCR-mcyE (copies/mL)"] == {
        "analyte": "mcyE gene", "method": "qPCR", "unit": "copies/mL",
        "fraction": "Total", "matrix": "samplewater"}
    # dry-weight ("/g") -> tissue matrix + dry-weight fraction
    dry = cols["Microcystin (toxins/g)"]
    assert dry["fraction"] == "Dry Weight" and dry["matrix"] == "sampletissue"
    # Bend's "Chloropyhll" typo maps to the pigment analyte via Spectrophotometry
    assert cols["Chloropyhll-a (ug/L)"] == {
        "analyte": "Chlorophyll a", "method": "Spectrophotometry", "unit": "ug/L",
        "fraction": "Total", "matrix": "samplewater"}


def test_bend_wide_to_ceden_nd_and_values():
    rows = [_row(**{"Location": "630BPRD01", "Collected": "6/16/2025", "BG_ID": "WB5903",
                    "Anatoxin-a (ug/L)": "ND", "Microcystin/Nod. (ug/L)": "4.13",
                    "qPCR-mcyE (copies/mL)": "", "Chloropyhll-a (ug/L)": "12"})]
    out = bend_wide_to_ceden(rows, HEADER, lambda r: (r["Location"], None))
    by = {r["Analyte"]: r for r in out}
    # blank cell (qPCR) skipped; the other three analytes emitted
    assert set(by) == {"Anatoxin-a", "Microcystins", "Chlorophyll a"}
    assert by["Anatoxin-a"]["ResQualCode"] == "ND" and by["Anatoxin-a"]["Result"] == ""
    assert by["Microcystins"]["Result"] == "4.13" and by["Microcystins"]["ResQualCode"] == ""
    assert by["Anatoxin-a"]["StationCode"] == "630BPRD01"
    assert by["Anatoxin-a"]["SampleDate"] == "6/16/2025"


def test_region_from():
    assert region_from("Clear Lake (RB5)") == "Region 5"
    assert region_from("no region here") is None


def _write_folder(tmp_path, name, csv_rows, pdfs=("COC_x.pdf", "Cyanobacteria_testing_results.pdf")):
    import csv
    d = tmp_path / name
    d.mkdir()
    with (d / "20250101_results.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        w.writeheader()
        for r in csv_rows:
            w.writerow(r)
    for p in pdfs:
        (d / p).write_bytes(b"%PDF-1.4 fake")
    return d


def _bend_csv_bytes(rows):
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def test_ingest_eml_unpacks_attachments_and_ingests(conn, tmp_path):
    """A staffer drops the raw lab email (.eml): its results CSV + CoC PDF are unpacked and
    ingested, the body + original email are kept for provenance, and the subject labels the batch."""
    from email.message import EmailMessage
    conn.execute("INSERT INTO station_registry (station_code, latitude, longitude) "
                 "VALUES ('630BPRD01', 38.2, -119.2)")
    conn.commit()
    m = EmailMessage()
    m["Subject"] = "Bridgeport Reservoir (RB6) cyanotoxin results"
    m["From"] = "lab@example.org"; m["To"] = "staff@waterboards.ca.gov"
    m.set_content("Results + chain of custody attached.")
    m.add_attachment(_bend_csv_bytes([
        _row(**{"Location": "630BPRD01", "Collected": "6/16/2025", "BG_ID": "WB5903",
                "Microcystin/Nod. (ug/L)": "4.13"})]),
        maintype="text", subtype="csv", filename="results_bridgeport.csv")
    m.add_attachment(b"%PDF-1.4 fake coc", maintype="application", subtype="pdf",
                     filename="COC_bridgeport.pdf")
    m.add_attachment(b"\x89PNGlogo", maintype="image", subtype="png", filename="sig.png",
                     cid="<sig>", disposition="inline")           # inline logo -> skipped
    d = tmp_path / "eml_drop"; d.mkdir()
    (d / "lab.eml").write_bytes(bytes(m))

    r = ingest_bend_folder(conn, d)
    assert r["source"] == "Bridgeport Reservoir (RB6) cyanotoxin results"   # subject -> label
    assert r["region"] == "Region 6"                                         # parsed from subject
    assert r["samples"] == 1 and r["geocoded"] == 1 and r["results"] >= 1
    cats = {f["category"] for f in batch_files(conn, r["batch_id"])}
    assert "data" in cats and "coc" in cats and "email" in cats             # results, CoC, + email kept
    names = {f["filename"] for f in batch_files(conn, r["batch_id"])}
    assert "lab.eml" in names and "sig.png" not in names                    # original kept, logo skipped


def test_expand_email_ignores_unparseable_msg(tmp_path):
    """A .msg that isn't a valid OLE file is left in place (attached whole), not fatal."""
    from fhab.bendlab import expand_email_files
    d = tmp_path / "bad"; d.mkdir()
    (d / "note.msg").write_bytes(b"not really an outlook message")
    out = expand_email_files(d)
    assert out["emails"] == 1 and out["attachments"] == 0
    assert (d / "note.msg").exists()


def test_ingest_folder_materializes_and_stores_files(conn, tmp_path):
    # A station the registry knows -> the sample should geocode.
    conn.execute("INSERT INTO station_registry (station_code, latitude, longitude) "
                 "VALUES ('630BPRD01', 38.2, -119.2)")
    conn.commit()
    folder = _write_folder(tmp_path, "Bridgeport Reservoir (RB6)", [
        _row(**{"Location": "630BPRD01", "Collected": "6/16/2025", "BG_ID": "WB5903",
                "Anatoxin-a (ug/L)": "ND", "Microcystin/Nod. (ug/L)": "4.13"}),
        _row(**{"Location": "UNKNOWNXX", "Collected": "6/16/2025", "BG_ID": "WB5904",
                "Microcystin/Nod. (ug/L)": "ND"}),
    ])
    r = ingest_bend_folder(conn, folder)
    assert r["region"] == "Region 6"
    assert r["samples"] == 2 and r["geocoded"] == 1        # only the registry station geocodes
    assert r["results"] == 3 and r["files"] == 3           # csv + 2 pdfs

    # samples are materialized, unlinked, and tagged to the batch
    n = conn.execute("SELECT count(*) c FROM sample WHERE lab_batch_id=%s AND bloom_report_id IS NULL",
                     (r["batch_id"],)).fetchone()["c"]
    assert n == 2
    geo = conn.execute("SELECT count(*) c FROM sample s JOIN station st ON st.id=s.station_id "
                       "WHERE s.lab_batch_id=%s AND st.geom IS NOT NULL", (r["batch_id"],)).fetchone()["c"]
    assert geo == 1
    # files stored + retrievable
    files = batch_files(conn, r["batch_id"])
    assert {f["category"] for f in files} == {"data", "coc", "transmittal"}
    one = batch_file(conn, files[0]["id"])
    assert bytes(one["data"]).startswith(b"%PDF") or one["filename"].endswith(".csv")
    assert ingested_batches(conn)[0]["id"] == r["batch_id"]


def test_folder_without_csv_still_stores_files(conn, tmp_path):
    d = tmp_path / "Ewing Reservoir (RB1)"
    d.mkdir()
    (d / "COC_x.pdf").write_bytes(b"%PDF-1.4")
    (d / "Sample_receipt_form.pdf").write_bytes(b"%PDF-1.4")
    r = ingest_bend_folder(conn, d)
    assert r["samples"] == 0 and r["results"] == 0 and r["files"] == 2
    assert {f["category"] for f in batch_files(conn, r["batch_id"])} == {"coc", "receipt"}


# --- web ---

@pytest.fixture()
def client(conn):
    from fhab.auth import create_user, grant_role, set_password
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    staff = create_user(conn, "staff@wb.ca.gov")
    set_password(conn, staff, "pw"); grant_role(conn, staff, "wb_staff", region="Region 5")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_folder_ingest_page_and_download(client, conn, tmp_path):
    conn.execute("INSERT INTO station_registry (station_code, latitude, longitude) "
                 "VALUES ('630BPRD01', 38.2, -119.2)"); conn.commit()
    folder = _write_folder(tmp_path, "Clear Lake (RB5)", [
        _row(**{"Location": "630BPRD01", "Collected": "6/16/2025", "BG_ID": "WBX",
                "Microcystin/Nod. (ug/L)": "4.13"})])
    r = ingest_bend_folder(conn, folder)
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    page = client.get("/ingest/folders")
    assert page.status_code == 200 and b"Clear Lake (RB5)" in page.data
    # workboard scoped to the batch shows its file links
    wb = client.get(f"/lab/workboard?batch={r['batch_id']}")
    assert b"Sampling event" in wb.data
    fid = batch_files(conn, r["batch_id"])[0]["id"]
    dl = client.get(f"/batch/{r['batch_id']}/file/{fid}")
    assert dl.status_code == 200 and len(dl.data) > 0


def test_multifolder_ajax_upload_returns_json(client, conn):
    """The multi-folder uploader POSTs one subfolder at a time with ajax=1 and expects JSON."""
    import csv as _csv
    import io
    conn.execute("INSERT INTO station_registry (station_code, latitude, longitude) "
                 "VALUES ('630BPRD01', 38.2, -119.2)"); conn.commit()
    buf = io.StringIO(); w = _csv.DictWriter(buf, fieldnames=HEADER); w.writeheader()
    w.writerow(_row(**{"Location": "630BPRD01", "Collected": "6/16/2025", "BG_ID": "WBX",
                       "Microcystin/Nod. (ug/L)": "4.13"}))
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    data = {"ajax": "1", "source": "Clear Lake (RB5)",
            "files": [(io.BytesIO(buf.getvalue().encode()), "20250101_results.csv"),
                      (io.BytesIO(b"%PDF-1.4"), "COC_x.pdf")]}
    r = client.post("/ingest/folders", data=data, content_type="multipart/form-data")
    j = r.get_json()
    assert r.status_code == 200
    assert j["samples"] == 1 and j["files"] == 2 and j["region"] == "Region 5"
    # empty ajax post -> JSON error, not a redirect
    bad = client.post("/ingest/folders", data={"ajax": "1"}, content_type="multipart/form-data")
    assert bad.status_code == 400 and "error" in bad.get_json()


def test_ingestion_report_metadata_totals_and_filters(conn):
    """The report lists every batch with who/when, the lab-result date range, per-batch counts +
    totals, and filters by kind/region/search."""
    from fhab.auth import create_user
    from fhab.bendlab import ingestion_report
    uid = create_user(conn, "ingester@wb.ca.gov")
    conn.execute("""INSERT INTO lab_batch (id, kind, source, region, status, n_samples, n_geocoded, n_results, uploaded_by)
                    VALUES (9001,'ingested','Clear Lake (RB5)','Region 5','open',10,8,30,%s),
                           (9002,'ingested','Bridgeport (RB6)','Region 6','open',4,4,12,NULL),
                           (9003,'staged','A CEDEN file',NULL,'open',5,0,5,NULL)""", (uid,))
    # two samples in the Clear Lake batch, spanning a date range
    for d in ("2025-06-01", "2025-06-20"):
        conn.execute("INSERT INTO sample (lab_batch_id, sample_date) VALUES (9001, %s)", (d,))
    conn.commit()

    rep = ingestion_report(conn)
    assert rep["totals"]["batches"] == 3
    assert rep["totals"]["samples"] == 19 and rep["totals"]["geocoded"] == 12
    assert rep["totals"]["geocoded_pct"] == round(100 * 12 / 19)
    cl = next(b for b in rep["batches"] if b["id"] == 9001)
    assert str(cl["uploaded_by"]) == "ingester@wb.ca.gov"        # who ran the ingestion
    assert cl["uploaded_at"] is not None                        # when
    assert str(cl["first_sample"]) == "2025-06-01" and str(cl["last_sample"]) == "2025-06-20"  # result date range
    assert cl["n_actual"] == 2                                  # samples actually materialized
    # filters
    assert ingestion_report(conn, kind="ingested")["totals"]["batches"] == 2
    assert ingestion_report(conn, region="Region 6")["totals"]["samples"] == 4
    assert ingestion_report(conn, q="clear")["batches"][0]["source"] == "Clear Lake (RB5)"


def test_ingestion_sessions_group_multi_folder_uploads(conn):
    """Batches sharing an ingest_session roll up into one session; batches without one stand alone."""
    from fhab.bendlab import ingestion_report
    conn.execute("""INSERT INTO lab_batch (id, kind, source, status, n_samples, n_geocoded, n_results, ingest_session)
                    VALUES (8001,'ingested','Folder A','open',3,3,9,'sess-xyz'),
                           (8002,'ingested','Folder B','open',2,1,6,'sess-xyz'),
                           (8003,'ingested','Lone folder','open',4,4,4,NULL)""")
    conn.commit()
    sessions = ingestion_report(conn)["sessions"]
    by = {s["session"]: s for s in sessions}
    assert by["sess-xyz"]["n_batches"] == 2                       # the two folders grouped
    assert by["sess-xyz"]["samples"] == 5 and by["sess-xyz"]["geocoded"] == 4
    assert by["sess-xyz"]["geocoded_pct"] == 80
    assert {b["id"] for b in by["sess-xyz"]["batches"]} == {8001, 8002}
    assert by["b8003"]["n_batches"] == 1                          # ungrouped batch = its own session


def test_ingest_folder_records_the_user(conn, tmp_path):
    """The ingesting user is recorded on the batch so the report can show who did it."""
    from fhab.auth import create_user
    uid = create_user(conn, "folder@wb.ca.gov")
    d = tmp_path / "Ewing Reservoir (RB1)"; d.mkdir()
    (d / "COC_x.pdf").write_bytes(b"%PDF-1.4")
    r = ingest_bend_folder(conn, d, user_id=uid)
    who = conn.execute("SELECT uploaded_by FROM lab_batch WHERE id=%s", (r["batch_id"],)).fetchone()["uploaded_by"]
    assert who == uid


def _batch_with_samples(conn, bid, *, n=2, session=None, linked=False):
    """A lab_batch with n samples (each carrying one result), optionally linked to a report."""
    conn.execute("INSERT INTO lab_batch (id, kind, source, status, ingest_session) "
                 "VALUES (%s,'ingested',%s,'open',%s)", (bid, f"Folder {bid}", session))
    conn.execute("INSERT INTO lab_batch_file (batch_id, filename, data) VALUES (%s,'coc.pdf',%s)",
                 (bid, b"%PDF-1.4"))
    sids = []
    for i in range(n):
        sid = conn.execute("INSERT INTO sample (lab_batch_id, sample_date) VALUES (%s,'2025-06-01') "
                           "RETURNING id", (bid,)).fetchone()["id"]
        conn.execute("INSERT INTO result (result_id_unique, sample_id) VALUES (%s,%s)",
                     (f"{bid}-{i}", sid))
        sids.append(sid)
    if linked:
        conn.execute("INSERT INTO event (bloom_report_id) VALUES (%s)", (bid,))
        conn.execute("UPDATE sample SET bloom_report_id=%s WHERE id=%s", (bid, sids[0]))
    conn.commit()
    return sids


def test_delete_batches_removes_folder_and_everything_in_it(conn):
    from fhab.bendlab import delete_batches
    sids = _batch_with_samples(conn, 7101, n=3)
    res = delete_batches(conn, user_id=1, batch_ids=[7101])
    assert res["deleted_batches"] == 1 and res["deleted_samples"] == 3 and res["skipped"] == []
    assert conn.execute("SELECT count(*) c FROM lab_batch WHERE id=7101").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM lab_batch_file WHERE batch_id=7101").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM sample WHERE id=ANY(%s)", (sids,)).fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) c FROM result WHERE sample_id=ANY(%s)", (sids,)).fetchone()["c"] == 0


def test_delete_batches_multi_folder_session_at_once(conn):
    from fhab.bendlab import delete_batches
    _batch_with_samples(conn, 7201, n=2, session="sess-A")
    _batch_with_samples(conn, 7202, n=1, session="sess-A")
    res = delete_batches(conn, user_id=1, batch_ids=[7201, 7202])   # a whole upload session
    assert res["deleted_batches"] == 2 and res["deleted_samples"] == 3
    assert conn.execute("SELECT count(*) c FROM lab_batch WHERE id=ANY(ARRAY[7201,7202])").fetchone()["c"] == 0


def test_delete_batches_guards_linked_unless_forced(conn):
    from fhab.bendlab import delete_batches
    _batch_with_samples(conn, 7301, n=2, linked=True)     # one sample linked to a report
    res = delete_batches(conn, user_id=1, batch_ids=[7301])
    assert res["deleted_batches"] == 0 and len(res["skipped"]) == 1
    assert res["skipped"][0]["id"] == 7301 and "linked" in res["skipped"][0]["reason"]
    assert conn.execute("SELECT count(*) c FROM lab_batch WHERE id=7301").fetchone()["c"] == 1  # kept
    # forcing deletes it and its samples
    res2 = delete_batches(conn, user_id=1, batch_ids=[7301], force_linked=True)
    assert res2["deleted_batches"] == 1 and res2["deleted_samples"] == 2
    assert conn.execute("SELECT count(*) c FROM lab_batch WHERE id=7301").fetchone()["c"] == 0


def test_delete_batches_is_audited(conn):
    from fhab.auth import create_user
    from fhab.bendlab import delete_batches
    uid = create_user(conn, "deleter@wb.ca.gov")
    sids = _batch_with_samples(conn, 7401, n=1)
    delete_batches(conn, user_id=uid, batch_ids=[7401])
    a = conn.execute("SELECT actor_id, action FROM audit_log WHERE table_name='sample' "
                     "AND row_key=%s", (str(sids[0]),)).fetchone()
    assert a and a["action"] == "DELETE" and a["actor_id"] == uid


# --- older (2022–2023) Bend "analytical report" layout ------------------------------------

REPORT_FIXTURE = Path(__file__).parent / "fixtures" / "bend" / "report_2023_sample.csv"


def test_bend_report_parses_sections():
    """The sectioned report layout yields long CEDEN rows: station picked from Sample ID or
    Location, date/time split, QL→RL, ND/DNQ, dry weight, qPCR gene targets, microscopy taxa
    (incl. a 'none observed' ND), and the QC block ignored."""
    from fhab.bendlab import bend_report_to_ceden, is_bend_report
    text = REPORT_FIXTURE.read_text(encoding="utf-8")
    assert is_bend_report(text)
    rows = bend_report_to_ceden(text)
    by = {(r["LabSampleID"], r["Analyte"]): r for r in rows}
    assert len(rows) == 9                                  # 6 results + 3 microscopy, no QC rows
    assert not any("Blank" in r["Analyte"] or "Positive" in r["Analyte"] for r in rows)

    s1 = by[("1", "mcyE gene")]                            # QPCR target -> gene marker
    assert (s1["StationCode"], s1["StationName"]) == ("999TSTA01", None)   # code in Location
    assert (s1["SampleDate"], s1["SampleTime"], s1["BG_ID"]) == ("5/4/2023", "11:00", "TB0001")
    assert (s1["ResQualCode"], s1["Result"], s1["RL"], s1["MethodName"]) == ("ND", "", "100", "qPCR")
    assert by[("1", "sxtA gene")]["Result"] == "46075"     # thousands separator stripped
    assert s1["ProjectCode"] == "TEST_IncResp_2023"

    mat = by[("999TSTB02", "Microcystins")]                # code in Sample ID, name in Location
    assert (mat["StationCode"], mat["StationName"]) == ("999TSTB02", "Test Lake")
    assert mat["SampleTime"] is None                       # 0:00 = not recorded
    assert (mat["Fraction"], mat["MatrixName"]) == ("Dry Weight", "sampletissue")
    assert by[("999TSTB02", "Anatoxin-a")]["ResQualCode"] == "DNQ"   # C1 = below quantitation

    s2 = by[("2", "Cylindrospermopsin")]                   # neither looks like a code: place name
    assert (s2["StationCode"], s2["SampleTime"]) == ("Test Pond", "13:30")

    assert by[("1", "Cyanobacteria taxa (dominant)")]["Taxa"] == "Nostoc"
    assert by[("1", "Cyanobacteria taxa (sub-dominant)")]["ResultText"] == "Planktothrix"
    none_seen = by[("2", "Cyanobacteria taxa (dominant)")]
    assert (none_seen["ResQualCode"], none_seen["Taxa"]) == ("ND", None)


def test_bend_report_station_prefers_registry():
    """A code the CEDEN registry knows wins over the look-alike heuristic."""
    from fhab.bendlab import _report_station
    known = {"Test Lake"}.__contains__
    assert _report_station("999TSTB02", "Test Lake", known) == ("Test Lake", "999TSTB02")
    assert _report_station("1", "Test Lake", known) == ("Test Lake", None)   # running number dropped
    assert _report_station("999TSTB02", "", {"999TSTB02"}.__contains__) == ("999TSTB02", None)


def test_ingest_folder_with_report_layout(conn, tmp_path):
    """A folder whose results CSV is the report layout materializes samples + results (it used to
    store the files but load 0 samples), with microscopy typed and taxa kept."""
    d = tmp_path / "Testing results - Test Lake (RB5)"; d.mkdir()
    (d / "20230511_TEST_R5_Results.csv").write_bytes(REPORT_FIXTURE.read_bytes())
    (d / "COC_20230504.pdf").write_bytes(b"%PDF-1.4")
    r = ingest_bend_folder(conn, d)
    assert (r["samples"], r["results"], r["files"]) == (3, 9, 2)
    micro = conn.execute(
        """SELECT a.analysis_type, r.taxa, r.measurement_text FROM result r
           JOIN analyte a ON a.id = r.analyte_id JOIN sample s ON s.id = r.sample_id
           WHERE s.lab_batch_id=%s AND r.taxa='Nostoc'""", (r["batch_id"],)).fetchone()
    assert micro["analysis_type"] == "Microscopy" and micro["measurement_text"] == "Nostoc"
    b = conn.execute("SELECT n_samples, n_results FROM lab_batch WHERE id=%s", (r["batch_id"],)).fetchone()
    assert (b["n_samples"], b["n_results"]) == (3, 9)


def test_report_layout_from_xlsx(tmp_path):
    """The same report saved as .xlsx parses identically to the CSV."""
    import csv as _csv
    import openpyxl
    from fhab.bendlab import bend_report_to_ceden, results_text
    wb = openpyxl.Workbook(); ws = wb.active
    for row in _csv.reader(REPORT_FIXTURE.read_text(encoding="utf-8").splitlines()):
        ws.append(row)
    x = tmp_path / "Cyanotoxin Final Report.xlsx"; wb.save(x)
    from_csv = bend_report_to_ceden(REPORT_FIXTURE.read_text(encoding="utf-8"))
    assert bend_report_to_ceden(results_text(x)) == from_csv


def test_reparse_stored_batches(conn):
    """Batches that hold the report file but 0 samples (an earlier ingest) are parsed from their
    stored bytes; re-running is a no-op; a PDF-only batch is reported, not touched."""
    from fhab.bendlab import reparse_batches
    data = REPORT_FIXTURE.read_bytes()
    conn.execute("INSERT INTO lab_batch (id, kind, source, status, ingest_session) VALUES "
                 "(9101,'ingested','Old folder','open','sess-old'), (9102,'ingested','PDF only','open','sess-old')")
    from tests.pdfmini import mini_pdf
    scanned = mini_pdf([[]])                               # a PDF with no text layer
    conn.execute("INSERT INTO lab_batch_file (batch_id, category, filename, data) VALUES "
                 "(9101,'other','20230511_TEST_R5_Results.csv',%s), (9102,'transmittal','Cyanotoxin Chart results 2023.pdf',%s)",
                 (data, scanned))
    conn.commit()
    res = reparse_batches(conn, [9101, 9102])
    assert [p["id"] for p in res["parsed"]] == [9101] and res["parsed"][0]["samples"] == 3
    assert res["skipped"][0]["id"] == 9102 and "scanned image" in res["skipped"][0]["reason"]
    b = conn.execute("SELECT n_samples, n_results, filename, ingest_session FROM lab_batch WHERE id=9101").fetchone()
    assert (b["n_samples"], b["n_results"], b["ingest_session"]) == (3, 9, "sess-old")
    assert conn.execute("SELECT category FROM lab_batch_file WHERE batch_id=9101").fetchone()["category"] == "data"
    again = reparse_batches(conn, [9101])                  # idempotent
    assert again["parsed"] == [] and again["skipped"][0]["reason"] == "already has samples"
    assert conn.execute("SELECT count(*) c FROM sample WHERE lab_batch_id=9101").fetchone()["c"] == 3
    # a forwarded copy of the same report (same BG_IDs) is named as a duplicate, not re-loaded
    conn.execute("INSERT INTO lab_batch (id, kind, source, status) VALUES (9103,'ingested','Copy (2)','open')")
    conn.execute("INSERT INTO lab_batch_file (batch_id, category, filename, data) "
                 "VALUES (9103,'data','20230511_TEST_R5_Results.csv',%s)", (data,))
    conn.commit()
    dup = reparse_batches(conn, [9103])
    assert dup["parsed"] == [] and dup["skipped"][0]["reason"].startswith("duplicate copy")
    assert conn.execute("SELECT count(*) c FROM sample").fetchone()["c"] == 3


def test_reparse_route(conn):
    from fhab.auth import create_user, grant_role, set_password
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    conn.execute("INSERT INTO lab_batch (id, kind, source, status) VALUES (9201,'ingested','Old','open')")
    conn.execute("INSERT INTO lab_batch_file (batch_id, category, filename, data) "
                 "VALUES (9201,'data','x_Results.csv',%s)", (REPORT_FIXTURE.read_bytes(),))
    u = create_user(conn, "rp@wb.ca.gov"); set_password(conn, u, "pw"); grant_role(conn, u, "wb_staff")
    conn.commit()
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    cl = app.test_client(); cl.post("/login", data={"email": "rp@wb.ca.gov", "password": "pw"})
    assert "Re-parse stored files" in cl.get("/ingest/report").get_data(as_text=True)
    html = cl.post("/ingest/reparse", data={"batch_ids": ["9201"]}, follow_redirects=True).get_data(as_text=True)
    assert "Re-parsed 1 ingestion" in html
    assert conn.execute("SELECT n_samples FROM lab_batch WHERE id=9201").fetchone()["n_samples"] == 3


def test_pdf_text_and_route(conn):
    from fhab.auth import create_user, grant_role, set_password
    from fhab.bendlab import pdf_text
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    from tests.pdfmini import mini_pdf
    pdf = mini_pdf([[[(40, "SAMPLE RESULTS")], [(40, "1"), (90, "ELISA"), (160, "Saxitoxin"), (300, "ND")]]])
    text = pdf_text(pdf)
    assert "SAMPLE RESULTS" in text and "Saxitoxin" in text
    conn.execute("INSERT INTO lab_batch (id, kind, status) VALUES (9301,'ingested','open')")
    fid = conn.execute("INSERT INTO lab_batch_file (batch_id, category, filename, data) "
                       "VALUES (9301,'transmittal','x_testing_results.pdf',%s) RETURNING id", (pdf,)).fetchone()["id"]
    u = create_user(conn, "pt@wb.ca.gov"); set_password(conn, u, "pw"); grant_role(conn, u, "wb_staff")
    conn.commit()
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    cl = app.test_client(); cl.post("/login", data={"email": "pt@wb.ca.gov", "password": "pw"})
    r = cl.get(f"/batch/9301/file/{fid}.txt")
    assert r.status_code == 200 and "Saxitoxin" in r.get_data(as_text=True)


# --- PDF lab reports ------------------------------------------------------------------------

def _report_pdf():
    from tests.fixtures.bend.make_report_pdf import build
    return build()


def test_bend_pdf_parses_like_the_csv():
    """A Bend PDF report: cells placed by column position (empty Location, wrapped
    'Quantitation / Limit' header, wrapped notes), lat/lon in Location kept as coordinates,
    nutrients/pigments typed, QC and photo captions ignored."""
    from fhab.bendlab import pdf_results, pdf_text
    rows = pdf_results(pdf_text(_report_pdf()))
    by = {(r["LabSampleID"], r.get("ResultKey") or r["Analyte"]): r for r in rows}
    assert len(rows) == 11                                # 4 results + 3 taxa + 2×2 enumeration
    assert {r["LabSampleID"] for r in rows} == {"999TSTC01", "Test Park"}   # caption not a sample
    mc = by[("999TSTC01", "Microcystins")]
    assert (mc["StationCode"], mc["Result"], mc["RL"], mc["Units"]) == ("999TSTC01", "0.24", "0.15", "ug/L")
    assert (mc["SampleDate"], mc["SampleTime"], mc["BG_ID"]) == ("6/20/2022", "10:58", "TP0001")
    tn = by[("Test Park", "Total Nitrogen")]
    assert (tn["AnalysisType"], tn["ResQualCode"], tn["QACode"]) == ("Nutrient", "DNQ", "C1,J")
    assert (tn["StationCode"], tn["TargetLatitude"], tn["TargetLongitude"]) == ("Test Park", 38.32465, -122.34848)
    assert by[("Test Park", "Chlorophyll a")]["AnalysisType"] == "Pigment"
    assert by[("999TSTC01", "anaC gene")]["ResQualCode"] == "ND"
    assert by[("999TSTC01", "Cyanobacteria taxa (also present)")]["Taxa"] == "Dolichospermum"
    assert by[("999TSTC01", "Cyanobacteria taxa (dominant)")]["ResQualCode"] == ""   # not a false ND
    assert by[("Test Park", "Cyanobacteria taxa (dominant)")]["ResQualCode"] == "ND"
    dens = by[("999TSTC01", "Cyanobacteria cell density:Microcystis")]
    assert (dens["Result"], dens["Units"], dens["Taxa"]) == ("12183", "cells/mL", "Microcystis")
    assert by[("999TSTC01", "Cyanobacteria total biovolume:Dolichospermum")]["Result"] == "41600.0"


def test_watsonville_pdf_parses():
    from fhab.bendlab import is_watsonville_report, pdf_results, pdf_text
    from tests.fixtures.bend.make_report_pdf import build_watsonville
    text = pdf_text(build_watsonville())
    assert is_watsonville_report(text)
    rows = pdf_results(text)
    mc = next(r for r in rows if r["Analyte"] == "Microcystins")
    assert (mc["Result"], mc["ResQualCode"], mc["Units"], mc["MethodName"]) == ("1", "<", "ppb", "Strip test")
    assert (mc["SampleDate"], mc["SampleTime"], mc["BG_ID"]) == ("3/21/2023", "10:45", "WPUL-230321-0001")
    assert (mc["StationCode"], mc["ProjectCode"]) == ("Boat Launch TL1", "HABID 9999")
    micro = next(r for r in rows if r["MethodName"] == "Microscopy")
    assert micro["ResQualCode"] == "ND"
    assert not any("Sampler" in str(v) or "example.org" in str(v) for r in rows for v in r.values())


def test_ingest_pdf_only_folder_geocodes_from_report(conn, tmp_path):
    """A folder with only PDFs loads its results from the report PDF (CoC ignored); a station
    whose report gave lat/lon is geocoded from it."""
    d = tmp_path / "Cyanobacteria testing results - Test Park (RB2)"; d.mkdir()
    (d / "Cyanobacteria_testing_results_20220624.pdf").write_bytes(_report_pdf())
    (d / "COC_20220620.pdf").write_bytes(b"%PDF-1.4 not a report")
    r = ingest_bend_folder(conn, d)
    assert (r["samples"], r["results"], r["geocoded"]) == (2, 11, 1)
    g = conn.execute("SELECT ST_Y(geom) lat FROM station WHERE station_code='Test Park'").fetchone()
    assert abs(g["lat"] - 38.32465) < 1e-6


def test_reparse_replace_only_untouched(conn):
    """replace=True re-builds a batch's parsed samples (e.g. after a parser fix) — but leaves a
    batch alone once staff have worked on any of its samples."""
    from fhab.bendlab import reparse_batches
    data = REPORT_FIXTURE.read_bytes()
    for bid in (9401, 9402):
        conn.execute("INSERT INTO lab_batch (id, kind, source, status) VALUES (%s,'ingested','F','open')", (bid,))
    conn.execute("INSERT INTO lab_batch_file (batch_id, category, filename, data) "
                 "VALUES (9401,'data','a_Results.csv',%s)", (data,))
    conn.commit()
    assert reparse_batches(conn, [9401])["parsed"][0]["samples"] == 3
    old_ids = {r["id"] for r in conn.execute("SELECT id FROM sample WHERE lab_batch_id=9401").fetchall()}
    res = reparse_batches(conn, [9401], replace=True, user_id=None)
    assert res["parsed"][0]["samples"] == 3
    new_ids = {r["id"] for r in conn.execute("SELECT id FROM sample WHERE lab_batch_id=9401").fetchall()}
    assert new_ids and not (new_ids & old_ids)                 # rebuilt, not duplicated
    assert conn.execute("SELECT count(*) c FROM sample").fetchone()["c"] == 3
    conn.execute("UPDATE sample SET qa_status='approved' WHERE id = %s", (min(new_ids),)); conn.commit()
    kept = reparse_batches(conn, [9401], replace=True)
    assert kept["parsed"] == [] and "worked on" in kept["skipped"][0]["reason"]
    assert {r["id"] for r in conn.execute("SELECT id FROM sample WHERE lab_batch_id=9401").fetchall()} == new_ids
