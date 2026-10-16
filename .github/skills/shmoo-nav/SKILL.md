---
name: shmoo-nav
description: 'Extract shmoo results from ituff files (.itf / .itf.gz) and build a single-file interactive HTML shmoo viewer (per unit and test, hover/click shows limiting pattern, plist, fail vector, pins) plus optional per-point CSV. Accepts a bare VPO / lot ID (e.g. I642014CR) and finds its ituffs on the hdmxdata shares automatically. Also traces which test program (TPL / sub test plan folder) a lot ran with, and (--trace-patterns) traces each failing pattern through FLASH to its converted .hdmt.data source, resolving the fail vector to file line, nearest label and expected pin states. Use when asked to pull, plot, view, or compare shmoos from a VPO, ituff, hdmxdata path, lot folder, ShmooParams/ShmooResults/LEGEND records, to find the test program / TPL path for a VPO or ituff, or to trace a failing pattern / fail vector back to its source.'
argument-hint: '<VPO | ituff path | folder | glob> [more...] [--filter REGEX] [--csv] [--test-program] [--trace-patterns]'
---

# Shmoo-Nav

Parses `_ShmooParams`, `_ShmooResults` and `^LEGEND^<C>` records from one or more ituffs and writes a self-contained HTML (no external deps, shareable as one file).

## Procedure

1. Collect inputs from the user's request: VPO / lot ID(s) (e.g. `I642014CR`), ituff file(s) (`.itf` / `.itf.gz`, UNC paths OK), folders, or globs. Optional test-name regex filter. Ask only if no input is given.
   - A bare VPO is passed as-is; the script searches `\\amr.corp.intel.com\ec\proj\mdl\<site>\intel\hdmxdata\{prod,engr}\<VPO>_*` across sites `sc,cr,an,fm,jf` (about 1 s) and uses every ituff chunk in the match. If the user names a site, add `--sites <site>` to skip the others.
   - If the user only wants to know which test program a lot ran with (TPL / test plan / program path), add `--test-program`. It reads headers only and prints the program folder, `BaseTestPlan.tpl` and the flow's sub test plan (`.stpl`); no HTML is written, so skip steps 2 and 5.
   - If the user wants to know what caused a failure / trace a pattern to its source, add `--trace-patterns` (and `--flash-prod <PROD>` if not DIO). Combine with `--filter` to keep it fast.
2. Choose output names, writing into the workspace `ituff_shmoo_viewer/` folder:
   - For a VPO input: `<VPO>_shmoo.html` (the matched folder is usually `<VPO>_<lcode>`; use that name if known).
   - Otherwise from the first input file stem (strip `.itf(.gz)`).
   - HTML: `ituff_shmoo_viewer/<stem>_shmoo.html`
   - CSV (only if the user asks for CSV / raw data): `ituff_shmoo_viewer/<stem>_shmoo.csv`
3. Run from the workspace root (venv Python):
   ```powershell
   python .github\skills\shmoo-nav\scripts\shmoo_nav.py "<VPO or path>" [more inputs] -o "ituff_shmoo_viewer\<stem>_shmoo.html" [--csv "ituff_shmoo_viewer\<stem>_shmoo.csv"] [--filter "<regex>"] [--sites sc]
   ```
4. Report the matched lot folder (printed as `VPO <id>: <path>`), the test program (printed as `Test program <name>` with its folder, tpl and stpl), the printed counts (units, shmoo tests, shmoos) and link the output file(s). With `--trace-patterns`, also summarize each traced fail (`line N <opcode> <label>+<offset> expects <pin>=<H|L> [state note]`, or `CALL <sub> -> <label>+<offset> inside the subroutine`) and any patterns that could not be traced. If the VPO wasn't found, say which sites were searched and ask whether it ran at another site or has aged off the share (shares keep roughly the last month). If the test program wasn't found on any share, give the tester path the ituff recorded. If 0 shmoos were found, suggest checking the filter or that the ituff contains `_ShmooResults` records.
5. If the user wants to see it, open the HTML in the integrated browser (`file:///` URL).

## Notes

- Script: [shmoo_nav.py](./scripts/shmoo_nav.py). `--help` for options.
- Result rows = Y steps starting at Y start (bottom of plot); chars = X steps. `*` = pass, other letters map to LEGEND entries.
- Units are separated by `2_lbeg`/`2_lend`; label = visual ID | site | start time; bins come from `3_curibin`/`3_curfbin`. Test keys include the die from `0_tssid` (e.g. `[U1]`).
- Multiple ituffs/folders can be combined into one HTML for cross-unit comparison.
- Test program trace: the header attribute `4_tsattrs_tplpath` (and `stplpath`) records the program on the tester's `I:` drive, which is the site share's `\intel` folder (the part of the ituff path before `\hdmxdata\`). Other sites are tried as a fallback. `socpath` / `envpath` point at the tester's local `D:\TPRecipes` and can't be resolved remotely.
- LEGEND location is `<domain>(<cycle>,<vector>,..)`; the cycle is 0-based. The vector is a vector-memory address: every vector line and every instruction line (`MOV`, `CALL`, ...) in the domain's `.hdmt.data` takes one address; label lines don't.
- Each vector line in `.hdmt.data` ends with `# t_start # t_end # address # cycle(1-based) # tap_state # note`. The trace only accepts the vector at the LEGEND address if its cycle matches; the note (e.g. `ppsf_BP2_WBP0_MBISTPG_GO_0_1_0`) names the signal being captured and is the best "what failed" hint.
- If the cycle falls between the vectors around a `CALL`, the fail is inside the called common subroutine and the LEGEND vector is a subroutine address. The subroutine ships only compiled (`dio_com_*.pinObj`, a copy sits in the pattern folder); its per-domain label tables are decoded to name the nearest label (e.g. `SCOREBOARD_DUMP_STB+637` in `IMH2_MAINCAT_SUBROUTINE`). No line-level source exists for it.
- Pattern trace: `flash_search` (FLASH MCP at `https://mdcx-tvpv-flash.intel.com/mcp`, no auth, proxy bypassed) gives the pattern's `/p/pde/tvpv/<rel>` path and site. `/p/pde` isn't exported over samba; the same `<rel>` lives on one of `\\samba.<site>.intel.com\nfs\site\disks\mfg_tvpv_*` (DIO: `mfg_tvpv_321`), found by probing. The pattern folder also has `..\inputs\` (source `.itpp` / `.stil`) and `..\logs\` (trace2ate log).
- Pin names in `.hdmt.data` are written vertically in the `#` header lines, one column per pin state. `H`/`L` = compare high/low, `0`/`1` = drive, `CTV` = capture vector, `LRPT n` = repeat. HPTP domains also use serial `{ S { rx=..;tx=..;clk=.. }{..} }` vectors, which have no per-pin compares.
- Viewer: clicking a traced fail shows a "Vector view" grid (pins as vertical column headers, vectors as rows, colored drive/strobe states) for ~22 vectors around the fail, with the failing pin at the failing vector in red (for subroutine fails, a red `F` under each failing pin on the CALL row, since the subroutine's vectors aren't readable). Only pins that strobe or toggle in that window are shown; windows are stored once in `DATA.grids`.
