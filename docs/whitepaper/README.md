# White paper — Linking laboratory data to FHAB incidents

A short data‑governance white paper proposing a **Sampling Event Code (SEC)** issued at sampling
time and carried on every communication (CoC, sample order, lab deliverable, filenames) so that
event → sample → result linkage happens by construction rather than by after‑the‑fact archaeology.

## Files

| File | What it is |
|------|------------|
| `index.html` | The web page (standalone, GitHub‑Pages‑ready). |
| `FHAB-Lab-Data-Linkage-Whitepaper-DRAFT.docx` | Editable Word version (regenerated, not hand‑edited). |
| `scripts/build_docx.py` | **Single source of truth for the .docx** — mirrors `index.html`. |
| `artifact.html` | Body‑only copy used to publish the shareable claude.ai artifact. |

## Regenerate the Word doc

```bash
cd docs/whitepaper
python3 scripts/build_docx.py      # needs: pip install --user python-docx
```

When the page content changes in a way that should reach the download, edit the text in
`scripts/build_docx.py` and re‑run — do not hand‑edit the `.docx`. To refresh `artifact.html`
after editing `index.html`, strip the `<style>` block + `<body>` inner HTML into it.

Status: **draft for discussion.**
