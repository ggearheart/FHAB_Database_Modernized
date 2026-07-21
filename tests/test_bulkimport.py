"""Consolidated CEDEN-long CSV import: batches per sampling event, geocode, routine tagging."""

import pytest

from fhab.bulkimport import import_consolidated


def _row(**kw):
    base = {"SamplingEvent": "Clear Lake (RB5)", "Region": "Region 5", "StationCode": "IMP1",
            "StationName": "Imp Lake", "SampleDate": "6/2/2025", "SampleTime": "9:15",
            "ProjectCode": "P", "LabBatch": "1", "BG_ID": "IMPA", "LabSampleID": "L1",
            "SampleType": "Water Grab", "Analyte": "Microcystins", "MethodName": "ELISA",
            "Result": "4.13", "ResQualCode": "", "Units": "ug/L", "Fraction": "Total",
            "MatrixName": "samplewater", "Latitude": "38.5", "Longitude": "-121.4", "SamplingType": ""}
    base.update(kw)
    return base


def test_import_consolidated(conn):
    rows = [
        _row(),                                          # sample IMPA, analyte 1, geocoded
        _row(Analyte="Anatoxin-a", Result="", ResQualCode="ND"),   # same sample, analyte 2 (non-detect)
        _row(BG_ID="IMPB", LabSampleID="L2", StationCode="SPATT1", Latitude="38.6", Longitude="-121.5",
             SampleType="PassiveSampler SPATT Bank", SamplingType="routine", Analyte="Microcystins",
             Result="ND", ResQualCode="ND"),            # a routine SPATT sample
    ]
    s = import_consolidated(conn, rows)
    assert s["events"] == 1 and s["samples"] == 2 and s["results"] == 3
    assert s["geocoded"] == 2 and s["routine"] == 1

    # station geocoded from the file's lat/long
    assert conn.execute("SELECT geom IS NOT NULL AS g FROM station WHERE station_code='IMP1'").fetchone()["g"]
    # routine sample tagged; the other unlinked (for reconcile)
    assert conn.execute("SELECT sampling_type FROM sample WHERE bg_id='IMPB'").fetchone()["sampling_type"] == "routine"
    assert conn.execute("SELECT sampling_type FROM sample WHERE bg_id='IMPA'").fetchone()["sampling_type"] is None
    # one sampling-event batch, ND stored as a non-detect
    b = conn.execute("SELECT n_samples, n_geocoded FROM lab_batch WHERE kind='ingested' AND source='Clear Lake (RB5)'").fetchone()
    assert b["n_samples"] == 2 and b["n_geocoded"] == 2
    nd = conn.execute("SELECT res_qual_code FROM result r JOIN sample s ON s.id=r.sample_id "
                      "WHERE s.bg_id='IMPA' AND r.res_qual_code='ND'").fetchone()
    assert nd is not None


def test_import_idempotent_on_bg_id(conn):
    rows = [_row(BG_ID="IDEM", StationCode="IDS")]
    import_consolidated(conn, rows)
    import_consolidated(conn, rows)                      # re-import upserts, no duplicate sample
    n = conn.execute("SELECT count(*) AS c FROM sample WHERE bg_id='IDEM'").fetchone()["c"]
    assert n == 1


@pytest.fixture()
def client(conn):
    from fhab.auth import create_user, grant_role, set_password
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    staff = create_user(conn, "staff@wb.ca.gov"); set_password(conn, staff, "pw")
    grant_role(conn, staff, "wb_staff", region="Region 5")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_import_via_web(client, conn):
    import io
    header = ",".join(_row().keys())
    line = ",".join(str(v) for v in _row(BG_ID="WEBIMP", StationCode="WEBST").values())
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    data = {"file": (io.BytesIO((header + "\n" + line + "\n").encode()), "prep.csv")}
    r = client.post("/ingest/consolidated", data=data, content_type="multipart/form-data",
                    follow_redirects=True)
    assert r.status_code == 200
    assert conn.execute("SELECT 1 FROM sample WHERE bg_id='WEBIMP'").fetchone()
