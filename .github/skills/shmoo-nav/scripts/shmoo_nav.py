r"""Shmoo-Nav: extract shmoo results from ituff files and build an interactive HTML viewer.

Usage:
    python shmoo_nav.py <VPO | ituff[.gz] | directory | glob> [...] [-o out.html] [--csv out.csv] [--filter REGEX] [--sites sc,cr]

A bare VPO (e.g. I642014CR) is looked up as <HDMX_ROOT>\<site>\intel\hdmxdata\{prod,engr}\<VPO>_* across SITES.

Ituff shmoo records (per unit, per test):
    0_tname_<test>_ShmooParams      0_strgval_<x>^<xs>^<xe>^<xstep>^<y>^<ys>^<ye>^<ystep>_ShmooHub
    0_tname_<test>_ShmooResults     0_strgval_<row0>_<row1>_...   (row = Y step from Y start, char = X step)
    0_tname_<test>^LEGEND^<C>       0_strgval_<pattern>|<idx>:<plist>:<domain>(<cyc>,<vec>,..):<pins>
'*' = pass; any other character = failure described by the matching LEGEND entry.

With --trace-patterns each failing pattern is looked up in FLASH, its converted .hdmt.data is read over samba,
and the failing vector address is resolved to its file line, opcode, expected pin states and nearest label.
"""

from __future__ import annotations

import argparse
import bz2
import csv
import glob
import gzip
import html
import json
import os
import re
import struct
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HDMX_ROOT = r"\\amr.corp.intel.com\ec\proj\mdl"
SITES = ("sc", "cr", "an", "fm", "jf")
AREAS = ("prod", "engr")
VPO_RE = re.compile(r"^[A-Z]\d{6}[A-Z0-9]*$", re.I)
ITUFF_RE = re.compile(r"\.(itf|ituff)(\.gz)?$", re.I)

PRE_UNIT_KEYS = {"prtnm", "eqpprtid", "siteid", "socket", "dvtststdt", "tiuid", "prttesterid"}
POST_UNIT_KEYS = {"curfbin", "curibin", "binn", "trslt", "dvtsteddt"}
HEADER_KEYS = {"lotid", "prgnm", "prdct", "sysid", "tempr", "processstep", "facid", "flstpid", "lcode"}
TP_ATTRS = {"tplpath", "stplpath", "socpath", "envpath", "tpbasepath"}
LEGEND_RE = re.compile(r"^(?P<pat>[^|]*)\|(?P<idx>\d+):(?P<rest>.*)$")
LOC_RE = re.compile(r":(?P<dom>[A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)*)\((?P<loc>[^)]*)\):(?P<pins>.*)$")
COORD_LEGEND_RE = re.compile(r"^\[(?P<x>[^\\^]+)\^(?P<y>[^\\^]+)\^(?P<index>\d+)\]$")

FLASH_URL = "https://mdcx-tvpv-flash.intel.com/mcp"
FLASH_PATH_PREFIX = "/p/pde/tvpv/"
LABEL_RE = re.compile(r"^([A-Za-z_]\w*):\s*$")


def open_ituff(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", errors="replace")
    return open(path, "r", errors="replace")


def parse_legend(raw: str) -> dict:
    out = {"raw": raw, "pattern": raw, "plist": "", "domain": "", "vector": "", "cycle": "", "pins": "", "suffix": ""}
    m = LEGEND_RE.match(raw)
    if not m:
        return out
    out["pattern"] = m["pat"]
    rest = ":" + m["rest"]
    lm = LOC_RE.search(rest)
    if lm:
        out["plist"] = rest[1:lm.start()]
        out["domain"] = lm["dom"]
        loc = lm["loc"].split(",")
        out["cycle"] = loc[0] if loc else ""
        out["vector"] = loc[1] if len(loc) > 1 else ""
        out["pins"], _, out["suffix"] = lm["pins"].partition("|")
    else:
        out["plist"] = m["rest"]
    return out


def parse_legend_coordinate(key: str) -> tuple[float, float, int] | None:
    match = COORD_LEGEND_RE.fullmatch(key)
    if not match:
        return None
    try:
        return float(match["x"]), float(match["y"]), int(match["index"])
    except ValueError:
        return None


def axis_index(axis: list[float], value: float) -> int | None:
    if not axis:
        return None
    index = min(range(len(axis)), key=lambda i: abs(axis[i] - value))
    steps = [abs(axis[i + 1] - axis[i]) for i in range(len(axis) - 1)]
    steps = [step for step in steps if step > 0]
    tolerance = max(min(steps) * 1e-6 if steps else 0, abs(value) * 1e-12, 1e-15)
    return index if abs(axis[index] - value) <= tolerance else None


def map_coordinate_legends(legend: dict, x: list[float], y: list[float]) -> dict:
    cells: dict[str, list[dict]] = {}
    unmatched = []
    for key, entry in legend.items():
        coord = parse_legend_coordinate(key)
        if coord is None:
            continue
        xv, yv, index = coord
        xi, yi = axis_index(x, xv), axis_index(y, yv)
        if xi is None or yi is None:
            unmatched.append(key)
            continue
        item = entry
        item.update(key=key, index=index)
        cells.setdefault(f"{yi},{xi}", []).append(item)
    if unmatched:
        raise ValueError(f"{len(unmatched)} coordinate legend(s) do not match the shmoo axes: {unmatched[0]}")
    for entries in cells.values():
        entries.sort(key=lambda item: item["index"])
    return cells


def axis_values(start: float, stop: float, step: float) -> list[float]:
    if step == 0 or start == stop:
        return [start]
    n = int(round((stop - start) / step)) + 1
    return [round(start + i * step, 15) for i in range(max(n, 1))]


def parse_params(raw: str) -> dict | None:
    raw = raw[: -len("_ShmooHub")] if raw.endswith("_ShmooHub") else raw
    f = raw.split("^")
    if len(f) != 8:
        return None
    try:
        xs, xe, xst, ys, ye, yst = (float(v) for v in (f[1], f[2], f[3], f[5], f[6], f[7]))
    except ValueError:
        return None
    return {
        "xname": f[0], "yname": f[4],
        "x": axis_values(xs, xe, xst), "y": axis_values(ys, ye, yst),
    }


def build_grid(params: dict | None, results: str, coordinate_legends: dict | None = None) -> tuple[list[float], list[float], list[list[str]], str, str]:
    """Return an X/Y grid, decoding coordinate-keyed decimal failure counts."""
    rows = results.split("_")
    if coordinate_legends is not None and params is None:
        raise ValueError("coordinate legends require ShmooParams axes")
    if params:
        x, y = params["x"], params["y"]
        if coordinate_legends is not None:
            if not x or len(rows) != len(y):
                raise ValueError("coordinate legends require matching ShmooParams axes and result rows")
            legends = map_coordinate_legends(coordinate_legends, x, y)
            cells = [
                [str(len(legends[f"{i},{j}"])) if f"{i},{j}" in legends else "*" for j in range(len(x))]
                for i in range(len(y))
            ]
            for i, row in enumerate(rows):
                expected = "".join(cells[i])
                if row != expected:
                    raise ValueError(f"result row {i} does not match coordinate legend counts: {row!r} != {expected!r}")
            return x, y, cells, params["xname"], params["yname"]
        if len(rows) == len(y) and all(len(r) == len(x) for r in rows):
            return x, y, [list(row) for row in rows], params["xname"], params["yname"]
        if len(rows) == len(x) and all(len(r) == len(y) for r in rows):
            return x, y, [[rows[j][i] for j in range(len(x))] for i in range(len(y))], params["xname"], params["yname"]
    width = max((len(r) for r in rows), default=0)
    rows = [r.ljust(width, "?") for r in rows]
    return list(range(width)), list(range(len(rows))), [list(row) for row in rows], "X index", "Y index"


def is_pass_cell(cell: str) -> bool:
    return bool(cell) and all(symbol == "*" for symbol in cell)


def summarize(y: list[float], rows: list[list[str]]) -> dict:
    """Per X column, lowest Y from which every higher Y point passes (None if top fails)."""
    ncol = len(rows[0]) if rows else 0
    col_min = []
    for j in range(ncol):
        best = None
        for i in range(len(rows) - 1, -1, -1):
            if not is_pass_cell(rows[i][j]):
                break
            best = y[i]
        col_min.append(best)
    fails = sum(not is_pass_cell(cell) for row in rows for cell in row)
    passes = [v for v in col_min if v is not None]
    return {"col_min": col_min, "min_pass": min(passes) if passes else None,
            "max_pass": max(passes) if passes else None, "fails": fails,
            "total": sum(len(row) for row in rows)}


def parse_ituff(path: Path, name_filter: re.Pattern | None) -> dict:
    header: dict[str, str] = {}
    units: list[dict] = []
    pending: dict[str, str] = {}
    unit: dict | None = None
    last_unit: dict | None = None
    post = False
    tname = None
    tssid = ""

    with open_ituff(path) as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            lvl, _, body = line.partition("_")
            key, _, val = body.partition("_")

            if unit is None:
                if lvl == "3":
                    if key in ("lsep", "lend", "lbeg"):
                        post = False
                    elif post and last_unit is not None and key in POST_UNIT_KEYS:
                        last_unit["info"][key] = val
                    elif key in PRE_UNIT_KEYS:
                        pending[key] = val
                elif lvl in ("4", "5", "6") and key in HEADER_KEYS and key not in header:
                    header[key] = val
                elif lvl == "4" and key == "tsattrs":
                    name, _, v = val.partition(",")
                    if name in TP_ATTRS and name not in header:
                        header[name] = v
                elif line == "2_lbeg":
                    unit = {"info": dict(pending), "shmoos": {}, "order": []}
                    pending = {}
                    tssid = ""
                continue

            if line == "2_lend":
                units.append(unit)
                last_unit, unit, post, tname = unit, None, True, None
                continue
            if key == "visualid":
                unit["info"].setdefault("visualid", val)
                continue
            if key == "tssid":
                tssid = val
                continue
            if key == "tname":
                tname = val
                continue
            if key != "strgval" or tname is None:
                continue

            name, value, tname = tname, val, None
            if "^LEGEND^" in name:
                base, _, letter = name.partition("^LEGEND^")
                kind = "legend"
            elif name.endswith("_ShmooParams"):
                base, kind = name[: -len("_ShmooParams")], "params"
            elif name.endswith("_ShmooResults"):
                base, kind = name[: -len("_ShmooResults")], "results"
            else:
                continue
            if name_filter and not name_filter.search(base):
                continue
            k = (base, tssid)
            rec = unit["shmoos"].get(k)
            if rec is None:
                rec = unit["shmoos"][k] = {"test": base, "die": tssid, "params": None, "results": None, "legend": {}}
                unit["order"].append(k)
            if kind == "legend":
                rec["legend"][letter] = parse_legend(value)
            elif kind == "params":
                rec["params_raw"] = value
                rec["params"] = parse_params(value)
            else:
                rec["results"] = value

    if unit is not None:
        units.append(unit)
    return {"header": header, "units": units}


def fmt_unit_label(info: dict, idx: int) -> str:
    vid = info.get("visualid") or info.get("eqpprtid") or f"unit{idx}"
    parts = [vid]
    if info.get("siteid"):
        parts.append(info["siteid"])
    if info.get("dvtststdt"):
        try:
            parts.append(datetime.strptime(info["dvtststdt"], "%Y%m%d%H%M%S").strftime("%m/%d %H:%M"))
        except ValueError:
            parts.append(info["dvtststdt"])
    return " | ".join(parts)


def read_tp_header(path: Path) -> dict:
    """Program name and test-program tsattrs from the ituff header (stops at the first unit)."""
    header: dict[str, str] = {}
    with open_ituff(path) as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if line == "2_lbeg":
                break
            if line.startswith("6_prgnm_"):
                header.setdefault("prgnm", line[8:])
            elif line.startswith("4_tsattrs_"):
                name, _, v = line[10:].partition(",")
                if name in TP_ATTRS:
                    header.setdefault(name, v)
    return header


def resolve_tester_path(raw: str, ituff_path: Path) -> Path | None:
    """Map a tester path to a reachable share path.

    The tester's I: is the site share's ...\\intel folder, i.e. whatever precedes \\hdmxdata\\ in the ituff's
    own path; other sites are tried as a fallback. Tester-local drives (e.g. D:\\TPRecipes) can't be resolved.
    """
    if not raw or raw[:2].upper() != "I:":
        return None
    rel = raw[2:].lstrip("\\/")
    roots: list[Path] = []
    m = re.match(r"(?i)(.*?)[\\/]hdmxdata[\\/]", str(ituff_path))
    if m:
        roots.append(Path(m[1] + "\\"))
    roots += [Path(HDMX_ROOT) / s / "intel" for s in SITES]
    for root in roots:
        cand = root / rel
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    return None


def trace_test_program(header: dict, ituff_path: Path) -> dict:
    tpl = resolve_tester_path(header.get("tplpath", ""), ituff_path)
    stpl = resolve_tester_path(header.get("stplpath", ""), ituff_path)
    return {
        "program": header.get("prgnm", ""),
        "tplpath": header.get("tplpath", ""),
        "stplpath": header.get("stplpath", ""),
        "tp_dir": str(tpl.parent) if tpl else "",
        "tpl": str(tpl) if tpl else "",
        "stpl": str(stpl) if stpl else "",
        "tester_local": {k: header[k] for k in ("socpath", "envpath") if header.get(k)},
    }


def print_test_programs(paths: list[Path], headers: list[dict]) -> None:
    seen: set[str] = set()
    for p, h in zip(paths, headers):
        key = h.get("tplpath", "") or h.get("prgnm", "")
        if key in seen:
            continue
        seen.add(key)
        tp = trace_test_program(h, p)
        print(f"Test program {tp['program'] or '?'} ({p.name}):")
        if tp["tp_dir"]:
            print(f"  folder : {tp['tp_dir']}")
            print(f"  tpl    : {tp['tpl']}")
            if tp["stpl"]:
                print(f"  stpl   : {tp['stpl']}")
        elif tp["tplpath"]:
            print(f"  not found on any site share; tester path was {tp['tplpath']}")
        else:
            print("  no tplpath recorded in this ituff")


def flash_call(tool: str, arguments: dict) -> dict:
    """Call a FLASH MCP tool over streamable HTTP and return its structuredContent."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # intranet host: bypass proxy
    session: str | None = None

    def rpc(method: str, params: dict, rid: int | None = None) -> dict | None:
        nonlocal session
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if rid is not None:
            body["id"] = rid
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(FLASH_URL, data=json.dumps(body).encode(), method="POST", headers=headers)
        with opener.open(req, timeout=180) as r:
            session = r.headers.get("Mcp-Session-Id") or session
            raw = r.read().decode("utf-8", "replace")
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        return json.loads(raw) if raw.strip() else None

    rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "shmoo-nav", "version": "1"}}, 1)
    rpc("notifications/initialized", {})
    res = rpc("tools/call", {"name": tool, "arguments": arguments}, 2) or {}
    result = res.get("result") or {}
    if res.get("error") or result.get("isError"):
        msg = res.get("error") or " ".join(c.get("text", "") for c in result.get("content", []))
        raise RuntimeError(f"FLASH {tool} failed: {msg}")
    return result.get("structuredContent") or {}


def flash_lookup_patterns(names: set[str], prod: str) -> dict[str, dict]:
    """FLASH pattern records by name; the most recent valid conversion wins."""
    out: dict[str, dict] = {}
    names_l = sorted(names)
    for i in range(0, len(names_l), 50):
        res = flash_call("flash_search", {"prod": prod, "cmd": "pat", "query_mode": "name",
                                          "query": names_l[i:i + 50], "verbose": True, "limit": 1000})
        for r in res.get("result", []):
            if not r.get("valid", True):
                continue
            cur = out.get(r["name"])
            if cur is None or str(r.get("requested_date", "")) > str(cur.get("requested_date", "")):
                out[r["name"]] = r
    return out


_disk_hits: dict[str, str] = {}


def resolve_flash_dir(unix_file: str, site: str) -> Path | None:
    """Map FLASH's /p/pde/tvpv/<rel> to the samba disk that holds it: \\\\samba.<site>.intel.com\\nfs\\site\\disks\\mfg_tvpv_*\\<rel>."""
    if not unix_file.startswith(FLASH_PATH_PREFIX) or not site:
        return None
    rel_dir = unix_file[len(FLASH_PATH_PREFIX):].rsplit("/", 1)[0].replace("/", "\\")
    root = Path(rf"\\samba.{site.lower()}.intel.com\nfs\site\disks")
    if site in _disk_hits:
        cand = root / _disk_hits[site] / rel_dir
        if cand.is_dir():
            return cand
    try:
        disks = _list_dirs_matching(root, "mfg_tvpv_")
    except OSError:
        return None

    def probe(disk: str) -> Path | None:
        try:
            return root / disk / rel_dir if (root / disk / rel_dir).is_dir() else None
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=32) as ex:
        for disk, hit in zip(disks, ex.map(probe, disks)):
            if hit:
                _disk_hits[site] = disk
                return hit
    return None


def read_pattern_data(folder: Path, name: str, domain: str) -> dict | None:
    """Parse <name>_<domain>.hdmt.data[.bz2]: one vector-memory address per vector or instruction line.

    Vector lines end with comment fields: # t_start # t_end # address # cycle # tap_state # note
    """
    path = next((p for p in (folder / f"{name}_{domain}.hdmt.data.bz2", folder / f"{name}_{domain}.hdmt.data")
                 if p.exists()), None)
    if path is None:
        return None
    raw = path.read_bytes()
    lines = (bz2.decompress(raw) if path.suffix == ".bz2" else raw).decode("utf-8", "replace").splitlines()

    header = [l for l in lines if l.startswith("#")]
    entries: list[dict] = []  # ln, op, st (parallel pin states or None), cyc, state, note, kind
    labels: list[tuple[int, str, int]] = []  # (address, label, file line)
    pins: list[str] = []
    for ln, l in enumerate(lines, 1):
        s = l.strip()
        if not s or s.startswith("#"):
            continue
        lm = LABEL_RE.match(s)
        if lm:
            labels.append((len(entries), lm[1], ln))
            continue
        m = re.search(r"\{\s*(\w+)\s*\{\s*\w+=([^\s;}]+)", l)
        if not m:
            entries.append({"ln": ln, "op": s, "st": None, "cyc": None, "state": "", "note": "", "kind": ""})
            continue
        op = l[:m.start()].strip()
        kind = m[1]
        if not pins and not op and kind != "S":
            # Pin names are written vertically in the header comments, aligned to unprefixed state columns.
            pins = ["".join(h[c] if c < len(h) else " " for h in header).strip()
                    for c in range(m.start(2), m.end(2))]
        f = [x.strip() for x in l.split("#")[1:]]
        entries.append({"ln": ln, "op": op, "st": m[2] if kind != "S" else None,
                        "cyc": int(f[3]) if len(f) > 3 and f[3].isdigit() else None,
                        "state": f[4] if len(f) > 4 else "", "note": f[5] if len(f) > 5 else "", "kind": kind})
    return {"file": str(path), "pins": pins, "entries": entries, "labels": labels}


def read_pinobj_labels(path: Path, domains: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Label tables of a compiled .pinObj, per domain.

    Layout: plain header naming the domains in order, then per domain a zlib label block
    (u32 count, then <name>\\n<u64 address> each) followed by vector data blocks.
    """
    raw = path.read_bytes()
    starts = [m.start() for m in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", raw)]
    if not starts:
        return {}
    hdr = raw[:starts[0]].decode("latin1")
    tables: list[list[tuple[int, str]]] = []
    pos = starts[0]
    while pos < len(raw):
        d = zlib.decompressobj()
        try:
            blk = d.decompress(raw[pos:])
        except zlib.error:
            nxt = [s for s in starts if s > pos]
            if not nxt:
                break
            pos = nxt[0]
            continue
        pos = len(raw) - len(d.unused_data)
        try:
            n, p, tab = struct.unpack_from("<I", blk, 0)[0], 4, []
            for _ in range(n):
                e = blk.index(b"\n", p, p + 4096)
                tab.append((struct.unpack_from("<Q", blk, e + 1)[0], blk[p:e].decode("ascii")))
                p = e + 9
            if n and p == len(blk) and all(t[1].isidentifier() for t in tab):
                tables.append(sorted(tab))
        except (struct.error, ValueError, UnicodeDecodeError):
            pass
    order = sorted((hdr.find(d), d) for d in domains if d in hdr)
    return dict(zip((d for _, d in order), tables))


def _compares(pd: dict, states: str | None) -> list[str]:
    if not states:
        return []
    return [f"{pd['pins'][i] if i < len(pd['pins']) else i}={c}" for i, c in enumerate(states) if c in "HL"]


def _context(pd: dict, center: int, before: int = 6, after: int = 4) -> list[str]:
    out = []
    for a in range(max(0, center - before), min(len(pd["entries"]), center + after)):
        e = pd["entries"][a]
        for la, lname, lln in pd["labels"]:
            if la == a:
                out.append(f"                    L{lln:<6} {lname}:")
        body = " ".join(_compares(pd, e["st"])) or ("serial" if e["kind"] == "S" else "-" if e["kind"] else "")
        extra = " ".join(x for x in (e["state"], e["note"]) if x)
        cyc = "" if e["cyc"] is None else e["cyc"]
        out.append(f"{'>>' if a == center else '  '} {a:6} {cyc:>8}  L{e['ln']:<6} {e['op'][:30]:<30} {body[:100]}"
                   + (f"   [{extra}]" if extra else ""))
    return out


def _find_call(pd: dict, cycle: int) -> tuple[int, int] | None:
    """(CALL entry index, cycles into the call) if the 1-based file cycle falls between a CALL's surrounding vectors."""
    prev: dict | None = None
    call: int | None = None
    for i, e in enumerate(pd["entries"]):
        if e["cyc"] is None:
            if e["op"].startswith("CALL"):
                call = i
            continue
        if call is not None and prev is not None and prev["cyc"] < cycle < e["cyc"]:
            return call, cycle - prev["cyc"]
        prev, call = e, None
    return None


def _grid(pd: dict, center: int, fail_pins: set[str], before: int = 14, after: int = 8) -> dict:
    """Vector window around `center` for the viewer: only pins that compare or toggle in the window, plus fail pins."""
    ents = pd["entries"]
    lo, hi = max(0, center - before), min(len(ents), center + after + 1)
    rows = ents[lo:hi]
    width = max((len(e["st"]) for e in rows if e["st"]), default=0)
    names = [pd["pins"][i] if i < len(pd["pins"]) else str(i) for i in range(width)]
    keep = []
    for i in range(width):
        col = {e["st"][i] for e in rows if e["st"] and i < len(e["st"])}
        if col & set("HL") or len(col - {"X"}) > 1 or names[i].upper() in fail_pins:
            keep.append(i)
    labels = {a: name for a, name, _ in pd["labels"]}
    return {"pins": [names[i] for i in keep], "center": center,
            "rows": [[a, e["cyc"], e["ln"], e["op"],
                      "".join(e["st"][i] if i < len(e["st"]) else " " for i in keep) if e["st"] else None,
                      e["note"], labels.get(a, "")] for a, e in zip(range(lo, hi), rows)]}


def locate_vector(pd: dict, addr: int, cycle: int | None, fail_pins: list[str], sub_labels) -> dict:
    """Resolve a LEGEND fail to the pattern's vector, or to a label inside a CALLed subroutine.

    LEGEND cycles are 0-based, the data file's cycle comments 1-based. The vector at `addr` is only
    accepted if its cycle matches; otherwise the cycle is looked up inside CALLs and `addr` read as a
    subroutine address (sub_labels: callable returning [(address, label)] for this domain).
    """
    ents = pd["entries"]
    fc = None if cycle is None else cycle + 1
    want = {p.split("::")[-1].upper() for p in fail_pins}
    e = ents[addr] if 0 <= addr < len(ents) else None
    if e and e["kind"] and (fc is None or e["cyc"] is None or e["cyc"] == fc):
        lab = next((x for x in reversed(pd["labels"]) if x[0] <= addr), None)
        comps = _compares(pd, e["st"])
        return {
            "addr": addr, "line": e["ln"], "op": e["op"], "sub": "",
            "label": lab[1] if lab else "", "label_offset": addr - lab[0] if lab else addr,
            "expect": " ".join(c for c in comps if c.split("=")[0].upper() in want) or " ".join(comps),
            "state": e["state"], "note": e["note"], "context": _context(pd, addr),
            "fail_pins": sorted(want), "grid": _grid(pd, addr, want),
        }
    hit = _find_call(pd, fc) if fc else None
    if hit:
        ci, into = hit
        op = ents[ci]["op"]
        target = op.split(None, 1)[1].strip() if " " in op else op
        labs = sub_labels() or []
        lab = next((x for x in reversed(labs) if x[0] <= addr), None)
        return {
            "addr": addr, "line": ents[ci]["ln"], "op": op, "sub": target,
            "label": lab[1] if lab else "", "label_offset": addr - lab[0] if lab else addr,
            "expect": "", "state": "",
            "note": f"fail is {into} cycles into the CALL; vector {addr} is a subroutine address"
                    + ("" if labs else " (subroutine labels unavailable)"),
            "context": _context(pd, ci),
            "fail_pins": sorted(want), "grid": _grid(pd, ci, want),
        }
    where = f"vector {addr}" + (f" / cycle {cycle}" if cycle is not None else "")
    return {"error": f"{where} not found in the pattern's {len(ents)} addresses or inside any CALL"}


def trace_patterns(data: dict, prod: str) -> None:
    """Attach a 'trace' dict to every failing LEGEND entry that has a vector address."""
    legs = [lg for u in data["units"] for s in u["shmoos"].values() for lg in s["legend"].values()
            if lg.get("pattern") and lg.get("vector", "").isdigit()]
    names = {lg["pattern"] for lg in legs}
    if not names:
        return
    print(f"Tracing {len(names)} failing pattern(s) through FLASH ({prod}) ...")
    try:
        recs = flash_lookup_patterns(names, prod)
    except (OSError, RuntimeError, ValueError) as e:
        print(f"  FLASH lookup failed: {e}")
        return
    cache: dict[tuple[str, str], dict | str] = {}
    sub_cache: dict[Path, dict] = {}

    def sub_labels_for(folder: Path, rec: dict, dom: str) -> list[tuple[int, str]]:
        # The pattern folder carries a copy of its common subroutine .pinObj (FLASH 'sub').
        name = (rec.get("sub") or "").rsplit("/", 1)[-1]
        p = folder / name if name else None
        if p is None or not p.exists():
            return []
        if p not in sub_cache:
            try:
                sub_cache[p] = read_pinobj_labels(p, list(rec.get("vecmem") or {}))
            except OSError:
                sub_cache[p] = {}
        return sub_cache[p].get(dom, [])

    for lg in legs:
        rec = recs.get(lg["pattern"])
        if not rec:
            lg["trace"] = {"error": f"pattern not found in FLASH ({prod})"}
            continue
        dom = lg["domain"].split("::")[-1]
        key = (lg["pattern"], dom)
        if key not in cache:
            folder = resolve_flash_dir(rec.get("path", ""), rec.get("site", ""))
            pd = read_pattern_data(folder, lg["pattern"], dom) if folder else None
            cache[key] = pd or (f"no {dom} data file in {folder}" if folder
                                else f"cannot reach {rec.get('path', '')} (site {rec.get('site', '?')}) over samba")
        pd = cache[key]
        base = {"flash_path": rec.get("path", ""), "tid": rec.get("tid", ""), "lineitem": rec.get("lineitem", "")}
        if isinstance(pd, str):
            lg["trace"] = {**base, "error": pd}
            continue
        fail_pins = [p for p in re.split(r"[,;\s]+", lg.get("pins", "").split("|")[0]) if p]
        cyc = int(lg["cycle"]) if lg.get("cycle", "").isdigit() else None
        pfolder = Path(pd["file"]).parent
        t = locate_vector(pd, int(lg["vector"]), cyc, fail_pins, lambda: sub_labels_for(pfolder, rec, dom))
        if "grid" in t:
            # Many fails share a window; store each once and reference it by key.
            gkey = f"{pd['file']}#{t['grid']['center']}"
            data.setdefault("grids", {})[gkey] = t["grid"]
            t["grid"] = gkey
        lg["trace"] = {**base, "file": pd["file"], **t}

    seen = set()
    for lg in legs:
        t = lg.get("trace", {})
        k = (lg["pattern"], lg["domain"], lg["vector"], lg["cycle"])
        if k in seen:
            continue
        seen.add(k)
        print(f"  {lg['pattern']}  {lg['domain']} vec {lg['vector']} cyc {lg['cycle']}:")
        if t.get("error"):
            print(f"    {t['error']}")
        elif t.get("sub"):
            print(f"    line {t['line']} {t['op']}  ->  {t['label']}+{t['label_offset']} inside the subroutine")
        else:
            extra = " ".join(x for x in (t.get("state"), t.get("note")) if x)
            print(f"    line {t['line']} {t['op'] or 'V'}  {t['label']}+{t['label_offset']}  expects {t['expect'] or '-'}"
                  + (f"  [{extra}]" if extra else ""))


def collect(paths: list[Path], name_filter: re.Pattern | None) -> dict:
    files, units, tests = [], [], {}
    for fi, p in enumerate(paths):
        print(f"Parsing {p} ...")
        parsed = parse_ituff(p, name_filter)
        files.append({"name": p.name, "path": str(p), "header": parsed["header"],
                      "tp": trace_test_program(parsed["header"], p)})
        for u in parsed["units"]:
            if not u["order"]:
                continue
            uid = len(units)
            info = u["info"]
            shmoos = {}
            for k in u["order"]:
                rec = u["shmoos"][k]
                if rec["results"] is None:
                    continue
                coordinate_legends = any(parse_legend_coordinate(key) for key in rec["legend"])
                x, y, rows, xname, yname = build_grid(
                    rec["params"], rec["results"], rec["legend"] if coordinate_legends else None)
                tkey = rec["test"] + (f" [{rec['die']}]" if rec["die"] else "")
                tests.setdefault(tkey, {"test": rec["test"], "die": rec["die"]})
                shmoos[tkey] = {
                    "x": x, "y": y, "rows": rows, "xname": xname, "yname": yname,
                    "legend": rec["legend"], "params_raw": rec.get("params_raw", ""),
                    "legend_mode": "coordinate" if coordinate_legends else "character",
                    "cell_legend": map_coordinate_legends(rec["legend"], x, y) if coordinate_legends else {},
                    "summary": summarize(y, rows),
                }
            units.append({
                "id": uid, "file": fi, "label": fmt_unit_label(info, uid),
                "info": info, "shmoos": shmoos,
            })
    return {"files": files, "units": units, "tests": list(tests.keys()),
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def write_csv(data: dict, out: Path) -> None:
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "visualid", "site", "unit_label", "test", "x_name", "x", "y_name", "y",
                    "result", "legend_count", "legend_keys", "pattern", "plist", "vector", "cycle", "pins", "legend_suffix",
                    "src_file", "src_line", "src_subroutine", "src_label", "src_label_offset", "src_expect", "src_note"])
        for u in data["units"]:
            fname = data["files"][u["file"]]["name"]
            for tkey, s in u["shmoos"].items():
                for i, yv in enumerate(s["y"]):
                    for j, xv in enumerate(s["x"]):
                        c = s["rows"][i][j]
                        if s["legend_mode"] == "coordinate":
                            legends = s["cell_legend"].get(f"{i},{j}", [])
                        else:
                            lg = s["legend"].get(c) if not is_pass_cell(c) else None
                            legends = [lg] if lg else []
                        traces = [lg.get("trace") or {} for lg in legends]
                        join = lambda key: " | ".join(str(lg[key]) for lg in legends if lg.get(key) not in (None, ""))
                        join_trace = lambda key: " | ".join(str(t[key]) for t in traces if t.get(key) not in (None, ""))
                        w.writerow([fname, u["info"].get("visualid", ""), u["info"].get("siteid", ""), u["label"],
                                    tkey, s["xname"], xv, s["yname"], yv, "PASS" if is_pass_cell(c) else c,
                                    len(legends), join("key") or (c if legends else ""),
                                    join("pattern"), join("plist"), join("vector"),
                                    join("cycle"), join("pins"), join("suffix"),
                                    join_trace("file"), join_trace("line"), join_trace("sub"), join_trace("label"),
                                    join_trace("label_offset"), join_trace("expect"),
                                    " | ".join(" ".join(str(t[key]) for key in ("state", "note", "error") if t.get(key))
                                               for t in traces if any(t.get(key) for key in ("state", "note", "error")))])


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--bd:#d8dce3;--txt:#1d2330;--mut:#6b7385;--acc:#0068b5;--pass:#3fb950}
*{box-sizing:border-box}
body{margin:0;font:13px/1.4 Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--txt);display:flex;flex-direction:column;height:100vh}
header{padding:8px 14px;background:#0b2a4a;color:#fff;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header .brand{display:flex;align-items:center;gap:10px;flex:none}
header .logo{width:48px;height:48px;flex:none;display:block}
header h1{font-size:16px;margin:0}
header .meta{color:#c5d3e6;font-size:12px;flex:1 1 320px;min-width:0;overflow-wrap:anywhere}
main{flex:1;display:flex;min-height:0}
aside{width:380px;min-width:260px;background:var(--panel);border-right:1px solid var(--bd);display:flex;flex-direction:column;min-height:0;resize:horizontal;overflow:auto}
aside h3{margin:10px 10px 4px;font-size:12px;text-transform:uppercase;color:var(--mut);letter-spacing:.04em;display:flex;justify-content:space-between}
aside h3 a{text-transform:none;color:var(--acc);cursor:pointer;font-weight:normal}
#units{padding:0 10px;max-height:30vh;overflow:auto}
#units label{display:block;padding:2px 0;cursor:pointer;white-space:nowrap}
#units .bin{color:var(--mut);font-size:11px}
#filter{margin:4px 10px;padding:5px 7px;border:1px solid var(--bd);border-radius:4px}
#tests{flex:1;overflow:auto;padding:0 4px 10px}
.grp{font-weight:600;color:var(--mut);padding:6px 6px 2px;font-size:11px;word-break:break-all}
.ti{padding:3px 6px 3px 12px;cursor:pointer;border-radius:3px;word-break:break-all;display:flex;justify-content:space-between;gap:6px}
.ti:hover{background:#eef3fa}.ti.sel{background:var(--acc);color:#fff}
.ti .badge{font-size:10px;padding:0 5px;border-radius:8px;background:#e6e9ef;color:#333;flex:none;align-self:center}
.ti .badge.f{background:#ffd7d5}
section{flex:1;overflow:auto;padding:12px 16px;min-width:0}
.tabs{display:flex;gap:4px;margin-bottom:10px}
.tabs button{border:1px solid var(--bd);background:var(--panel);padding:5px 12px;border-radius:4px;cursor:pointer}
.tabs button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
h2{font-size:14px;margin:0 0 8px;word-break:break-all}
.cards{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}
.card{background:var(--panel);border:1px solid var(--bd);border-radius:6px;padding:10px}
.card h4{margin:0 0 6px;font-size:12px}
.card .sub{color:var(--mut);font-size:11px;margin-bottom:6px}
table.sh{border-collapse:collapse;font:11px Consolas,monospace}
table.sh td{width:30px;height:16px;text-align:center;border:1px solid #fff;cursor:pointer;padding:0}
table.sh td.p{background:var(--pass)}
table.sh td.n{background:#ccc}
table.sh td:hover{outline:2px solid #000;outline-offset:-2px}
table.sh td.pin{outline:2px solid #000;outline-offset:-2px}
table.sh th{font-weight:normal;color:var(--mut);padding:0 4px;white-space:nowrap}
table.sh th.yl{text-align:right}
table.sh .ax{color:var(--txt);font-weight:600}
table.lg{border-collapse:collapse;margin-top:8px;font-size:11px;max-width:900px}
table.lg td,table.lg th{border:1px solid var(--bd);padding:2px 6px;text-align:left;vertical-align:top;word-break:break-all}
table.lg th{background:#f0f2f5}
.sw{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:middle;margin-right:4px}
#tip{position:fixed;pointer-events:none;background:#111c;color:#fff;padding:6px 8px;border-radius:4px;font-size:11px;max-width:620px;z-index:10;display:none;word-break:break-all}
#tip b{color:#ffd166}
#detail{margin-top:14px;background:var(--panel);border:1px solid var(--bd);border-radius:6px;padding:10px;font-size:12px;display:none}
#detail pre{white-space:pre-wrap;word-break:break-all;margin:6px 0 0;font:11px Consolas,monospace;background:#f4f5f7;padding:6px;border-radius:4px}
#detail pre.ctx{white-space:pre;overflow-x:auto;word-break:normal}
.wvscroll{overflow:auto;max-height:520px;border:1px solid var(--bd);border-radius:4px;margin-top:6px}
table.wv{border-collapse:collapse;font:10px Consolas,monospace}
table.wv th,table.wv td{border:1px solid #e3e6eb;padding:0 3px;white-space:nowrap}
table.wv thead th{position:sticky;top:0;background:#fff;z-index:1}
table.wv th.pn{writing-mode:vertical-rl;transform:rotate(180deg);padding:4px 0;font-weight:normal;min-width:13px}
table.wv th.pn.fp{color:#d1242f;font-weight:700}
table.wv td.s{width:13px;padding:0;text-align:center}
table.wv td.k-d0{background:#dbe9fb}table.wv td.k-d1{background:#8db8ee}
table.wv td.k-cH{background:#9be0a6}table.wv td.k-cL{background:#d4f2d9}
table.wv td.k-x{color:#b0b6c0}table.wv td.k-o{background:#f1e7c8}
table.wv tr.frow td{border-top:2px solid #d1242f;border-bottom:2px solid #d1242f;font-weight:700}
table.wv td.fail{background:#d1242f!important;color:#fff;outline:2px solid #7a0d14;outline-offset:-2px}
table.wv tr.lab td{background:#f4f5f7;color:var(--acc);font-weight:700;white-space:normal;word-break:break-all;max-width:0}
table.wv td.ad{color:var(--mut);text-align:right}table.wv td.nt{color:var(--mut)}
table.ov{border-collapse:collapse;font-size:11px}
table.ov th,table.ov td{border:1px solid var(--bd);padding:3px 6px;white-space:nowrap}
table.ov th{background:#f0f2f5;position:sticky;top:0}
table.ov td.c{cursor:pointer;text-align:center;font-family:Consolas,monospace}
table.ov td.c:hover{outline:2px solid var(--acc)}
table.ov td.t{max-width:520px;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
.ok{background:#d6f5dc}.bad{background:#ffd7d5}.na{color:#aaa}
.hint{color:var(--mut);font-size:11px;margin:4px 0 10px}
</style></head><body>
<header>
<div class="brand">
<svg class="logo" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="48" height="48" aria-hidden="true" focusable="false">
<circle cx="60" cy="60" r="56" fill="#1d3237"/>
<circle cx="60" cy="60" r="50" fill="none" stroke="#527076" stroke-width="1.5"/>
<path d="M60 8V18M60 102V112M8 60H18M102 60H112" stroke="#caed62" stroke-width="3" stroke-linecap="round"/>
<g fill="#2fbf8f">
<rect x="29" y="29" width="18" height="18" rx="4"/>
<rect x="51" y="29" width="18" height="18" rx="4"/>
<rect x="73" y="29" width="18" height="18" rx="4"/>
<rect x="73" y="51" width="18" height="18" rx="4"/>
<rect x="73" y="73" width="18" height="18" rx="4"/>
</g>
<g fill="#f07a5f">
<rect x="29" y="51" width="18" height="18" rx="4"/>
<rect x="29" y="73" width="18" height="18" rx="4"/>
<rect x="51" y="73" width="18" height="18" rx="4"/>
</g>
<rect x="51" y="51" width="18" height="18" rx="4" fill="#ffd166" stroke="#1b2b28" stroke-width="2.5"/>
<g transform="translate(48.12 71.88) rotate(45) scale(.6)">
<path d="M0 -28L-20 22L0 10L20 22Z" fill="none" stroke="#1d3237" stroke-width="10" stroke-linejoin="round"/>
<path d="M0 -28L-20 22L0 10Z" fill="#ffffff"/>
<path d="M0 -28L0 10L20 22Z" fill="#c8dfd7"/>
</g>
</svg>
<h1>Shmoo-Nav</h1>
</div>
<span class="meta" id="meta"></span>
</header>
<main>
<aside>
  <h3>Units <span><a id="uall">all</a> / <a id="unone">none</a></span></h3>
  <div id="units"></div>
  <h3>Tests</h3>
  <input id="filter" placeholder="filter tests (regex)">
  <div id="tests"></div>
</aside>
<section>
  <div class="tabs"><button id="tabOv">Overview</button><button id="tabPl">Shmoo plots</button></div>
  <div id="view"></div>
  <div id="detail"></div>
</section>
</main>
<div id="tip"></div>
<script>
const DATA = __DATA__;
const PAL=["#e5534b","#f0883e","#d29922","#a371f7","#db61a2","#1f6feb","#8b5a2b","#bf3989","#6e7781","#cf222e","#9a6700","#0550ae","#8250df","#953800","#116329","#4d2d00"];
const $=s=>document.querySelector(s), el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
const isPassCell=ch=>ch.length>0&&[...ch].every(c=>c==="*");
const isUnknownCell=ch=>ch.length>0&&[...ch].every(c=>c==="?")
const state={units:new Set(DATA.units.map(u=>u.id)),test:null,tab:"ov",pinned:null};
const colorOf=ch=>isPassCell(ch)?"var(--pass)":(/^[A-Z]$/.test(ch)?PAL[(ch.charCodeAt(0)-65+PAL.length*4)%PAL.length]:"#d1242f");
const patternNames=[...new Set(DATA.units.flatMap(u=>Object.values(u.shmoos).flatMap(s=>Object.values(s.legend).map(lg=>lg.pattern))).filter(Boolean))].sort();
const patternColors=new Map(patternNames.map((name,i)=>[name,i<PAL.length?PAL[i]:`hsl(${(i*137.508)%360},60%,38%)`]));
const patternColor=name=>patternColors.get(name)||"#6e7781";
const fmt=(v,name)=>{if(typeof v!=="number")return String(v);const a=Math.abs(v);
  if(a!==0&&a<1e-6)return +(v*1e9).toPrecision(6)+"ns";return String(+v.toPrecision(6))+(/VOLT|vcc|PCODE/i.test(name)?"V":"")};
const short=t=>t.split("::").pop();
const group=t=>{const p=t.split("::");p.pop();return p.join("::")||"(none)"};

function header(){
  const h=DATA.files.map(f=>{const x=f.header;return `${f.name}  |  lot ${x.lotid||"?"}  |  ${x.prgnm||""}  |  ${x.processstep||x.flstpid||""}  |  ${x.tempr?x.tempr+"C":""}  |  ${x.sysid||""}`});
  const tps=[...new Set(DATA.files.map(f=>f.tp&&(f.tp.tp_dir||f.tp.tplpath)).filter(Boolean))];
  $("#meta").textContent=h.join("   ||   ")+(tps.length?`   |   TP: ${tps.join(" ; ")}`:"")+`   (generated ${DATA.generated})`;
  $("#meta").title=DATA.files.map(f=>f.tp?`${f.name}\nprogram: ${f.tp.program}\nfolder: ${f.tp.tp_dir||"(not found on shares)"}\ntpl: ${f.tp.tpl||f.tp.tplpath}\nstpl: ${f.tp.stpl||f.tp.stplpath}`:f.name).join("\n\n");
}

function renderUnits(){
  const box=$("#units");box.innerHTML="";
  DATA.units.forEach(u=>{
    const l=el("label");const cb=el("input");cb.type="checkbox";cb.checked=state.units.has(u.id);
    cb.onchange=()=>{cb.checked?state.units.add(u.id):state.units.delete(u.id);renderTests();renderView()};
    l.append(cb," "+u.label+" ");
    const i=u.info;l.append(el("span","bin",`[${i.curibin||"?"}/${i.curfbin||"?"}]`));
    l.title=Object.entries(i).map(([k,v])=>k+": "+v).join("\n");box.append(l);
  });
}

function selUnits(){return DATA.units.filter(u=>state.units.has(u.id))}

function renderTests(){
  const box=$("#tests");box.innerHTML="";let re=null;
  try{re=new RegExp($("#filter").value,"i")}catch(e){}
  let last=null;
  DATA.tests.filter(t=>!re||re.test(t)).forEach(t=>{
    const g=group(t);if(g!==last){box.append(el("div","grp",g));last=g}
    const d=el("div","ti"+(t===state.test?" sel":""));d.append(el("span",null,short(t)));
    const us=selUnits().filter(u=>u.shmoos[t]);const nf=us.filter(u=>u.shmoos[t].summary.min_pass==null).length;
    const b=el("span","badge"+(nf?" f":""),nf?`${nf}/${us.length} no pass`:`${us.length} units`);
    b.title="units with no fully passing X column / units with this test";d.append(b);
    d.title=t;d.onclick=()=>{state.test=t;state.tab="pl";state.pinned=null;renderTests();renderView()};box.append(d);
  });
}

function legendInfo(s,ch){
  if(isPassCell(ch))return null;const lg=s.legend[ch];
  return lg||{pattern:"(no legend entry for '"+ch+"')",plist:"",vector:"",cycle:"",pins:"",raw:""};
}

function cellLegends(s,i,j,ch){
  if(s.legend_mode==="coordinate")return s.cell_legend[`${i},${j}`]||[];
  return isPassCell(ch)?[]:[legendInfo(s,ch)];
}

function cellPattern(s,i,j,ch){
  const legends=cellLegends(s,i,j,ch),patterns=[...new Set(legends.map(lg=>lg.pattern).filter(Boolean))];
  return {count:legends.length,patterns,color:patterns.length===1?patternColor(patterns[0]):"#6e7781"};
}

function showTip(ev,u,s,i,j){
  const ch=s.rows[i][j],tip=$("#tip");tip.innerHTML="";
  const add=(k,v)=>{if(v===""||v==null)return;const r=el("div");r.append(el("b",null,k+": "),document.createTextNode(v));tip.append(r)};
  add("Unit",u.label);add(s.xname,fmt(s.x[j],s.xname));add(s.yname,fmt(s.y[i],s.yname));
  if(isPassCell(ch))add("Result","PASS");
  else{
    const legends=cellLegends(s,i,j,ch);
    add("Result","FAIL ("+ch+")");
    if(s.legend_mode==="coordinate"){
      add("Failures",ch);add("Patterns",cellPattern(s,i,j,ch).patterns.length);
      if(legends.length){const lg=legends[0];add("First pattern",lg.pattern);add("Plist",lg.plist);
        add("Fail vector/cycle",lg.vector?lg.vector+" / "+lg.cycle:"");add("Pins",lg.pins);
        if(legends.length>1)add("Details","Click the point to see all "+legends.length+" legend entries")}
      else add("Legend","No coordinate legend matches this point");
    }else{
      const lg=legends[0];add("Pattern",lg.pattern);add("Plist",lg.plist);
      add("Fail vector/cycle",lg.vector?lg.vector+" / "+lg.cycle:"");add("Pins",lg.pins);
      const t=lg.trace;if(t&&!t.error){add("Source",t.sub?`${t.sub}: ${t.label}+${t.label_offset} (CALL at line ${t.line})`:`line ${t.line}${t.label?"  "+t.label+"+"+t.label_offset:""}`);
        add("Expected",t.expect);add("Captures",t.sub?"":[t.state,t.note].filter(Boolean).join("  "))}}
  }
  tip.style.display="block";moveTip(ev);
}
function moveTip(ev){const t=$("#tip");const w=t.offsetWidth,h=t.offsetHeight;
  let x=ev.clientX+14,y=ev.clientY+14;if(x+w>innerWidth)x=ev.clientX-w-14;if(y+h>innerHeight)y=ev.clientY-h-14;t.style.left=x+"px";t.style.top=Math.max(0,y)+"px"}
function hideTip(){$("#tip").style.display="none"}

function appendTraceDetail(d,t){
  if(!t)return;
  d.append(el("h4",null,"Pattern source"));
  d.append(el("pre",null,t.error?`FLASH   : ${t.flash_path||""}\n${t.error}`:
    `File    : ${t.file}\nFLASH   : ${t.flash_path}\nTID     : ${t.tid}   line item: ${t.lineitem}\n`+
    (t.sub?`Location: inside subroutine ${t.sub}, called at file line ${t.line}\nLabel   : ${t.label?t.label+" + "+t.label_offset+" vectors (subroutine address "+t.addr+")":"(no subroutine label)"}\nNote    : ${t.note}`:
    `Vector  : ${t.addr} at file line ${t.line}  ${t.op||"V"}\nLabel   : ${t.label?t.label+" + "+t.label_offset+" vectors":"(none before this vector)"}\nExpected: ${t.expect||"(no compares on this vector)"}`+
    (t.state||t.note?`\nState   : ${t.state||""}\nNote    : ${t.note||""}`:""))));
  if(!t.error)d.append(el("pre","ctx","  addr     cycle  line    opcode                         compares\n"+t.context.join("\n")));
  if(!t.error&&t.grid&&DATA.grids&&DATA.grids[t.grid])d.append(waveGrid(DATA.grids[t.grid],t));
}

function showDetail(u,s,i,j){
  const ch=s.rows[i][j],d=$("#detail");d.style.display="block";d.innerHTML="";
  d.append(el("h4",null,`${u.label}  —  ${state.test}`));
  d.append(el("div",null,`${s.xname} = ${fmt(s.x[j],s.xname)},  ${s.yname} = ${fmt(s.y[i],s.yname)}  →  ${isPassCell(ch)?"PASS":"FAIL ("+ch+")"}`));
  if(!isPassCell(ch)){
    const legends=cellLegends(s,i,j,ch);
    if(s.legend_mode==="coordinate"){
      d.append(el("h4",null,`Failures: ${ch} | Patterns: ${cellPattern(s,i,j,ch).patterns.length}`));
      d.append(el("h4",null,`Failure legends (${legends.length})`));
      if(!legends.length)d.append(el("pre",null,"No coordinate legend matches this point."));
      legends.forEach((lg,n)=>{
        d.append(el("pre",null,`${n+1}. ${lg.key||""}\nPattern : ${lg.pattern}\nPlist   : ${lg.plist}\nDomain  : ${lg.domain||""}\nVector  : ${lg.vector}\nCycle   : ${lg.cycle}\nPins    : ${lg.pins}\nSuffix  : ${lg.suffix||""}\n\nRaw     : ${lg.raw}`));
        appendTraceDetail(d,lg.trace);
      });
    }else{
      const lg=legends[0];
      d.append(el("pre",null,`Pattern : ${lg.pattern}\nPlist   : ${lg.plist}\nDomain  : ${lg.domain||""}\nVector  : ${lg.vector}\nCycle   : ${lg.cycle}\nPins    : ${lg.pins}\nSuffix  : ${lg.suffix||""}\n\nRaw     : ${lg.raw}`));
      appendTraceDetail(d,lg.trace);
    }
  }

function waveGrid(g,t){
  const box=el("div");
  box.append(el("h4",null,"Vector view"+(t.sub?` (fail is inside ${t.sub} at ${t.label}+${t.label_offset}; failing pins marked F on the CALL row)`:"")));
  const fp=new Set((t.fail_pins||[]).map(p=>p.toUpperCase()));
  const kind=ch=>({"0":"d0","1":"d1","H":"cH","L":"cL","X":"x"}[ch]||"o");
  const tb=el("table","wv"),th=el("thead"),hr=el("tr");
  ["addr","cycle","line","op"].forEach(h=>hr.append(el("th",null,h)));
  g.pins.forEach(p=>{const c=el("th","pn"+(fp.has(p.toUpperCase())?" fp":""),p);c.title=p;hr.append(c)});
  hr.append(el("th",null,"note"));th.append(hr);tb.append(th);
  const body=el("tbody");let failCell=null;
  g.rows.forEach(([a,c,ln,op,st,note,lab])=>{
    if(lab){const lr=el("tr","lab"),td=el("td",null,lab+":");td.colSpan=g.pins.length+5;lr.append(td);body.append(lr)}
    const tr=el("tr",a===g.center?"frow":"");
    tr.append(el("td","ad",a),el("td","ad",c==null?"":c),el("td","ad","L"+ln),el("td",null,op||""));
    if(st==null&&a===g.center&&t.sub){
      g.pins.forEach(p=>{const f=fp.has(p.toUpperCase()),td=el("td","s"+(f?" fail":""),f?"F":"");
        if(f){td.title=`${p}: failing strobe is inside ${t.sub} at ${t.label}+${t.label_offset} (subroutine vector ${t.addr})`;failCell=failCell||td}tr.append(td)})}
    else if(st==null){const td=el("td","nt",op?"":"serial vector (no per-pin states)");td.colSpan=g.pins.length;tr.append(td)}
    else [...st].forEach((ch,i)=>{const td=el("td","s k-"+kind(ch),ch);td.title=`${g.pins[i]} = ${ch}  (vector ${a})`;
      if(a===g.center&&!t.sub&&fp.has(g.pins[i].toUpperCase())){td.classList.add("fail");failCell=failCell||td}tr.append(td)});
    tr.append(el("td","nt",note||""));body.append(tr);
  });
  tb.append(body);const sc=el("div","wvscroll");sc.append(tb);box.append(sc);
  box.append(el("div","hint","Pins run left to right, vectors top to bottom. 0/1 = drive, H/L = strobe (compare), X = don't care. Red = the failing pin at the failing vector; only pins that strobe or toggle in this window are shown."));
  if(failCell)requestAnimationFrame(()=>{sc.scrollLeft=failCell.offsetLeft-sc.clientWidth/2;sc.scrollTop=failCell.offsetTop-sc.clientHeight/2});
  return box;
}
}

function shmooCard(u,t){
  const s=u.shmoos[t],c=el("div","card");
  c.append(el("h4",null,u.label));
  const sm=s.summary;
  c.append(el("div","sub",`bin ${u.info.curibin||"?"}/${u.info.curfbin||"?"}  |  fails ${sm.fails}/${sm.total}  |  min pass ${sm.min_pass==null?"none":fmt(sm.min_pass,s.yname)}`));
  const tb=el("table","sh");
  for(let i=s.y.length-1;i>=0;i--){
    const tr=el("tr");tr.append(el("th","yl",fmt(s.y[i],s.yname)));
    for(let j=0;j<s.x.length;j++){
      const ch=s.rows[i][j],pass=isPassCell(ch),unknown=isUnknownCell(ch),td=el("td",pass?"p":(unknown?"n":""));
      if(!pass&&!unknown){
        td.style.background=s.legend_mode==="coordinate"?cellPattern(s,i,j,ch).color:colorOf(ch);
        td.style.color="#fff";td.textContent=ch;
      }
      if(state.pinned&&state.pinned.u===u.id&&state.pinned.i===i&&state.pinned.j===j)td.classList.add("pin");
      td.onmouseenter=e=>showTip(e,u,s,i,j);td.onmousemove=moveTip;td.onmouseleave=hideTip;
      td.onclick=()=>{state.pinned={u:u.id,i,j};document.querySelectorAll("td.pin").forEach(x=>x.classList.remove("pin"));td.classList.add("pin");showDetail(u,s,i,j)};
      tr.append(td);
    }
    tb.append(tr);
  }
  const xr=el("tr");xr.append(el("th"));s.x.forEach(v=>xr.append(el("th",null,fmt(v,s.xname))));tb.append(xr);
  const ar=el("tr");ar.append(el("th"));const ax=el("th","ax",s.xname);ax.colSpan=s.x.length;ar.append(ax);tb.append(ar);
  const wrap=el("div");wrap.style.display="flex";wrap.style.gap="6px";wrap.style.alignItems="center";
  const yl=el("div","ax",s.yname);yl.style.cssText="writing-mode:vertical-rl;transform:rotate(180deg);font-size:11px;font-weight:600";
  wrap.append(yl,tb);c.append(wrap);
  if(s.legend_mode==="coordinate"){
    c.append(el("div","hint","Numbers = failure counts per point. A single pattern uses the same color across plots; gray = multiple patterns. Click to see all failures."));
    const used=new Map();
    s.rows.forEach((row,i)=>row.forEach((ch,j)=>{
      if(isPassCell(ch))return;
      const byPattern=new Map();
      cellLegends(s,i,j,ch).forEach(lg=>byPattern.set(lg.pattern,(byPattern.get(lg.pattern)||0)+1));
      byPattern.forEach((count,name)=>{
        const entry=used.get(name)||{points:0,failures:0};
        entry.points++;entry.failures+=count;used.set(name,entry);
      });
    }));
    if(used.size){
      const lt=el("table","lg"),hr=el("tr");
      ["Color","Pattern","#pts","#failures"].forEach(h=>hr.append(el("th",null,h)));lt.append(hr);
      [...used.keys()].sort().forEach(name=>{
        const entry=used.get(name),tr=el("tr"),sw=el("td"),b=el("span","sw");
        b.style.background=patternColor(name);sw.append(b);
        tr.append(sw,el("td",null,name),el("td",null,entry.points),el("td",null,entry.failures));lt.append(tr);
      });
      c.append(lt);
    }
  }else{
    const used={};s.rows.forEach(r=>r.forEach(ch=>{if(!isPassCell(ch)&&!isUnknownCell(ch))used[ch]=(used[ch]||0)+1}));
    const keys=Object.keys(used).sort();
    if(keys.length){
      const lt=el("table","lg");const hr=el("tr");["","#pts","Pattern","Plist","Vec/Cyc","Pins"].forEach(h=>hr.append(el("th",null,h)));lt.append(hr);
      keys.forEach(k=>{const lg=legendInfo(s,k),tr=el("tr");const sw=el("td");const b=el("span","sw");b.style.background=colorOf(k);sw.append(b,k);
        tr.append(sw,el("td",null,used[k]),el("td",null,lg.pattern),el("td",null,lg.plist),el("td",null,lg.vector?lg.vector+"/"+lg.cycle:""),el("td",null,lg.pins));lt.append(tr)});
      c.append(lt);
    }
  }
  return c;
}

function renderPlots(v){
  if(!state.test){v.append(el("div","hint","Select a test on the left, or click a cell in the Overview."));return}
  v.append(el("h2",null,state.test));
  v.append(el("div","hint","Hover a point for its result; click to pin failure details below. Green = pass; colored cells = failures."));
  const cards=el("div","cards");
  const us=selUnits().filter(u=>u.shmoos[state.test]);
  if(!us.length)cards.append(el("div","hint","No selected unit has this test."));
  us.forEach(u=>cards.append(shmooCard(u,state.test)));v.append(cards);
}

function renderOverview(v){
  v.append(el("div","hint","Each cell = lowest passing "+"Y value (all points above it pass) across X; red = no full pass column / any failure at top. Click a cell to open the plot."));
  let re=null;try{re=new RegExp($("#filter").value,"i")}catch(e){}
  const us=selUnits(),tb=el("table","ov"),hr=el("tr");hr.append(el("th",null,"Test"));
  us.forEach(u=>{const th=el("th",null,u.label);th.title=Object.entries(u.info).map(([k,v])=>k+": "+v).join("\n");hr.append(th)});tb.append(hr);
  DATA.tests.filter(t=>!re||re.test(t)).forEach(t=>{
    const tr=el("tr"),tn=el("td","t",short(t));tn.title=t;tn.onclick=()=>{state.test=t;state.tab="pl";renderTests();renderView()};tr.append(tn);
    us.forEach(u=>{const s=u.shmoos[t];
      if(!s){tr.append(el("td","c na","-"));return}
      const sm=s.summary,ok=sm.min_pass!=null;
      const td=el("td","c "+(ok?"ok":"bad"),ok?fmt(sm.min_pass,s.yname)+(sm.fails?"":" ✓"):"FAIL");
      td.title=`${u.label}\nfails ${sm.fails}/${sm.total}\nper-X min pass: ${sm.col_min.map(v=>v==null?"none":fmt(v,s.yname)).join(", ")}`;
      td.onclick=()=>{state.test=t;state.tab="pl";state.pinned=null;renderTests();renderView()};tr.append(td)});
    tb.append(tr);
  });
  v.append(tb);
}

function renderView(){
  const v=$("#view");v.innerHTML="";$("#detail").style.display="none";hideTip();
  $("#tabOv").classList.toggle("on",state.tab==="ov");$("#tabPl").classList.toggle("on",state.tab==="pl");
  state.tab==="ov"?renderOverview(v):renderPlots(v);
}

$("#tabOv").onclick=()=>{state.tab="ov";renderView()};
$("#tabPl").onclick=()=>{state.tab="pl";renderView()};
$("#filter").oninput=()=>{renderTests();if(state.tab==="ov")renderView()};
$("#uall").onclick=()=>{DATA.units.forEach(u=>state.units.add(u.id));renderUnits();renderTests();renderView()};
$("#unone").onclick=()=>{state.units.clear();renderUnits();renderTests();renderView()};
header();renderUnits();renderTests();renderView();
</script></body></html>
"""


def write_html(data: dict, out: Path) -> None:
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    title = html.escape(", ".join(f["name"] for f in data["files"]))
    out.write_text(HTML_TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload), encoding="utf-8")


def _list_dirs_matching(base: Path, prefix: str) -> list[str]:
    """Names of subfolders of base starting with prefix (case-insensitive)."""
    if os.name == "nt":
        # Server-side wildcard filter: avoids listing thousands of lot folders over SMB.
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.FindFirstFileW.restype = wintypes.HANDLE
        k32.FindFirstFileW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.WIN32_FIND_DATAW))
        k32.FindNextFileW.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.WIN32_FIND_DATAW))
        k32.FindClose.argtypes = (wintypes.HANDLE,)
        data = wintypes.WIN32_FIND_DATAW()
        h = k32.FindFirstFileW(str(base / f"{prefix}*"), ctypes.byref(data))
        if h is None or h == wintypes.HANDLE(-1).value:
            return []
        names = []
        try:
            while True:
                if data.dwFileAttributes & 0x10:  # FILE_ATTRIBUTE_DIRECTORY
                    names.append(data.cFileName)
                if not k32.FindNextFileW(h, ctypes.byref(data)):
                    break
        finally:
            k32.FindClose(h)
        return names
    with os.scandir(base) as it:
        return [e.name for e in it if e.is_dir() and e.name.upper().startswith(prefix.upper())]


def find_vpo_dirs(vpo: str, sites: tuple[str, ...] = SITES) -> list[Path]:
    """Lot folders named <VPO> or <VPO>_<lcode> in every site's hdmxdata prod/engr area."""
    vpo = vpo.upper()

    def scan(base: Path) -> list[Path]:
        try:
            return [base / n for n in _list_dirs_matching(base, vpo) if n.upper() == vpo or n.upper().startswith(vpo + "_")]
        except OSError:  # site/area missing or access denied
            return []

    bases = [Path(HDMX_ROOT) / s / "intel" / "hdmxdata" / a for s in sites for a in AREAS]
    with ThreadPoolExecutor(max_workers=len(bases)) as ex:
        return sorted(d for found in ex.map(scan, bases) for d in found)


def expand_inputs(items: list[str], sites: tuple[str, ...] = SITES) -> list[Path]:
    paths: list[Path] = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            paths += sorted(q for q in p.iterdir() if ITUFF_RE.search(q.name))
        elif p.exists():
            paths.append(p)
        elif VPO_RE.match(it):
            dirs = find_vpo_dirs(it, sites)
            print(f"VPO {it}: " + (", ".join(map(str, dirs)) if dirs else f"no folder found in sites {', '.join(sites)}"))
            for d in dirs:
                paths += sorted(q for q in d.iterdir() if ITUFF_RE.search(q.name))
        else:
            paths += [Path(g) for g in sorted(glob.glob(it))]
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="VPO ID(s), ituff file(s) (.itf / .itf.gz), directories, or globs")
    ap.add_argument("-o", "--output", help="output HTML path (default: <first input>_shmoo.html in cwd)")
    ap.add_argument("--csv", help="also write a flat per-point CSV to this path")
    ap.add_argument("--filter", help="only keep shmoo tests whose name matches this regex")
    ap.add_argument("--sites", default=",".join(SITES), help=f"comma-separated sites to search for VPOs (default: {','.join(SITES)})")
    ap.add_argument("--test-program", action="store_true", help="only report where each input's test program lives (reads headers only; no HTML)")
    ap.add_argument("--trace-patterns", action="store_true",
                    help="look up failing patterns in FLASH and resolve each fail vector to its .hdmt.data line, label and expected pins")
    ap.add_argument("--flash-prod", default="DIO", help="FLASH product for --trace-patterns (default: DIO)")
    args = ap.parse_args()

    paths = expand_inputs(args.inputs, tuple(s.strip() for s in args.sites.split(",") if s.strip()))
    if not paths:
        ap.error("no input ituff files found")
    if args.test_program:
        firsts = list({p.parent: p for p in reversed(paths)}.values())[::-1]  # chunks in one lot folder share a program
        print_test_programs(firsts, [read_tp_header(p) for p in firsts])
        return
    flt = re.compile(args.filter, re.I) if args.filter else None
    data = collect(paths, flt)
    print_test_programs(paths, [f["header"] for f in data["files"]])

    n_shmoo = sum(len(u["shmoos"]) for u in data["units"])
    print(f"Found {len(data['units'])} unit(s), {len(data['tests'])} shmoo test(s), {n_shmoo} shmoo(s).")
    if not n_shmoo:
        return
    if args.trace_patterns:
        trace_patterns(data, args.flash_prod)

    base = re.sub(r"\.(itf|ituff)(\.gz)?$", "", paths[0].name, flags=re.I)
    out = Path(args.output) if args.output else Path.cwd() / f"{base}_shmoo.html"
    write_html(data, out)
    print(f"HTML  -> {out}")
    if args.csv:
        write_csv(data, Path(args.csv))
        print(f"CSV   -> {args.csv}")


if __name__ == "__main__":
    main()
