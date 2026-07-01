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
    assert b"Ingest batch" in wb.data
    fid = batch_files(conn, r["batch_id"])[0]["id"]
    dl = client.get(f"/batch/{r['batch_id']}/file/{fid}")
    assert dl.status_code == 200 and len(dl.data) > 0
