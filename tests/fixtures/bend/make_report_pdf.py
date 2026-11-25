"""Synthetic Bend-style PDF report (made-up stations/IDs) for parser tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.pdfmini import mini_pdf  # noqa: E402

LETTERHEAD = [[(80, "Example Lab, LLC"), (420, "Project:"), (470, "TEST_SWAMP_2022")],
              [(80, "1 Example Way"), (470, "Analysis for Toxic Cyanobacteria")],
              [(80, "Sacramento, CA 95825"), (420, "Project #:"), (470, "00-000-000")],
              [(80, "Tel: (000) 000-0000"), (420, "Reported:"), (470, "6/24/2022 10:00")], []]
COLS_S = [20, 90, 200, 290, 380, 440, 510]
COLS_R = [20, 90, 190, 300, 370, 440, 510]
COLS_M = [20, 90, 200, 290, 400]


def row(xs, cells):
    return [(x, c) for x, c in zip(xs, cells) if c]


def build() -> bytes:
    cover = [[(40, "Example Lab, LLC")], [(40, "Date:"), (120, "6/24/2022")],
             [(40, "Testing results are enclosed for ELISA analyses.")],
             [(520, "Page 1 of 4")]]
    p2 = LETTERHEAD + [[(30, "ANALYTICAL REPORT FOR SAMPLES")],
          row(COLS_S, ["Sample ID", "Location", "Date Collected", "Date Received", "Matrix", "Preserved", "BG_ID"]),
          row(COLS_S, ["999TSTC01", "", "6/20/2022 10:58", "6/21/2022 9:33", "Water", "N", "TP0001"]),
          row(COLS_S, ["Test Park", "38.32465 -122.34848", "6/20/2022 0:00", "6/21/2022 9:33", "Water", "N", "TP0002"]),
          [], [(520, "Page 2 of 4")]]
    p3 = LETTERHEAD + [[(30, "SAMPLE RESULTS")],
          [(370, "Quantitation")],
          row(COLS_R, ["Sample ID", "Method", "Target Analyte", "Result", "Limit", "Units", "Notes"]),
          row(COLS_R, ["999TSTC01", "ELISA", "Microcystin/Nod.", "0.24", "0.15", "ug/L", ""]),
          row(COLS_R, ["999TSTC01", "QPCR", "Anatoxin-a", "ND", "100", "copies/mL", "U"]),
          row(COLS_R, ["Test Park", "Spectrophotometry", "Total Nitrogen", "0.66", "1.00", "mg/L", "C1,J"]),
          row(COLS_R, ["Test Park", "Fluorometry", "Chlorophyll a", "21.96", "0.20", "ug/L", ""]),
          [], [(520, "Page 3 of 4")]]
    p4 = LETTERHEAD + [[(30, "MICROSCOPY RESULTS - Identification of CyanoHABs")],
          row(COLS_M, ["Sample ID", "Dominant", "Sub-dominant", "Also present", "Notes"]),
          row(COLS_M, ["999TSTC01", "Microcystis", "", "Dolichospermum", "This sample contained a"]),
          [(400, "moderate amount of"), (560, "Microcystis")],          # a gap inside the note
          row(COLS_M, ["Test Park", "NA", "", "", "No cyanobacteria were observed."]),
          [], [(210, "Microcystis")], [],
          [(20, "MICROSCOPY RESULTS - Enumeration of CyanoHABs"), (560, "Total")],
          [(200, "Density"), (290, "Cell Biovolume"), (380, "Biovolume")],
          [(20, "Sample ID"), (90, "Genus"), (200, "(cells/mL)"), (290, "(\u00b5m3)"),
           (380, "(\u00b5m3/mL)"), (500, "Notes")],
          [(20, "999TSTC01"), (90, "Microcystis"), (200, "12,183"), (290, "22.4"), (380, "272,899.2")],
          [(20, "999TSTC01"), (90, "Dolichospermum"), (200, "640"), (290, "65.0"), (380, "41,600.0")],
          [],
          [(30, "QUALITY CONTROL")],
          [(30, "Method"), (110, "Analyte"), (200, "Result")],
          [(30, "ELISA"), (110, "MC - Blank"), (200, "ND")],
          [(520, "Page 4 of 4")]]
    return mini_pdf([cover, p2, p3, p4])


if __name__ == "__main__":
    Path(sys.argv[1]).write_bytes(build())


def build_watsonville() -> bytes:
    """Synthetic single-sample city-lab report in the Watsonville layout (invented people/IDs)."""
    lines = [[(40, "City of Watsonville")], [(220, "Public Utilities Department Laboratory")],
             [(260, "ANALYTICAL REPORT")], [(40, "Example Contact")], [(40, "contact@example.org")],
             [(150, "HABID:"), (230, "9999")],
             [(110, "Laboratory ID No:"), (230, "230321-0001")],
             [(130, "Sample Name:"), (230, "Test Lake Boat Launch Grab - TL1"), (520, "Sample Type: Grab")],
             [(60, "Collection Date/Time:"), (230, "03/21/23 10:45 AM"), (520, "Sample Collector:"), (640, "A. Sampler")],
             [(150, "Location:"), (230, "Boat Launch TL1")],
             [(60, "Laboratory Received:"), (230, "03/21/23 11:07 AM")],
             [(130, "Microscopic"), (230, "No cyanobacteria identified. Few ciliates.")],
             [(150, "Analysis:")],
             [(40, "Results:")],
             [(40, "Analyte"), (210, "Method"), (330, "Units"), (400, "Result"), (470, "Qual"),
              (520, "PQL"), (570, "MDL"), (620, "Analyzed Date/Time")],
             [(40, "*Microcystin-Abraxis"), (210, "Strip Test Abraxis"), (330, "ppb"), (400, "<1"),
              (620, "03/21/23 11:10 AM")],
             [],
             [(200, "Report Approved by:")]]
    return mini_pdf([lines])
