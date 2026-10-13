# shmoo_analyzer

Extracts shmoo results from ituff files (`.itf` / `.itf.gz`) and builds a **single-file interactive HTML shmoo viewer**, packaged as a GitHub Copilot skill for VS Code.

- Browse shmoos by **unit** and **test**; compare units side by side.
- **Hover** any point to see the result and the limiting pattern, plist, fail vector/cycle and failing pins; **click** to pin the details.
- Overview table: lowest fully-passing Y (e.g. Vmin) per test × unit.
- Optional flat per-point **CSV** export.
- Output HTML is fully self-contained (no internet / external libraries) and can be shared as one file.
- Pure Python standard library — nothing to `pip install`.

## Requirements

- Python 3.7+
- VS Code with GitHub Copilot Chat (agent mode) — only needed for the skill; the script also runs standalone.
- Read access to the ituff location (e.g. `\\amr.corp.intel.com\ec\proj\mdl\...\hdmxdata\prod\<lot>`), or local ituff copies.

## Clone

```powershell
git clone https://github.com/intel-sandbox/shmoo_analyzer.git
cd shmoo_analyzer
code .
```

## Use as a Copilot skill

The skill lives in [.github/skills/ituff-shmoo-viewer](.github/skills/ituff-shmoo-viewer/SKILL.md). VS Code picks it up automatically when this repo is open as the workspace.

In Copilot Chat (agent mode), either type the slash command:

```
/ituff-shmoo-viewer \\amr.corp.intel.com\ec\proj\mdl\cr\intel\hdmxdata\prod\<LOT_FOLDER>
```

or just ask in plain language, e.g. *"pull the shmoos from this ituff: \<path\>"*, *"only CFCMEM tests, and give me a CSV too"*.

The agent runs the script and writes output to `ituff_shmoo_viewer/<ituff_name>_shmoo.html` (and `.csv` if requested) in the workspace, then can open it in the integrated browser.

### Using the skill in another workspace

Copy the `.github/skills/ituff-shmoo-viewer/` folder into the other workspace's `.github/skills/`. To make it available in **every** workspace, copy it to `%USERPROFILE%\.copilot\skills\ituff-shmoo-viewer\` and update the script path in `SKILL.md` step 3 to that location.

## Use the script directly

```powershell
python .github\skills\ituff-shmoo-viewer\scripts\ituff_shmoo_html.py <ituff | folder | glob> [more ...] [-o out.html] [--csv out.csv] [--filter REGEX]
```

| Argument | Description |
|---|---|
| `inputs` | One or more `.itf` / `.itf.gz` files, lot folders, or globs. Multiple inputs are combined into one HTML. |
| `-o, --output` | Output HTML path. Default: `<first ituff name>_shmoo.html` in the current directory. |
| `--csv` | Also write a per-point CSV (unit, test, X, Y, result, pattern, plist, vector, cycle, pins). |
| `--filter` | Case-insensitive regex; keep only shmoo tests whose name matches. |

Examples:

```powershell
# Whole lot folder
python .github\skills\ituff-shmoo-viewer\scripts\ituff_shmoo_html.py "\\amr.corp.intel.com\ec\proj\mdl\cr\intel\hdmxdata\prod\J642051MV_6248"

# Specific tests, with CSV
python .github\skills\ituff-shmoo-viewer\scripts\ituff_shmoo_html.py lot.itf.gz --filter "CFCMEM|FIXDIGW" --csv lot_shmoo.csv -o lot_shmoo.html
```

## How the ituff is interpreted

| Record | Meaning |
|---|---|
| `0_tname_<test>_ShmooParams` / `0_strgval_<x>^<xs>^<xe>^<xstep>^<y>^<ys>^<ye>^<ystep>_ShmooHub` | Axis definitions |
| `0_tname_<test>_ShmooResults` / `0_strgval_<row0>_<row1>_...` | One row per Y step starting at Y start (plot bottom); one char per X step. `*` = pass |
| `0_tname_<test>^LEGEND^<C>` / `0_strgval_<pattern>\|<idx>:<plist>:<domain>(<vec>,<cyc>,..):<pins>` | Meaning of fail character `<C>` |
| `2_lbeg` … `2_lend` | One unit run; labelled `visual ID \| site \| start time`, bins from `3_curibin` / `3_curfbin` |
| `0_tssid_<die>` | Die the following tests ran on (shown as `[U1]` etc. in test names) |

A part retested in the same ituff (e.g. site swap) appears as separate units.

## Data handling

Generated `*_shmoo.html` / `*_shmoo.csv` files contain lot/unit test data and are git-ignored — don't commit them.
