"""Lab-data map: cyanotoxin tiering + per-station features + the geojson route."""

import pytest

from fhab.bulkimport import import_consolidated
from fhab.labmap import lab_map_features, tier_counts, tier_for


def _row(**kw):
    base = {"SamplingEvent": "Clear Lake (RB5)", "Region": "Region 5", "StationCode": "IMP1",
            "StationName": "Imp Lake", "SampleDate": "6/2/2025", "SampleTime": "9:15",
            "ProjectCode": "P", "LabBatch": "1", "BG_ID": "IMPA", "LabSampleID": "L1",
            "SampleType": "Water Grab", "Analyte": "Microcystins", "MethodName": "ELISA",
            "Result": "4.13", "ResQualCode": "", "Units": "ug/L", "Fraction": "Total",
            "MatrixName": "samplewater", "Latitude": "39.0", "Longitude": "-122.8", "SamplingType": ""}
    base.update(kw)
    return base


def test_tier_for():
    assert tier_for("Microcystins", 0.5, False) == "nondetect"   # detected, below Caution
    assert tier_for("Microcystins", 0.9, False) == "caution"
    assert tier_for("Microcystins", 10, False) == "warning"
    assert tier_for("Microcystins", 25, False) == "danger"
    assert tier_for("Microcystins", None, True) == "nondetect"
    assert tier_for("Anatoxin-a", 0.1, False) == "caution"       # any detection -> Caution
    assert tier_for("Saxitoxin", 0.3, False) == "caution"        # detection-based
    assert tier_for("Chlorophyll a", 5, False) == "none"         # not a tiered toxin


def test_features_worst_tier_and_metadata(conn):
    from fhab.auth import create_user, grant_role
    uid = create_user(conn, "map@wb.ca.gov"); grant_role(conn, uid, "wb_staff", region="Region 5")
    import_consolidated(conn, [
        _row(Result="25", BG_ID="IMPA"),                          # microcystins Danger
        _row(Analyte="Anatoxin-a", Result="0.5", BG_ID="IMPA"),   # detection at same station
        _row(StationCode="RT1", StationName="Routine Pond", BG_ID="RTB", Latitude="38.5",
             Longitude="-121.4", SamplingType="routine", Result="", ResQualCode="ND"),  # ND routine
    ])
    feats = lab_map_features(conn, uid)
    by_code = {f["properties"]["station_code"]: f["properties"] for f in feats}

    imp = by_code["IMP1"]
    assert imp["tier"] == "danger"                                # worst across its toxins
    assert imp["toxins"]["Microcystins"]["max"] == 25.0
    assert "Anatoxin-a" in imp["toxins"]
    assert imp["region"] == "Region 5" and imp["last_sample"] == "2025-06-02"

    rt = by_code["RT1"]
    assert rt["tier"] == "nondetect" and rt["routine"] is True

    # tier filter + counts
    only_danger = lab_map_features(conn, uid, tier="danger")
    assert [f["properties"]["station_code"] for f in only_danger] == ["IMP1"]
    assert tier_counts(feats)["danger"] == 1


@pytest.fixture()
def client(conn):
    from fhab.auth import create_user, grant_role, set_password
    from fhab.web import create_app
    from tests.conftest import TEST_DSN
    staff = create_user(conn, "staff@wb.ca.gov"); set_password(conn, staff, "pw")
    grant_role(conn, staff, "wb_staff", region="Region 5")
    app = create_app(dsn=TEST_DSN); app.config["TESTING"] = True
    return app.test_client()


def test_map_geojson_route(client, conn):
    import_consolidated(conn, [_row(Result="10", StationCode="WEB1", BG_ID="WEBA")])
    client.post("/login", data={"email": "staff@wb.ca.gov", "password": "pw"}, follow_redirects=True)
    r = client.get("/lab/map.geojson")
    assert r.status_code == 200
    fc = r.get_json()
    assert fc["type"] == "FeatureCollection"
    props = [f["properties"] for f in fc["features"]]
    web = next(p for p in props if p["station_code"] == "WEB1")
    assert web["tier"] == "warning" and web["tier_color"]
    # page renders
    assert client.get("/lab/map").status_code == 200
