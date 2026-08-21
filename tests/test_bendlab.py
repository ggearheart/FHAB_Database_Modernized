"""Ingest of Bend/partner lab email folders: wide->CEDEN conversion + batch file storage."""

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
