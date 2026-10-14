---
name: shmoo-nav
description: 'Extract shmoo results from ituff files (.itf / .itf.gz) and build a single-file interactive HTML shmoo viewer (per unit and test, hover/click shows limiting pattern, plist, fail vector, pins) plus optional per-point CSV. Accepts a bare VPO / lot ID (e.g. I642014CR) and finds its ituffs on the hdmxdata shares automatically. Use when asked to pull, plot, view, or compare shmoos from a VPO, ituff, hdmxdata path, lot folder, ShmooParams/ShmooResults/LEGEND records.'
argument-hint: '<VPO | ituff path | folder | glob> [more...] [--filter REGEX] [--csv]'
---

# Shmoo-Nav

Parses `_ShmooParams`, `_ShmooResults` and `^LEGEND^<C>` records from one or more ituffs and writes a self-contained HTML (no external deps, shareable as one file).

## Procedure

1. Collect inputs from the user's request: VPO / lot ID(s) (e.g. `I642014CR`), ituff file(s) (`.itf` / `.itf.gz`, UNC paths OK), folders, or globs. Optional test-name regex filter. Ask only if no input is given.
   - A bare VPO is passed as-is; the script searches `\\amr.corp.intel.com\ec\proj\mdl\<site>\intel\hdmxdata\{prod,engr}\<VPO>_*` across sites `sc,cr,an,fm,jf` (about 10 s) and uses every ituff chunk in the match. If the user names a site, add `--sites <site>` to skip the others.
2. Choose output names, writing into the workspace `ituff_shmoo_viewer/` folder:
   - For a VPO input: `<VPO>_shmoo.html` (the matched folder is usually `<VPO>_<lcode>`; use that name if known).
   - Otherwise from the first input file stem (strip `.itf(.gz)`).
   - HTML: `ituff_shmoo_viewer/<stem>_shmoo.html`
   - CSV (only if the user asks for CSV / raw data): `ituff_shmoo_viewer/<stem>_shmoo.csv`
3. Run from the workspace root (venv Python):
   ```powershell
   python .github\skills\shmoo-nav\scripts\shmoo_nav.py "<VPO or path>" [more inputs] -o "ituff_shmoo_viewer\<stem>_shmoo.html" [--csv "ituff_shmoo_viewer\<stem>_shmoo.csv"] [--filter "<regex>"] [--sites sc]
   ```
4. Report the matched lot folder (printed as `VPO <id>: <path>`), the printed counts (units, shmoo tests, shmoos) and link the output file(s). If the VPO wasn't found, say which sites were searched and ask whether it ran at another site or has aged off the share (shares keep roughly the last month). If 0 shmoos were found, suggest checking the filter or that the ituff contains `_ShmooResults` records.
5. If the user wants to see it, open the HTML in the integrated browser (`file:///` URL).

## Notes

- Script: [shmoo_nav.py](./scripts/shmoo_nav.py). `--help` for options.
- Result rows = Y steps starting at Y start (bottom of plot); chars = X steps. `*` = pass, other letters map to LEGEND entries.
- Units are separated by `2_lbeg`/`2_lend`; label = visual ID | site | start time; bins come from `3_curibin`/`3_curfbin`. Test keys include the die from `0_tssid` (e.g. `[U1]`).
- Multiple ituffs/folders can be combined into one HTML for cross-unit comparison.
