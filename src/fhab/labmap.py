"""Lab-data map: per-station cyanotoxin summaries with a swim-advisory colour tier.

Plots geocoded lab samples on a map, one marker per station, coloured by the worst
cyanotoxin advisory tier seen at that station — mirroring the public "Can I Swim Here?"
map. Tiers use California's recreational HAB trigger levels (the CCHAB/OEHHA voluntary
guidance action levels for microcystins, anatoxin-a and cylindrospermopsin; detection
for saxitoxin, which has no published recreational level). SPATT (toxins/g) and qPCR
gene results are presence indicators, not tiered — they are summarised as metadata.
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

# Tier presentation, shared with the map legend/tooltip. order sorts worst-last.
TIER_META = {
    "none":    {"order": 0, "color": "#94a3b8", "label": "No toxin result",
                "guidance": "No cyanotoxin water result at this station (gene/SPATT only, or awaiting analysis)."},
    "nondetect": {"order": 1, "color": "#16a34a", "label": "Non-detect",
                "guidance": "Cyanotoxins below the reporting limit in the latest water grab."},
    "caution": {"order": 2, "color": "#eab308", "label": "Caution",
                "guidance": "Toxins detected at low levels. Advise the public to keep an eye out for scums and keep kids and pets away from algae."},
    "warning": {"order": 3, "color": "#ea580c", "label": "Warning",
                "guidance": "Toxins above the Warning trigger. Recreational-use warning is appropriate; avoid contact with water and scum."},
    "danger":  {"order": 4, "color": "#dc2626", "label": "Danger",
                "guidance": "Toxins above the Danger trigger. Danger advisory is appropriate; no water contact — a health hazard to people and animals."},
}
TIER_ORDER = sorted(TIER_META, key=lambda k: TIER_META[k]["order"])


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


def lab_map_features(conn, uid, *, region=None, days=None, tier=None, kind=None) -> list[dict]:
    """One feature per geocoded station: worst cyanotoxin tier + a metadata summary.

    Filters: region (lab_batch.region), days (recent samples only), tier (advisory tier),
    kind ('routine' | 'linked' | 'unlinked').
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
                       a.analyte, r.measurement_value AS val, r.measurement_unit AS unit,
                       r.res_qual_code AS rqc
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
                "linked": False, "routine": False, "toxins": {}, "spatt": 0, "genes": 0,
            }
        st["samples"].add(r["sample_id"])
        if r["lab_batch_id"]:
            st["events"][r["lab_batch_id"]] = r["event_name"]
        if r["bloom_report_id"] or r["case_id"]:
            st["linked"] = True
        if r["sampling_type"] == "routine":
            st["routine"] = True
        d = str(r["sample_date"]) if r["sample_date"] else None
        if d and (st["last_sample"] is None or d > st["last_sample"]):
            st["last_sample"] = d

        analyte, unit = r["analyte"], (r["unit"] or "")
        if analyte in CYANOTOXINS and "g/l" in unit.lower():           # ug/L water grab -> tiered
            is_nd = (r["rqc"] or "").upper() == "ND" or r["val"] is None
            val = None if is_nd else float(r["val"])
            cur = st["toxins"].get(analyte)
            # keep the highest value seen, and the value/date of the latest sample
            if cur is None:
                st["toxins"][analyte] = {"max": val, "unit": "ug/L", "latest": val,
                                         "latest_date": d, "nd": is_nd}
            else:
                if val is not None and (cur["max"] is None or val > cur["max"]):
                    cur["max"] = val
                if d and (cur["latest_date"] is None or d > cur["latest_date"]):
                    cur["latest"], cur["latest_date"], cur["nd"] = val, d, is_nd
        elif analyte in CYANOTOXINS and "toxin" in unit.lower():        # toxins/g -> SPATT indicator
            st["spatt"] += 1
        elif analyte and "gene" in analyte.lower():
            st["genes"] += 1

    features = []
    for st in stations.values():
        # Worst tier across the toxins present; "none" if no water cyanotoxin result at all.
        worst = "none"
        for analyte, tox in st["toxins"].items():
            worst = _worse(worst, tier_for(analyte, tox["max"], tox["nd"]))
        if tier and worst != tier:
            continue
        meta = TIER_META[worst]
        events = [{"id": eid, "name": name} for eid, name in st["events"].items()]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
            "properties": {
                "station_id": st["station_id"], "station_code": st["station_code"],
                "station_name": st["station_name"], "region": st["region"],
                "tier": worst, "tier_label": meta["label"], "tier_color": meta["color"],
                "guidance": meta["guidance"], "n_samples": len(st["samples"]),
                "last_sample": st["last_sample"], "linked": st["linked"],
                "routine": st["routine"], "spatt": st["spatt"], "genes": st["genes"],
                "events": events, "toxins": st["toxins"],
            },
        })
    # worst tier drawn last (on top)
    features.sort(key=lambda f: TIER_META[f["properties"]["tier"]]["order"])
    return features


def tier_counts(features: list[dict]) -> dict:
    c = defaultdict(int)
    for f in features:
        c[f["properties"]["tier"]] += 1
    return dict(c)
