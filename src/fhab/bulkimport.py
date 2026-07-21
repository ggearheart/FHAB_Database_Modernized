"""Import a prepared consolidated CEDEN-long CSV (one file, many sampling events).

Complements the per-folder ingest: a single file where geocoding (from the registry or the CoC)
and the routine/link classification are already computed. Groups rows by `SamplingEvent` into one
lab_batch (sampling event) each, materializes samples + results, sets each sample's location from
the file's Latitude/Longitude, and tags routine samples. Samples come in unlinked (for the
workboard to reconcile) except those marked routine. Uses the owner connection like the folder ingest.

Expected columns: SamplingEvent, Region, StationCode, StationName, SampleDate, SampleTime,
ProjectCode, LabBatch, BG_ID, LabSampleID, SampleType, Analyte, MethodName, Result, ResQualCode,
Units, Fraction, MatrixName, Latitude, Longitude, GeocodeSource, SamplingType.
"""

from __future__ import annotations

import csv
from collections import defaultdict

from .ceden import CedenLoader
from .parsing import clean, parse_date, parse_float


def _rows_from(path_or_rows):
    if isinstance(path_or_rows, (list, tuple)):
        return list(path_or_rows)
    with open(path_or_rows, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def import_consolidated(conn, path_or_rows) -> dict:
    """Import the consolidated CSV. Returns {events, samples, geocoded, routine, results}."""
    rows = _rows_from(path_or_rows)
    loader = CedenLoader(conn)            # reuse station + analyte resolution
    events = defaultdict(list)
    for r in rows:
        events[(r.get("SamplingEvent") or "").strip()].append(r)

    stats = {"events": 0, "samples": 0, "geocoded": 0, "routine": 0, "results": 0}
    for ev, erows in events.items():
        if not ev:
            continue
        region = clean(erows[0].get("Region"))
        n_samp = sum(1 for _ in _group_samples(erows))
        bid = conn.execute(
            """INSERT INTO lab_batch (kind, source, region, status, n_samples)
               VALUES ('ingested', %s, %s, 'open', %s) RETURNING id""",
            (ev, region, n_samp)).fetchone()["id"]
        stats["events"] += 1

        geoc = 0
        for key, srows in _group_samples(erows):
            r0 = srows[0]
            station_id = loader._station_id(r0.get("StationCode"), r0.get("StationName"))
            lat, lon = parse_float(r0.get("Latitude")), parse_float(r0.get("Longitude"))
            if station_id is not None and lat is not None and lon is not None:
                conn.execute(
                    """UPDATE station SET geom = ST_SetSRID(ST_MakePoint(%s,%s),4326),
                           datum = COALESCE(datum, 'WGS84') WHERE id = %s AND geom IS NULL""",
                    (lon, lat, station_id))
            routine = (clean(r0.get("SamplingType")) or "").lower() == "routine"
            bg = clean(r0.get("BG_ID"))
            sid = conn.execute(
                """INSERT INTO sample (station_id, lab_batch_id, sample_date, sample_time,
                     sample_type, bg_id, lab_sample_id, project_code, lab_batch, sampling_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (bg_id) WHERE bg_id IS NOT NULL
                     DO UPDATE SET lab_batch_id = EXCLUDED.lab_batch_id
                   RETURNING id""",
                (station_id, bid, parse_date(r0.get("SampleDate")),
                 clean(r0.get("SampleTime")) or None, clean(r0.get("SampleType")), bg,
                 clean(r0.get("LabSampleID")), clean(r0.get("ProjectCode")),
                 clean(r0.get("LabBatch")), "routine" if routine else None)).fetchone()["id"]
            stats["samples"] += 1
            if lat is not None and lon is not None:
                geoc += 1
            if routine:
                stats["routine"] += 1
            for r in srows:
                analyte = clean(r.get("Analyte"))
                if analyte is None:
                    continue
                analyte_id = loader._analyte_id(analyte, r.get("MethodName"))
                mval = parse_float(r.get("Result"))
                ruid = f"{bg or key}:{analyte}:{clean(r.get('Units')) or ''}"
                conn.execute(
                    """INSERT INTO result
                         (result_id_unique, sample_id, analyte_id, data_type, method,
                          measurement_value, measurement_text, measurement_unit, res_qual_code,
                          fraction_name, matrix_name)
                       VALUES (%s,%s,%s,'Laboratory',%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (result_id_unique) DO UPDATE SET
                         measurement_value = EXCLUDED.measurement_value,
                         res_qual_code = EXCLUDED.res_qual_code""",
                    (ruid, sid, analyte_id, clean(r.get("MethodName")), mval,
                     clean(r.get("Result")) if mval is None else None,
                     clean(r.get("Units")), clean(r.get("ResQualCode")),
                     clean(r.get("Fraction")), clean(r.get("MatrixName"))))
                stats["results"] += 1
        stats["geocoded"] += geoc
        conn.execute("UPDATE lab_batch SET n_geocoded=%s WHERE id=%s", (geoc, bid))
    conn.commit()
    return stats


def _group_samples(erows):
    """Group an event's rows into samples by (station, date, bg_id, lab_sample_id)."""
    samples = defaultdict(list)
    for r in erows:
        key = (clean(r.get("StationCode")), clean(r.get("SampleDate")),
               clean(r.get("BG_ID")) or "", clean(r.get("LabSampleID")) or "")
        samples[key].append(r)
    return list(samples.items())
