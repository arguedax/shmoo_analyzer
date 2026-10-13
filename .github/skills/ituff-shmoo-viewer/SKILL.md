---
name: ituff-shmoo-viewer
description: 'Extract shmoo results from ituff files (.itf / .itf.gz) and build a single-file interactive HTML shmoo viewer (per unit and test, hover/click shows limiting pattern, plist, fail vector, pins) plus optional per-point CSV. Use when asked to pull, plot, view, or compare shmoos from an ituff, hdmxdata path, lot folder, ShmooParams/ShmooResults/LEGEND records.'
argument-hint: '<ituff path | folder | glob> [more...] [--filter REGEX] [--csv]'
---

# Ituff Shmoo Viewer

Parses `_ShmooParams`, `_ShmooResults` and `^LEGEND^<C>` records from one or more ituffs and writes a self-contained HTML (no external deps, shareable as one file).

## Procedure

1. Collect inputs from the user's request: ituff file(s) (`.itf` / `.itf.gz`, UNC paths OK), folders, or globs. Optional test-name regex filter. Ask only if no input path is given.
2. Choose output names from the first input file stem (strip `.itf(.gz)`), writing into the workspace `ituff_shmoo_viewer/` folder:
   - HTML: `ituff_shmoo_viewer/<stem>_shmoo.html`
   - CSV (only if the user asks for CSV / raw data): `ituff_shmoo_viewer/<stem>_shmoo.csv`
3. Run from the workspace root (venv Python):
   ```powershell
   python .github\skills\ituff-shmoo-viewer\scripts\ituff_shmoo_html.py "<input>" [more inputs] -o "ituff_shmoo_viewer\<stem>_shmoo.html" [--csv "ituff_shmoo_viewer\<stem>_shmoo.csv"] [--filter "<regex>"]
   ```
4. Report the printed counts (units, shmoo tests, shmoos) and link the output file(s). If 0 shmoos were found, say so and suggest checking the filter or that the ituff contains `_ShmooResults` records.
5. If the user wants to see it, open the HTML in the integrated browser (`file:///` URL).

## Notes

- Script: [ituff_shmoo_html.py](./scripts/ituff_shmoo_html.py). `--help` for options.
- Result rows = Y steps starting at Y start (bottom of plot); chars = X steps. `*` = pass, other letters map to LEGEND entries.
- Units are separated by `2_lbeg`/`2_lend`; label = visual ID | site | start time; bins come from `3_curibin`/`3_curfbin`. Test keys include the die from `0_tssid` (e.g. `[U1]`).
- Multiple ituffs/folders can be combined into one HTML for cross-unit comparison.
