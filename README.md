# Shmoo-Nav

Extracts shmoo results from ituff files (`.itf` / `.itf.gz`) and builds a **single-file interactive HTML shmoo viewer**, packaged as a GitHub Copilot skill for VS Code.

- Browse shmoos by **unit** and **test**; compare units side by side.
- **Hover** a failing point to see its pattern, plist, fail cycle/vector and pins; **click** to pin details.
- Optional **pattern tracing** looks up failing patterns in FLASH and resolves the fail location in `.hdmt.data`. The viewer shows the source line, nearest label, expected pin states, capture note and a colored vector grid; the failing strobe is highlighted.
- Look up a bare VPO across the hdmxdata site shares, or report the lot's test-program folder, TPL and STPL.
- Overview table: lowest fully-passing Y (e.g. Vmin) per test × unit.
- Optional flat per-point **CSV** export.
- Output HTML is fully self-contained (no external libraries or runtime network access) and can be shared as one file. Pattern tracing requires network access only while generating the viewer.
- Pure Python standard library — nothing to `pip install`.

## Requirements

- Python 3.10+
- VS Code with GitHub Copilot Chat (agent mode) — only needed for the skill; the script also runs standalone.
- Read access to the ituff location (e.g. `\\amr.corp.intel.com\ec\proj\mdl\...\hdmxdata\prod\<lot>`), or local ituff copies.
- For `--trace-patterns`: network access to FLASH and read access to the pattern's site samba share. The script calls FLASH directly; enabling FLASH as a VS Code MCP server is not required.

## Clone

```powershell
git clone https://github.com/intel-sandbox/shmoo_analyzer.git
cd shmoo_analyzer
code .
```

## Use as a Copilot skill

The skill lives in [.github/skills/shmoo-nav](.github/skills/shmoo-nav/SKILL.md). VS Code picks it up automatically when this repo is open as the workspace.

In Copilot Chat (agent mode), either type the slash command:

```
/shmoo-nav I642014CR
/shmoo-nav \\amr.corp.intel.com\ec\proj\mdl\cr\intel\hdmxdata\prod\<LOT_FOLDER>
```

or just ask in plain language, e.g. *"pull the shmoos for I642014CR"*, *"pull the shmoos from this ituff: \<path\>"*, *"only CFCMEM tests, and give me a CSV too"*.

The agent runs the script and writes output to `ituff_shmoo_viewer/<ituff_name>_shmoo.html` (and `.csv` if requested) in the workspace, then can open it in the integrated browser.

### Using the skill in another workspace

Copy the `.github/skills/shmoo-nav/` folder into the other workspace's `.github/skills/`. To make it available in **every** workspace, copy it to `%USERPROFILE%\.copilot\skills\shmoo-nav\` and update the script path in `SKILL.md` step 3 to that location.

## Use the script directly

```powershell
python .github\skills\shmoo-nav\scripts\shmoo_nav.py <VPO | ituff | folder | glob> [more ...] [-o out.html] [--csv out.csv] [--filter REGEX] [--sites sc,cr] [--test-program] [--trace-patterns] [--flash-prod DIO]
```

| Argument | Description |
|---|---|
| `inputs` | One or more VPO / lot IDs, `.itf` / `.itf.gz` files, lot folders, or globs. Multiple inputs are combined into one HTML. |
| `-o, --output` | Output HTML path. Default: `<first ituff name>_shmoo.html` in the current directory. |
| `--csv` | Also write a per-point CSV (unit, test, X, Y, result, pattern, plist, cycle, vector, pins); with tracing, includes source file/line, subroutine, label, expected states and capture note. |
| `--filter` | Case-insensitive regex; keep only shmoo tests whose name matches. |
| `--sites` | Sites to search for a VPO. Default: `sc,cr,an,fm,jf`. |
| `--test-program` | Only report where each lot's test program lives (folder, `BaseTestPlan.tpl`, flow `.stpl`); reads headers only, no HTML. |
| `--trace-patterns` | Look up failing patterns in FLASH and resolve failure locations in the converted `.hdmt.data`; includes the vector grid in the HTML. Optional and may take longer for large lots. |
| `--flash-prod` | FLASH product used with `--trace-patterns`. Default: `DIO`. |

A bare VPO (e.g. `I642014CR`) is looked up as `\\amr.corp.intel.com\ec\proj\mdl\<site>\intel\hdmxdata\{prod,engr}\<VPO>_*` across the sites (about 1 s), and every ituff chunk in the matching folder is used. The shares only keep roughly the last month of data.

Examples:

```powershell
# By VPO (searches all sites)
python .github\skills\shmoo-nav\scripts\shmoo_nav.py I642014CR

# Which test program did this lot run?
python .github\skills\shmoo-nav\scripts\shmoo_nav.py I642014CR --test-program

# Trace failing patterns and show their vector windows
python .github\skills\shmoo-nav\scripts\shmoo_nav.py I642014CR --trace-patterns

# Whole lot folder
python .github\skills\shmoo-nav\scripts\shmoo_nav.py "\\amr.corp.intel.com\ec\proj\mdl\cr\intel\hdmxdata\prod\J642051MV_6248"

# Specific tests, with CSV
python .github\skills\shmoo-nav\scripts\shmoo_nav.py lot.itf.gz --filter "CFCMEM|FIXDIGW" --csv lot_shmoo.csv -o lot_shmoo.html
```

## How the ituff is interpreted

| Record | Meaning |
|---|---|
| `0_tname_<test>_ShmooParams` / `0_strgval_<x>^<xs>^<xe>^<xstep>^<y>^<ys>^<ye>^<ystep>_ShmooHub` | Axis definitions |
| `0_tname_<test>_ShmooResults` / `0_strgval_<row0>_<row1>_...` | One row per Y step starting at Y start (plot bottom). Standard results use one symbol per X step; coordinate-keyed results concatenate decimal failure counts per X/Y point. `*` = pass |
| `0_tname_<test>^LEGEND^<C>` / `0_strgval_<pattern>\|<idx>:<plist>:<domain>(<cycle>,<vector>,..):<pins>` | Meaning of fail character `<C>`; cycle is zero-based |
| `0_tname_<test>^LEGEND^[<x>^<y>^<n>]` | Failure legend(s) associated with coordinate X/Y; multiple entries are shown together when that point is selected |
| `2_lbeg` … `2_lend` | One unit run; labelled `visual ID \| site \| start time`, bins from `3_curibin` / `3_curfbin` |
| `0_tssid_<die>` | Die the following tests ran on (shown as `[U1]` etc. in test names) |

A part retested in the same ituff (e.g. site swap) appears as separate units. Coordinate-keyed results concatenate decimal failure counts (for example, `10` occupies two characters but represents one point); these are validated against the coordinate legends to preserve the X/Y positions. Each failed point displays its failure count. Points whose failures all reference one pattern share that pattern's color across plots; points with multiple patterns are gray. The pattern legend lists point and failure totals, and clicking a point lists all its failures.

With `--trace-patterns`, a failure in the pattern is matched by both its vector address and cycle, then displayed with its `.hdmt.data` line, nearest label, expected pin values and capture note. The vector grid shows nearby vectors and highlights the failing strobe. If the failure is inside a called compiled subroutine, the grid marks the CALL row with an `F` under the failing pin and reports the nearest subroutine label; source vector lines are not available for compiled subroutines.

## Data handling

Generated `*_shmoo.html` / `*_shmoo.csv` files contain lot/unit test data and are git-ignored — don't commit them.
