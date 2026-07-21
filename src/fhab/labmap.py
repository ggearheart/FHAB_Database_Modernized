"""Lab-data map: per-station lab results with a swim-advisory colour tier + a trend view.

Plots geocoded lab samples on a map, one marker per station. Marker **shape** encodes the
lab method (● chemistry / ▲ genetic / ■ microscopy); marker **colour** encodes status —
chemistry uses California's recreational HAB trigger levels (the CCHAB/OEHHA voluntary
guidance action levels for microcystins, anatoxin-a and cylindrospermopsin; detection for
saxitoxin, which has no published recreational level), while genetic and microscopy assays
have no swim threshold so they read simply as target-detected / non-detect / no-result.

`station_trend` backs a per-station time-series page for locations sampled more than once.
"""

from __future__ import annotations

from collections import defaultdict

from .auth import acting_as

# The four cyanotoxins we tier on, and their recreational trigger levels in ug/L (water grabs).
# None as a lower bound means "any detection" triggers that tier. Ordered low->high severity.
CYANOTOXINS = ("Microcystins", "Anatoxin-a", "Cylindrospermopsin", "Saxitoxin")
TRIGGERS = {
    "Microcystins":       {"caution": 0.8, "warning": 6.0, "danger": 20.0},
    "Anatoxin-a":         {"caution": None, "warning": 20.0, "danger": 90.0},   # detection -> Caution
    "Cylindrospermopsin": {"caution": 1.0, "warning": 4.0, "danger": 17.0},
    "Saxitoxin":          {"caution": None, "warning": None, "danger": None},   # detection -> Caution
}

# Chemistry advisory-tier presentation, shared with the map legend/tooltip. order sorts worst-last.
TIER_META = {
    "none":    {"order": 0, "color": "#94a3b8", "label": "No toxin result",
                "guidance": "No cyanotoxin water result at this station (gene/microscopy only, or awaiting analysis)."},
    "nondetect": {"order": 1, "color": "#16a34a", "label": "Non-detect",
                "guidance": "Cyanotoxins below the reporting limit in the latest water grab."},
    "caution": {"order": 2, "color": "#eab308", "label": "Caution",
                "guidance": "Toxins detected at low levels. Advise the public to watch for scums and keep kids and pets away from algae."},
    "warning": {"order": 3, "color": "#ea580c", "label": "Warning",
                "guidance": "Toxins above the Warning trigger. Recreational-use warning is appropriate; avoid contact with water and scum."},
    "danger":  {"order": 4, "color": "#dc2626", "label": "Danger",
                "guidance": "Toxins above the Danger trigger. Danger advisory is appropriate; no water contact — a health hazard to people and animals."},
}
TIER_ORDER = sorted(TIER_META, key=lambda k: TIER_META[k]["order"])

# Lab category -> marker shape. Chemistry is coloured by TIER_META; the rest by DETECT_META.
# SPATT is chemically an ELISA assay but on a passive solid-phase sampler (toxins/g, no swim
# threshold), so the map treats it as its own category rather than folding it into water chemistry.
METHOD_META = {
    "chemistry":  {"label": "Chemistry (ELISA toxins, µg/L)", "shape": "circle"},
    "spatt":      {"label": "SPATT (passive sampler, toxins/g)", "shape": "diamond"},
    "genetic":    {"label": "Genetic (qPCR gene assay)", "shape": "triangle"},
    "microscopy": {"label": "Microscopy (cell ID / counts)", "shape": "square"},
}
METHOD_ORDER = ["chemistry", "spatt", "genetic", "microscopy"]

# Detection status for genetic/microscopy (no swim threshold applies).
DETECT_META = {
    "none":      {"color": "#94a3b8", "label": "No result",
                  "guidance": "No result of this type at this station yet."},
    "nondetect": {"color": "#16a34a", "label": "Non-detect",
                  "guidance": "Assay run; the target was below detection."},
    "detected":  {"color": "#d97706", "label": "Target detected",
                  "guidance": "The assay detected its target (e.g. a toxin-producing gene or cyanobacteria). A screening signal, not a toxin concentration."},
}


def method_of(analysis_type: str) -> str:
    at = (analysis_type or "").strip().lower()
    if at == "genetic":
        return "genetic"
    if at == "microscopy":
        return "microscopy"
    if at in ("cyanotoxin", "pigment"):
        return "chemistry"
    return "other"


def _is_spatt(unit: str) -> bool:
    """SPATT passive-sampler results are reported per gram of resin (toxins/g, toxin/g)."""
    u = (unit or "").lower().replace(" ", "")
    return "toxin/g" in u or "toxins/g" in u


def result_category(analysis_type: str, unit: str) -> str:
    """Map one result to a map category: spatt (toxins/g) wins over its ELISA analysis_type."""
    if _is_spatt(unit):
        return "spatt"
    return method_of(analysis_type)


def tier_for(analyte: str, value, is_nd: bool) -> str:
    """Advisory tier for one cyanotoxin water result. `value` in ug/L (None if not quantified)."""
    if analyte not in TRIGGERS:
        return "none"
    t = TRIGGERS[analyte]
    if is_nd or value is None or value <= 0:
        return "nondetect"
    v = float(value)
    if t["danger"] is not None and v >= t["danger"]:
        return "danger"
    if t["warning"] is not None and v >= t["warning"]:
        return "warning"
    if t["caution"] is None or v >= t["caution"]:
        return "caution"          # caution None => any detection triggers Caution
    return "nondetect"            # detected but below the Caution level


def _worse(a: str, b: str) -> str:
    return a if TIER_META[a]["order"] >= TIER_META[b]["order"] else b


def _is_detected(value, rqc: str) -> bool:
    if (rqc or "").upper() == "ND":
        return False
    return value is not None and float(value) > 0


def lab_map_features(conn, uid, *, region=None, days=None, tier=None, kind=None, method=None) -> list[dict]:
    """One feature per geocoded station.

    Filters: region (lab_batch.region), days (recent samples only), tier (chemistry advisory
    tier), kind ('routine' | 'linked' | 'unlinked'), method ('chemistry' | 'genetic' | 'microscopy').
    Marker shape follows the displayed method; colour follows its status.
    """
    cond = ["st.geom IS NOT NULL"]
    p: dict = {}
    if region:
        cond.append("b.region = %(region)s"); p["region"] = region
    if days:
        cond.append("s.sample_date >= current_date - %(days)s::int"); p["days"] = int(days)
    if kind == "routine":
        cond.append("s.sampling_type = 'routine'")
    elif kind == "linked":
        cond.append("(s.bloom_report_id IS NOT NULL OR s.case_id IS NOT NULL)")
    elif kind == "unlinked":
        cond.append("s.bloom_report_id IS NULL AND s.case_id IS NULL "
                    "AND s.sampling_type IS DISTINCT FROM 'routine'")

    with acting_as(conn, uid):
        rows = conn.execute(
            f"""SELECT st.id AS station_id, ST_Y(st.geom) AS lat, ST_X(st.geom) AS lon,
                       st.station_code, st.station_name, b.region,
                       s.id AS sample_id, s.sample_date, s.sampling_type,
                       s.bloom_report_id, s.case_id, s.lab_batch_id, b.source AS event_name,
                       a.analyte, a.analysis_type, r.measurement_value AS val,
                       r.measurement_unit AS unit, r.measurement_text AS txt, r.res_qual_code AS rqc
                FROM sample s
                JOIN station st ON st.id = s.station_id
                LEFT JOIN lab_batch b ON b.id = s.lab_batch_id
                LEFT JOIN result r ON r.sample_id = s.id
                LEFT JOIN analyte a ON a.id = r.analyte_id
                WHERE {' AND '.join(cond)}""",
            p).fetchall()

    stations: dict = {}
    for r in rows:
        sid = r["station_id"]
        st = stations.get(sid)
        if st is None:
            st = stations[sid] = {
                "station_id": sid, "lat": r["lat"], "lon": r["lon"],
                "station_code": r["station_code"], "station_name": r["station_name"],
                "region": r["region"], "samples": set(), "events": {}, "last_sample": None,
                "linked": False, "routine": False, "methods": set(), "dates": set(),
                "toxins": {}, "spatt": {}, "genes": {}, "taxa": [],
            }
        st["samples"].add(r["sample_id"])
        if r["lab_batch_id"]:
            st["events"][r["lab_batch_id"]] = r["event_name"]
        if r["bloom_report_id"] or r["case_id"]:
            st["linked"] = True
        if r["sampling_type"] == "routine":
            st["routine"] = True
        d = str(r["sample_date"]) if r["sample_date"] else None
        if d:
            st["dates"].add(d)
            if st["last_sample"] is None or d > st["last_sample"]:
                st["last_sample"] = d

        cat = result_category(r["analysis_type"], r["unit"])
        analyte, unit = r["analyte"], (r["unit"] or "")
        if cat in METHOD_META:
            st["methods"].add(cat)
        if cat == "chemistry" and analyte in CYANOTOXINS and "g/l" in unit.lower():   # tiered toxin
            is_nd = (r["rqc"] or "").upper() == "ND" or r["val"] is None
            val = None if is_nd else float(r["val"])
            cur = st["toxins"].get(analyte)
            if cur is None:
                st["toxins"][analyte] = {"max": val, "unit": "µg/L", "latest": val,
                                         "latest_date": d, "nd": is_nd}
            else:
                if val is not None and (cur["max"] is None or val > cur["max"]):
                    cur["max"] = val
                if d and (cur["latest_date"] is None or d > cur["latest_date"]):
                    cur["latest"], cur["latest_date"], cur["nd"] = val, d, is_nd
        elif cat == "spatt" and analyte:
            det = _is_detected(r["val"], r["rqc"])
            sp = st["spatt"].setdefault(analyte, {"detected": False, "unit": unit or "toxins/g"})
            sp["detected"] = sp["detected"] or det
        elif cat == "genetic" and analyte:
            det = _is_detected(r["val"], r["rqc"])
            g = st["genes"].setdefault(analyte, {"detected": False, "unit": unit or "copies/mL"})
            g["detected"] = g["detected"] or det
        elif cat == "microscopy" and (r["txt"] or analyte):
            label = r["txt"] or analyte
            if label and label not in st["taxa"]:
                st["taxa"].append(label)

    features = []
    for st in stations.values():
        methods = st["methods"]
        # Choose which category the marker displays: an explicit filter (station must have it),
        # else chemistry > spatt > genetic > microscopy by information value.
        if method in METHOD_META:
            if method not in methods:
                continue
            dm = method
        else:
            dm = next((m for m in METHOD_ORDER if m in methods), None)
            if dm is None:
                continue

        if dm == "chemistry":
            worst = "none"
            for a, tox in st["toxins"].items():
                worst = _worse(worst, tier_for(a, tox["max"], tox["nd"]))
            if tier and worst != tier:
                continue
            meta = TIER_META[worst]
            status_key, color, label, guidance = worst, meta["color"], meta["label"], meta["guidance"]
        else:
            if tier:                                  # tier filter is a chemistry concept
                continue
            if dm == "genetic":
                det = any(g["detected"] for g in st["genes"].values())
                dk = "detected" if det else ("nondetect" if st["genes"] else "none")
            elif dm == "spatt":
                det = any(sp["detected"] for sp in st["spatt"].values())
                dk = "detected" if det else ("nondetect" if st["spatt"] else "none")
            else:  # microscopy
                dk = "detected" if st["taxa"] else "none"
            meta = DETECT_META[dk]
            status_key, color, label, guidance = dk, meta["color"], meta["label"], meta["guidance"]

        events = [{"id": eid, "name": name} for eid, name in st["events"].items()]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
            "properties": {
                "station_id": st["station_id"], "station_code": st["station_code"],
                "station_name": st["station_name"], "region": st["region"],
                "method": dm, "shape": METHOD_META[dm]["shape"],
                "status_key": status_key, "tier": status_key if dm == "chemistry" else None,
                "status_label": label, "color": color, "guidance": guidance,
                "methods": sorted(methods), "n_samples": len(st["samples"]),
                "n_dates": len(st["dates"]), "last_sample": st["last_sample"],
                "linked": st["linked"], "routine": st["routine"],
                "toxins": st["toxins"],
                "spatt": [{"name": k, "detected": v["detected"]} for k, v in st["spatt"].items()],
                "genes": [{"name": k, "detected": v["detected"]} for k, v in st["genes"].items()],
                "taxa": st["taxa"], "events": events,
            },
        })
    # draw chemistry worst-tier last (on top); non-chemistry sit below
    features.sort(key=lambda f: TIER_META.get(f["properties"]["tier"], {"order": 0})["order"])
    return features


def tier_counts(features: list[dict]) -> dict:
    c: dict = defaultdict(int)
    for f in features:
        c[f["properties"]["status_key"]] += 1
    return dict(c)


def method_counts(features: list[dict]) -> dict:
    c: dict = defaultdict(int)
    for f in features:
        c[f["properties"]["method"]] += 1
    return dict(c)


def station_trend(conn, uid, station_id: int) -> dict:
    """Time series for one station: numeric lab results grouped by (analyte, unit) over date.

    Backs the trend page linked from the map tooltip when a station has >1 sample date.
    """
    with acting_as(conn, uid):
        st = conn.execute(
            """SELECT id, station_code, station_name FROM station WHERE id = %s""",
            (station_id,)).fetchone()
        rows = conn.execute(
            """SELECT s.sample_date, a.analyte, a.analysis_type, r.measurement_value AS val,
                      r.measurement_unit AS unit, r.res_qual_code AS rqc
               FROM sample s
               JOIN result r ON r.sample_id = s.id
               LEFT JOIN analyte a ON a.id = r.analyte_id
               WHERE s.station_id = %s AND s.sample_date IS NOT NULL AND a.analyte IS NOT NULL
               ORDER BY s.sample_date""",
            (station_id,)).fetchall()

    series: dict = {}
    for r in rows:
        analyte, unit = r["analyte"], (r["unit"] or "")
        key = f"{analyte}|{unit}"
        s = series.get(key)
        if s is None:
            tox = analyte if (analyte in TRIGGERS and "g/l" in unit.lower()) else None
            s = series[key] = {
                "analyte": analyte, "unit": unit, "method": method_of(r["analysis_type"]),
                "thresholds": TRIGGERS.get(tox) if tox else None, "points": [],
            }
        is_nd = (r["rqc"] or "").upper() == "ND" or r["val"] is None
        s["points"].append({"date": str(r["sample_date"]),
                            "value": None if is_nd else float(r["val"]), "nd": is_nd})

    # Keep series with at least two dated points; toxins first, then the rest.
    kept = [v for v in series.values() if len(v["points"]) >= 2]
    kept.sort(key=lambda v: (v["thresholds"] is None, v["analyte"]))
    return {"station": dict(st) if st else None, "series": kept,
            "n_points": len(rows)}
