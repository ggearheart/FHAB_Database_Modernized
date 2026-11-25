"""Build small text-layer PDFs for tests (no external libraries).

Each page is a list of lines; a line is a list of (x, text) runs placed on one baseline, so tests
can lay out table columns the way a lab report's PDF does."""


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def mini_pdf(pages: list[list[list[tuple[float, str]]]]) -> bytes:
    objs: list[bytes] = []
    kids = []
    font_id = 3
    for p, lines in enumerate(pages):
        ops = ["BT", "/F1 9 Tf"]
        y = 760
        for runs in lines:
            for x, text in runs:
                ops.append(f"1 0 0 1 {x} {y} Tm ({_esc(text)}) Tj")
            y -= 14
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        content_id = 4 + 2 * p
        page_id = content_id + 1
        objs.append((content_id, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        objs.append((page_id, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 792 792] "
                               f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                               f"/Contents {content_id} 0 R >>").encode()))
        kids.append(f"{page_id} 0 R")
    objs.insert(0, (1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.insert(1, (2, f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()))
    objs.insert(2, (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num, body in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    n = max(offsets) + 1
    out += f"xref\n0 {n}\n0000000000 65535 f \n".encode()
    for i in range(1, n):
        out += f"{offsets[i]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
