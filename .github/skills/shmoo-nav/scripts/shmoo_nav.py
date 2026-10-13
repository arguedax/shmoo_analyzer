"""Shmoo-Nav: extract shmoo results from ituff files and build an interactive HTML viewer.

Usage:
    python shmoo_nav.py <ituff[.gz] | directory | glob> [...] [-o out.html] [--csv out.csv] [--filter REGEX]

Ituff shmoo records (per unit, per test):
    0_tname_<test>_ShmooParams      0_strgval_<x>^<xs>^<xe>^<xstep>^<y>^<ys>^<ye>^<ystep>_ShmooHub
    0_tname_<test>_ShmooResults     0_strgval_<row0>_<row1>_...   (row = Y step from Y start, char = X step)
    0_tname_<test>^LEGEND^<C>       0_strgval_<pattern>|<idx>:<plist>:<domain>(<vec>,<cyc>,..):<pins>
'*' = pass; any other character = failure described by the matching LEGEND entry.
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import html
import json
import re
from datetime import datetime
from pathlib import Path

PRE_UNIT_KEYS = {"prtnm", "eqpprtid", "siteid", "socket", "dvtststdt", "tiuid", "prttesterid"}
POST_UNIT_KEYS = {"curfbin", "curibin", "binn", "trslt", "dvtsteddt"}
HEADER_KEYS = {"lotid", "prgnm", "prdct", "sysid", "tempr", "processstep", "facid", "flstpid", "lcode"}
LEGEND_RE = re.compile(r"^(?P<pat>[^|]*)\|(?P<idx>\d+):(?P<rest>.*)$")
LOC_RE = re.compile(r":(?P<dom>[A-Za-z0-9_]+(?:::[A-Za-z0-9_]+)*)\((?P<loc>[^)]*)\):(?P<pins>.*)$")


def open_ituff(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", errors="replace")
    return open(path, "r", errors="replace")


def parse_legend(raw: str) -> dict:
    out = {"raw": raw, "pattern": raw, "plist": "", "domain": "", "vector": "", "cycle": "", "pins": ""}
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
        out["vector"] = loc[0] if loc else ""
        out["cycle"] = loc[1] if len(loc) > 1 else ""
        out["pins"] = lm["pins"]
    else:
        out["plist"] = m["rest"]
    return out


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


def build_grid(params: dict | None, results: str) -> tuple[list[float], list[float], list[str], str, str]:
    """Return (x, y, rows, xname, yname); rows[i][j] = result at y[i], x[j]."""
    rows = results.split("_")
    if params:
        x, y = params["x"], params["y"]
        if len(rows) == len(y) and all(len(r) == len(x) for r in rows):
            return x, y, rows, params["xname"], params["yname"]
        if len(rows) == len(x) and all(len(r) == len(y) for r in rows):
            return x, y, ["".join(r[i] for r in rows) for i in range(len(y))], params["xname"], params["yname"]
    width = max((len(r) for r in rows), default=0)
    rows = [r.ljust(width, "?") for r in rows]
    return list(range(width)), list(range(len(rows))), rows, "X index", "Y index"


def summarize(y: list[float], rows: list[str]) -> dict:
    """Per X column, lowest Y from which every higher Y point passes (None if top fails)."""
    ncol = len(rows[0]) if rows else 0
    col_min = []
    for j in range(ncol):
        best = None
        for i in range(len(rows) - 1, -1, -1):
            if rows[i][j] != "*":
                break
            best = y[i]
        col_min.append(best)
    fails = sum(c != "*" for r in rows for c in r)
    passes = [v for v in col_min if v is not None]
    return {"col_min": col_min, "min_pass": min(passes) if passes else None,
            "max_pass": max(passes) if passes else None, "fails": fails,
            "total": sum(len(r) for r in rows)}


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


def collect(paths: list[Path], name_filter: re.Pattern | None) -> dict:
    files, units, tests = [], [], {}
    for fi, p in enumerate(paths):
        print(f"Parsing {p} ...")
        parsed = parse_ituff(p, name_filter)
        files.append({"name": p.name, "path": str(p), "header": parsed["header"]})
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
                x, y, rows, xname, yname = build_grid(rec["params"], rec["results"])
                tkey = rec["test"] + (f" [{rec['die']}]" if rec["die"] else "")
                tests.setdefault(tkey, {"test": rec["test"], "die": rec["die"]})
                shmoos[tkey] = {
                    "x": x, "y": y, "rows": rows, "xname": xname, "yname": yname,
                    "legend": rec["legend"], "params_raw": rec.get("params_raw", ""),
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
                    "result", "pattern", "plist", "vector", "cycle", "pins"])
        for u in data["units"]:
            fname = data["files"][u["file"]]["name"]
            for tkey, s in u["shmoos"].items():
                for i, yv in enumerate(s["y"]):
                    for j, xv in enumerate(s["x"]):
                        c = s["rows"][i][j]
                        lg = s["legend"].get(c, {}) if c != "*" else {}
                        w.writerow([fname, u["info"].get("visualid", ""), u["info"].get("siteid", ""), u["label"],
                                    tkey, s["xname"], xv, s["yname"], yv, "PASS" if c == "*" else c,
                                    lg.get("pattern", ""), lg.get("plist", ""), lg.get("vector", ""),
                                    lg.get("cycle", ""), lg.get("pins", "")])


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
const state={units:new Set(DATA.units.map(u=>u.id)),test:null,tab:"ov",pinned:null};
const colorOf=ch=>ch==="*"?"var(--pass)":PAL[(ch.charCodeAt(0)-65+PAL.length*4)%PAL.length];
const fmt=(v,name)=>{if(typeof v!=="number")return String(v);const a=Math.abs(v);
  if(a!==0&&a<1e-6)return +(v*1e9).toPrecision(6)+"ns";return String(+v.toPrecision(6))+(/VOLT|vcc|PCODE/i.test(name)?"V":"")};
const short=t=>t.split("::").pop();
const group=t=>{const p=t.split("::");p.pop();return p.join("::")||"(none)"};

function header(){
  const h=DATA.files.map(f=>{const x=f.header;return `${f.name}  |  lot ${x.lotid||"?"}  |  ${x.prgnm||""}  |  ${x.processstep||x.flstpid||""}  |  ${x.tempr?x.tempr+"C":""}  |  ${x.sysid||""}`});
  $("#meta").textContent=h.join("   ||   ")+`   (generated ${DATA.generated})`;
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
  if(ch==="*")return null;const lg=s.legend[ch];
  return lg||{pattern:"(no legend entry for '"+ch+"')",plist:"",vector:"",cycle:"",pins:"",raw:""};
}

function showTip(ev,u,s,i,j){
  const ch=s.rows[i][j],tip=$("#tip");tip.innerHTML="";
  const add=(k,v)=>{if(v===""||v==null)return;const r=el("div");r.append(el("b",null,k+": "),document.createTextNode(v));tip.append(r)};
  add("Unit",u.label);add(s.xname,fmt(s.x[j],s.xname));add(s.yname,fmt(s.y[i],s.yname));
  if(ch==="*")add("Result","PASS");else{const lg=legendInfo(s,ch);add("Result","FAIL ("+ch+")");add("Pattern",lg.pattern);add("Plist",lg.plist);
    add("Fail vector/cycle",lg.vector?lg.vector+" / "+lg.cycle:"");add("Pins",lg.pins)}
  tip.style.display="block";moveTip(ev);
}
function moveTip(ev){const t=$("#tip");const w=t.offsetWidth,h=t.offsetHeight;
  let x=ev.clientX+14,y=ev.clientY+14;if(x+w>innerWidth)x=ev.clientX-w-14;if(y+h>innerHeight)y=ev.clientY-h-14;t.style.left=x+"px";t.style.top=Math.max(0,y)+"px"}
function hideTip(){$("#tip").style.display="none"}

function showDetail(u,s,i,j){
  const ch=s.rows[i][j],d=$("#detail");d.style.display="block";d.innerHTML="";
  d.append(el("h4",null,`${u.label}  —  ${state.test}`));
  d.append(el("div",null,`${s.xname} = ${fmt(s.x[j],s.xname)},  ${s.yname} = ${fmt(s.y[i],s.yname)}  →  ${ch==="*"?"PASS":"FAIL ("+ch+")"}`));
  if(ch!=="*"){const lg=legendInfo(s,ch);
    d.append(el("pre",null,`Pattern : ${lg.pattern}\nPlist   : ${lg.plist}\nDomain  : ${lg.domain||""}\nVector  : ${lg.vector}\nCycle   : ${lg.cycle}\nPins    : ${lg.pins}\n\nRaw     : ${lg.raw}`));}
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
      const ch=s.rows[i][j],td=el("td",ch==="*"?"p":(ch==="?"?"n":""));
      if(ch!=="*"&&ch!=="?"){td.style.background=colorOf(ch);td.textContent=ch}
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
  const used={};s.rows.forEach(r=>[...r].forEach(ch=>{if(ch!=="*"&&ch!=="?")used[ch]=(used[ch]||0)+1}));
  const keys=Object.keys(used).sort();
  if(keys.length){
    const lt=el("table","lg");const hr=el("tr");["","#pts","Pattern","Plist","Vec/Cyc","Pins"].forEach(h=>hr.append(el("th",null,h)));lt.append(hr);
    keys.forEach(k=>{const lg=legendInfo(s,k),tr=el("tr");const sw=el("td");const b=el("span","sw");b.style.background=colorOf(k);sw.append(b,k);
      tr.append(sw,el("td",null,used[k]),el("td",null,lg.pattern),el("td",null,lg.plist),el("td",null,lg.vector?lg.vector+"/"+lg.cycle:""),el("td",null,lg.pins));lt.append(tr)});
    c.append(lt);
  }
  return c;
}

function renderPlots(v){
  if(!state.test){v.append(el("div","hint","Select a test on the left, or click a cell in the Overview."));return}
  v.append(el("h2",null,state.test));
  v.append(el("div","hint","Hover a point for the limiting pattern; click to pin details below. Green = pass, letters = failure legend."));
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


def expand_inputs(items: list[str]) -> list[Path]:
    paths: list[Path] = []
    for it in items:
        p = Path(it)
        if p.is_dir():
            paths += sorted(q for q in p.iterdir() if re.search(r"\.(itf|ituff)(\.gz)?$", q.name, re.I))
        elif p.exists():
            paths.append(p)
        else:
            paths += [Path(g) for g in sorted(glob.glob(it))]
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="ituff file(s) (.itf / .itf.gz), directories, or globs")
    ap.add_argument("-o", "--output", help="output HTML path (default: <first input>_shmoo.html in cwd)")
    ap.add_argument("--csv", help="also write a flat per-point CSV to this path")
    ap.add_argument("--filter", help="only keep shmoo tests whose name matches this regex")
    args = ap.parse_args()

    paths = expand_inputs(args.inputs)
    if not paths:
        ap.error("no input ituff files found")
    flt = re.compile(args.filter, re.I) if args.filter else None
    data = collect(paths, flt)

    n_shmoo = sum(len(u["shmoos"]) for u in data["units"])
    print(f"Found {len(data['units'])} unit(s), {len(data['tests'])} shmoo test(s), {n_shmoo} shmoo(s).")
    if not n_shmoo:
        return

    base = re.sub(r"\.(itf|ituff)(\.gz)?$", "", paths[0].name, flags=re.I)
    out = Path(args.output) if args.output else Path.cwd() / f"{base}_shmoo.html"
    write_html(data, out)
    print(f"HTML  -> {out}")
    if args.csv:
        write_csv(data, Path(args.csv))
        print(f"CSV   -> {args.csv}")


if __name__ == "__main__":
    main()
