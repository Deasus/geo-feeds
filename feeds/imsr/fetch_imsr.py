#!/usr/bin/env python3
"""
geo-feeds IMSR pipeline — pulls today's NICC Incident Management Situation
Report (https://www.nifc.gov/nicc-files/sitreprt.pdf), parses the structured
sections into JSON, and writes data/imsr.json + data/health.json for the
geo-feeds frontend to consume.

The IMSR is a once-daily PDF published by the National Interagency
Coordination Center, ~06:30 MT. It contains:

  - National Preparedness Level + national fire-activity totals
  - Per-GACC Active Incident Resource Summary table (page 1)
  - Per-GACC narrative + large-fire detail tables (pages 2-4)
  - Yesterday's fires/acres by protection (page 5)
  - Year-to-Date fires/acres + 10-year average (page 6)
  - Predictive Services Discussion (page 7)

Why this matters for geo-feeds: WFIGS gives us live IRWIN incident geometry,
but the IMSR is the authoritative human-curated daily summary that ICs and
fire-management leadership read first thing in the morning. Surfacing it
directly lets consumers answer "what's the national prep level today" / "who's got
a CIMT committed" / "what does Predictive Services say about today's
weather" closes a real situational gap.

Cadence: every 15 min via GHA self-redispatch loop. NIFC publishes once daily
but sometimes re-publishes a corrected version mid-day; 15-min catches that
within an acceptable window. Watchdog `health.json` tracks consecutive fetch
failures so we can spot if NIFC starts rate-limiting or if the URL moves.

Stdlib only — no JWT, no API key. pdfplumber is the one runtime dep
(installed via the GHA workflow). Public IMSR has no auth, no robots
restriction (verified by reading https://www.nifc.gov/robots.txt — sitreprt
is not Disallowed).

Schema is in README.md.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pdfplumber

PDF_URL = "https://www.nifc.gov/nicc-files/sitreprt.pdf"
USER_AGENT = "geo-feeds/1.0 (+https://github.com/Deasus/geo-feeds)"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
PDF_PATH = DATA_DIR / "imsr.pdf"
JSON_PATH = DATA_DIR / "imsr.json"
HEALTH_PATH = DATA_DIR / "health.json"

GACC_ROW_RE = re.compile(
    # GACC code, then an OPTIONAL per-GACC Preparedness Level column, then the
    # 7 numeric columns (incidents, acres, crews, engines, helicopters,
    # personnel, change-in-personnel).
    #
    # The PL column is optional on purpose. NICC ADDED it to the page-1 table
    # sometime between 2026-06-15 and 2026-07-01 (national PL went 2 -> 4 in
    # that window). The original 7-column regex then matched nothing, and
    # because there was no row-count guard the pipeline reported healthy while
    # `resource_summary` sat empty for ~2 months. Keeping the group optional
    # means both the pre-July (7-col) and current (8-col) layouts parse, so a
    # future NICC format flip in either direction degrades instead of zeroing.
    # Regex backtracking resolves the ambiguity: exactly 7 trailing numeric
    # columns are required, so a 7-number row skips the optional group and an
    # 8-number row binds it.
    #
    # The Total row carries "---" in the PL column rather than a digit.
    r"^(AICC|NWCC|ONCC|OSCC|NRCC|GBCC|SWCC|RMCC|EACC|SACC|Total)\s+"
    r"(?:(\d|---)\s+)?"
    r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+(-?[\d,]+)\s*$"
)

# GACC name → human-readable area name (the page-2/3/4 narrative section
# headers don't use the 4-letter codes; they use the full names).
GACC_AREA_NAMES = {
    "Alaska Area": "AICC",
    "Northwest Area": "NWCC",
    "Northern California Area": "ONCC",
    "Southern California Area": "OSCC",
    "Northern Rockies Area": "NRCC",
    "Great Basin Area": "GBCC",
    "Southwest Area": "SWCC",
    "Rocky Mountain Area": "RMCC",
    "Eastern Area": "EACC",
    "Southern Area": "SACC",
}

# Page 5/6 protection-area names (slightly different from GACC narrative
# headers — "Northern California" vs "Northern California Area")
PROTECTION_AREAS = [
    "Alaska", "Northwest", "Northern California", "Southern California",
    "Northern Rockies", "Great Basin", "Southwest", "Rocky Mountain",
    "Eastern Area", "Southern Area",
]


def fetch_pdf() -> bytes:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(PDF_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return r.read()


def parse_top_summary(p1_text: str) -> dict:
    """Page 1 header — date, prep level, national totals."""
    out = {}
    # Date line: "Monday June 8, 2026 – 0730 MDT"
    m = re.search(r"^(\w+ \w+ \d{1,2}, \d{4})\s*[–-]\s*(\d{4} \w{2,4})", p1_text, re.MULTILINE)
    if m:
        out["report_date_human"] = m.group(1)
        out["report_time"] = m.group(2)
    # Prep level: "National Preparedness Level 2"
    m = re.search(r"National Preparedness Level (\d)", p1_text)
    if m:
        out["national_preparedness_level"] = int(m.group(1))
    # Activity ladder
    for label, key in [
        ("Initial attack activity", "initial_attack_activity"),
        ("New large incidents", "new_large_incidents"),
        ("Large fires contained", "large_fires_contained"),
        ("Uncontained large fires", "uncontained_large_fires"),
        ("CIMTs committed", "cimts_committed"),
        ("NIMOs committed", "nimos_committed"),
    ]:
        # Initial attack carries free text; the rest are integers.
        if key == "initial_attack_activity":
            m = re.search(rf"{label}:\s*([^\n]+)", p1_text)
            if m:
                out[key] = m.group(1).strip()
        else:
            m = re.search(rf"{label}:\s*(\d+)", p1_text)
            if m:
                out[key] = int(m.group(1))
    return out


def _to_int(s: str) -> int:
    return int(s.replace(",", ""))


def parse_resource_summary(p1_text: str) -> list:
    """Page 1 GACC table — Active Incident Resource Summary.

    `pl` is the per-GACC Preparedness Level (1-5), or None when the column is
    absent (pre-July-2026 layout) or not applicable (the Total row, which
    prints "---").

    NOTE: the Total row IS included in this list (it always has been, despite
    an earlier comment claiming otherwise). Consumers filter by `gacc`, so
    leaving it in place keeps the shape consumers already expect.
    """
    out = []
    for line in p1_text.splitlines():
        m = GACC_ROW_RE.match(line.strip())
        if not m:
            continue
        pl_raw = m.group(2)
        out.append({
            "gacc": m.group(1),
            "pl": int(pl_raw) if pl_raw and pl_raw.isdigit() else None,
            "incidents": _to_int(m.group(3)),
            "cumulative_acres": _to_int(m.group(4)),
            "crews": _to_int(m.group(5)),
            "engines": _to_int(m.group(6)),
            "helicopters": _to_int(m.group(7)),
            "total_personnel": _to_int(m.group(8)),
            "change_in_personnel": _to_int(m.group(9)),
        })
    return out


def parse_gacc_sections(text: str, large_fire_tables=None, warnings=None) -> list:
    """Pages 2-4 — per-GACC narrative + uncontained-large-fire table.

    `large_fire_tables` is extract_large_fire_tables() output ({area: rows});
    a section it has no entry for falls back to reading its table from `text`.
    Large-fire problems are appended to `warnings`.
    """
    if warnings is None:
        warnings = []
    out = []
    # Find each "<Area> Area (PL N)" header, collect the block until the next
    # area header (or the "Fires and Acres Yesterday" header, which starts the
    # protection table on page 5).
    headers = []
    for area, gacc_code in GACC_AREA_NAMES.items():
        for m in re.finditer(rf"^{re.escape(area)} \(PL (\d)\)", text, re.MULTILINE):
            headers.append({
                "area": area,
                "gacc": gacc_code,
                "preparedness_level": int(m.group(1)),
                "start": m.start(),
            })
    headers.sort(key=lambda h: h["start"])

    # Sentinel end positions
    end_marker = re.search(r"^Fires and Acres Yesterday", text, re.MULTILINE)
    text_end = end_marker.start() if end_marker else len(text)

    for i, h in enumerate(headers):
        block_start = h["start"]
        block_end = headers[i + 1]["start"] if i + 1 < len(headers) else text_end
        block = text[block_start:block_end]
        section = {
            "gacc": h["gacc"],
            "area": h["area"],
            "preparedness_level": h["preparedness_level"],
        }
        # New fires / new large incidents / uncontained large fires / CIMTs
        for label, key in [
            ("New fires", "new_fires"),
            ("New large incidents", "new_large_incidents"),
            ("Uncontained large fires", "uncontained_large_fires"),
            ("CIMTs Committed", "cimts_committed"),
        ]:
            m = re.search(rf"{label}:\s*(\d+)", block)
            if m:
                section[key] = int(m.group(1))
        # Narrative paragraphs — anything between the activity totals and the
        # incident detail table that's wrapped in flowing prose. Heuristic:
        # grab lines that aren't header rows, table column rows, or empty.
        lines = block.splitlines()
        narrative_lines = []
        in_table = False
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            # Stop at table column header
            if "Incident Name" in s and "% Ctn" in s:
                in_table = True
                continue
            if in_table:
                # Table rows are not narrative; they are parsed into
                # section["large_fires"] below.
                continue
            if s.startswith(h["area"]):
                continue
            if any(s.startswith(prefix) for prefix in [
                "Fire Activity", "New fires:", "New large incidents:",
                "Uncontained large fires:", "CIMTs Committed:", "Total Chge",
                "Acres Acres",
            ]):
                continue
            narrative_lines.append(s)
        section["narrative"] = " ".join(narrative_lines).strip() or None
        section["large_fires"] = _section_large_fires(section, block, large_fire_tables, warnings)
        out.append(section)
    return out


# ── Large-fire tables ────────────────────────────────────────────────────────
#
# Each GACC section ends with a 15-column table of its large fires:
#
#   Incident Name | Unit | Total Acres | Chge in Acres | % | Ctn/Comp | Est |
#   Total PPL | Chge in PPL | Crw | Eng | Heli | Strc Lost | $$ CTD | Origin Own
#
# Every row becomes one dict with the LARGE_FIRE_FIELDS keys. Counts are ints
# (acres a float only if the PDF prints a decimal), cost_to_date_usd is whole
# dollars ("7.4M" -> 7400000), and blank / "---" / "UNK" / "NR" numeric cells
# are null. est_date stays as printed ("10/31", "UNK", "Comp"; no year), null
# when blank or "---". is_new is the leading "*" NICC puts on new incidents.
#
# Rows are read from the PDF's table cells (pdfplumber find_tables), not from
# extract_text() lines: on busy days long names and units wrap inside their
# cell ("Rowe Creek / Complex", "NV- / TMFX"), and the text layer puts the two
# halves on the lines above and below the numbers. If a section's table cannot
# be found as cells, its text lines are parsed instead, from the right edge
# (see parse_large_fire_line).

LARGE_FIRE_FIELDS = (
    "name", "unit", "acres", "acres_change", "pct", "ctn_or_comp", "est_date",
    "personnel", "personnel_change", "crews", "engines", "helicopters",
    "structures_lost", "cost_to_date_usd", "origin_owner", "is_new",
)
# The 13 fixed columns right of Unit, in PDF order.
_LF_TAIL = LARGE_FIRE_FIELDS[2:15]
_LF_TEXT_FIELDS = {"ctn_or_comp", "est_date", "origin_owner"}
_LF_DASHES = {"", "-", "--", "---", "----"}
_LF_NUM_NULLS = _LF_DASHES | {"UNK", "NR", "N/A", "NA", "TBD"}
_N = r"(?:\d{1,3}(?:,\d{3})+|\d+)"          # 1,234 or 1234
_LF_PATTERNS = {
    "acres": re.compile(rf"^{_N}(?:\.\d+)?$"),
    "acres_change": re.compile(rf"^[-+]?{_N}(?:\.\d+)?$"),
    "pct": re.compile(r"^\d{1,3}%?$"),
    "ctn_or_comp": re.compile(r"^(?:Ctn|Comp)$", re.I),
    "est_date": re.compile(r"^(?:\d{1,2}/\d{1,2}(?:/\d{2,4})?|UNK|Comp|TBD|N/?A)$", re.I),
    "personnel": re.compile(rf"^{_N}$"),
    "personnel_change": re.compile(rf"^[-+]?{_N}$"),
    "crews": re.compile(rf"^{_N}$"),
    "engines": re.compile(rf"^{_N}$"),
    "helicopters": re.compile(rf"^{_N}$"),
    "structures_lost": re.compile(rf"^{_N}$"),
    "cost_to_date_usd": re.compile(rf"^\$?({_N}(?:\.\d+)?)\s*([KMB])?$", re.I),
    "origin_owner": re.compile(r"^[A-Za-z][A-Za-z&/.]*$"),
}
_LF_COST_MULT = {"": 1, "K": 1000, "M": 1000000, "B": 1000000000}
_LF_UNIT_RE = re.compile(r"^[A-Z]{2,3}-[A-Z0-9]{2,6}$")
_LF_TOTAL_RE = re.compile(r"^(?:sub|grand\s+)?totals?\b", re.I)
_LF_VALUE_TOKEN = re.compile(
    r"^(?:[-+]?[\d,]+(?:\.\d+)?[KMB%]?|\$[\d,.]+[KMB]?|\d{1,2}/\d{1,2}|-{1,4}|UNK|NR)$", re.I)
_SECTION_HEADER_RE = re.compile(r"^(?P<area>[A-Z][A-Za-z.' -]*?) \(PL \d\)")
# (field, test on the lower-cased header text stacked above one column).
# First match wins, so the "Chge in ..." columns are tested before the totals.
_LF_COLUMN_LABELS = (
    ("name", lambda l: "incident name" in l or l == "incident"),
    ("unit", lambda l: l == "unit"),
    ("acres_change", lambda l: "acres" in l and "chg" in l),
    ("acres", lambda l: "acres" in l),
    ("pct", lambda l: l == "%"),
    ("ctn_or_comp", lambda l: "ctn" in l or "comp" in l),
    ("est_date", lambda l: l.startswith("est")),
    ("personnel_change", lambda l: "ppl" in l and "chg" in l),
    ("personnel", lambda l: "ppl" in l),
    ("crews", lambda l: l.startswith("crw")),
    ("engines", lambda l: l.startswith("eng")),
    ("helicopters", lambda l: l.startswith("heli")),
    ("structures_lost", lambda l: "strc" in l),
    ("cost_to_date_usd", lambda l: "ctd" in l),
    ("origin_owner", lambda l: "origin" in l),
)


def _lf_value(field: str, raw):
    """(ok, value) for one cell. ok is False when the text doesn't fit the column."""
    s = (raw or "").strip()
    if s.upper() in (_LF_DASHES if field in _LF_TEXT_FIELDS else _LF_NUM_NULLS):
        return True, None
    m = _LF_PATTERNS[field].match(s)
    if not m:
        return False, None
    if field in _LF_TEXT_FIELDS:
        return True, s
    if field == "cost_to_date_usd":
        amount = Decimal(m.group(1).replace(",", "")) * _LF_COST_MULT[(m.group(2) or "").upper()]
        return True, int(amount)
    n = s.replace(",", "").rstrip("%")
    return True, (float(n) if "." in n else int(n))


def _lf_unwrap(cell, joiner: str = " ") -> str:
    """Rejoin a cell that wrapped onto several lines ("NV-\\nTMFX" -> "NV-TMFX")."""
    out = ""
    for part in (cell or "").split("\n"):
        part = part.strip()
        if part:
            out = out + part if (not out or out.endswith("-")) else out + joiner + part
    return out


def _lf_row(name: str, unit, values: dict) -> dict:
    name = re.sub(r"\s+", " ", name).strip()
    row = {"name": name.lstrip("*").strip(), "unit": unit or None}
    for f in _LF_TAIL:
        row[f] = values.get(f)
    row["is_new"] = name.startswith("*")
    return row


def parse_large_fire_cells(cells: list, colmap: dict):
    """One table row (list of cell strings) -> (row, problem).

    (None, None) means "not a fire": spacer, repeated header, section label,
    footnote or total row. A row with a cell that doesn't fit its column keeps
    that field null and reports it; more than three such cells drops the row.
    """
    def cell(field, joiner=""):
        i = colmap.get(field)
        return _lf_unwrap(cells[i], joiner) if i is not None and i < len(cells) else ""

    raw = " | ".join(_lf_unwrap(c) for c in cells)
    name, unit = cell("name", " "), cell("unit")
    rest = {f: cell(f) for f in _LF_TAIL}
    filled = [v for v in rest.values() if v.upper() not in _LF_DASHES]
    if name.lower() == "incident name" or _LF_TOTAL_RE.match(name.lstrip("* ")):
        return None, None
    if not unit and not filled:
        return None, None
    if not name:
        if not any(_LF_VALUE_TOKEN.match(v) for v in filled):
            return None, None                       # header text without a name cell
        return None, f"row has no incident name: {raw!r}"
    values, bad = {}, []
    for f, v in rest.items():
        ok, values[f] = _lf_value(f, v) if f in colmap else (True, None)
        if not ok:
            bad.append(f"{f}={v!r}")
    if len(bad) > 3:
        return None, f"unparseable row ({', '.join(bad)}): {raw!r}"
    problem = f"cells set to null ({', '.join(bad)}): {raw!r}" if bad else None
    return _lf_row(name, unit, values), problem


def _lf_fit_tail(tokens: list) -> list:
    """Every in-order placement of tokens into _LF_TAIL; skipped columns are blank cells."""
    fits = []

    def place(ti, fi, acc):
        if ti == len(tokens):
            fits.append(dict(acc))
            return
        for f in range(fi, len(_LF_TAIL) - (len(tokens) - ti) + 1):
            ok, v = _lf_value(_LF_TAIL[f], tokens[ti])
            if ok:
                place(ti + 1, f + 1, acc + [(_LF_TAIL[f], v)])

    if len(tokens) <= len(_LF_TAIL):
        place(0, 0, [])
    return fits


def parse_large_fire_line(line: str):
    """One extract_text() table line -> (row, problem); (None, None) = not a fire row.

    Read from the right edge. Unit and the 13 columns after it each have a
    fixed shape, so the incident name, the only free-text column, is whatever
    sits left of the Unit (names may hold spaces and digits: "0587 Shingle",
    "MM115 HWY 20"). A blank cell is tolerated when only one placement of the
    remaining values fits the column types; when several fit, the columns they
    disagree on are set to null and reported.
    """
    toks = line.split()
    values_seen = sum(1 for t in toks if _LF_VALUE_TOKEN.match(t))
    u = next((i for i in range(len(toks) - 1, -1, -1) if _LF_UNIT_RE.match(toks[i])), None)
    if values_seen < 6 and not (u is not None and values_seen >= 3):
        return None, None                           # label, footnote, header or wrapped fragment
    if u is None:                                   # unit wrapped off this line: trust full rows only
        name_toks, unit, tail = toks[:-len(_LF_TAIL)], None, toks[-len(_LF_TAIL):]
    else:
        name_toks, unit, tail = toks[:u], toks[u], toks[u + 1:]
    name = " ".join(name_toks)
    if _LF_TOTAL_RE.match(name.lstrip("* ")):
        return None, None
    if not name.strip("* "):
        return None, f"no incident name left of the unit: {line.strip()!r}"
    fits = _lf_fit_tail(tail)
    if not fits:
        return None, f"values do not fit the 15-column layout: {line.strip()!r}"
    values, unsure = {}, []
    for f in _LF_TAIL:
        options = {fit.get(f) for fit in fits}
        values[f] = next(iter(options)) if len(options) == 1 else None
        if len(options) > 1:
            unsure.append(f)
    problems = (["no unit on this line"] if unit is None else []) + (
        [f"a cell is missing, so {', '.join(unsure)} set to null"] if unsure else [])
    problem = f"{'; '.join(problems)}: {line.strip()!r}" if problems else None
    return _lf_row(name, unit, values), problem


def _lf_is_header_row(cells: list) -> bool:
    first = _lf_unwrap(cells[0] if cells else "").lower()
    return first in ("", "incident name") and not any(
        _LF_VALUE_TOKEN.match((c or "").strip()) and (c or "").strip() not in _LF_DASHES for c in cells)


def _lf_column_map(header_rows: list):
    """({field: column index}, labels) from a table's header rows; map is None if it isn't a large-fire table."""
    width = max(len(r) for r in header_rows)
    labels = []
    for i in range(width):
        parts = [_lf_unwrap(r[i]) for r in header_rows if i < len(r) and r[i]]
        labels.append(" ".join(" ".join(parts).split()).lower())
    colmap = {}
    for i, label in enumerate(labels):
        for field, test in _LF_COLUMN_LABELS:
            if label and field not in colmap and test(label):
                colmap[field] = i
                break
    return (colmap if "name" in colmap else None), labels


def _page_section_headers(page) -> list:
    """[(top, area)] for every "<Area> (PL N)" line on a page."""
    lines = []
    for w in sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"])):
        if lines and w["top"] - lines[-1][0] <= 3:
            lines[-1][1].append(w)
        else:
            lines.append((w["top"], [w]))
    out = []
    for top, words in lines:
        m = _SECTION_HEADER_RE.match(" ".join(w["text"] for w in sorted(words, key=lambda w: w["x0"])))
        if m:
            out.append((top, m.group("area")))
    return out


def extract_large_fire_tables(pdf, warnings: list) -> dict:
    """{section area: [row, ...]} from the large-fire table cells, in PDF order.

    A table belongs to the nearest "<Area> (PL N)" header above it, which can
    be pages earlier: long narratives push a table onto later pages and long
    tables continue onto the next page. A table with no header row counts only
    as the continuation of the large-fire table before it. Tables seen before
    any section header are keyed "unassigned".
    """
    out = {}
    area, colmap, width = None, None, None
    for pno, page in enumerate(pdf.pages, start=1):
        events = [(top, 0, a) for top, a in _page_section_headers(page)]
        events += [(t.bbox[1], 1, t) for t in page.find_tables()]
        for _top, kind, obj in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == 0:
                area, colmap = obj, None
                continue
            rows = obj.extract()
            if not rows:
                continue
            n_header = 0
            while n_header < len(rows) and _lf_is_header_row(rows[n_header]):
                n_header += 1
            if n_header:
                colmap, labels = _lf_column_map(rows[:n_header])
                if colmap is None:
                    continue                        # resource summary, protection tables
                width = len(rows[0])
                missing = [f for f in ("unit",) + _LF_TAIL if f not in colmap]
                if missing:
                    warnings.append(f"large_fires: page {pno} table header has no {', '.join(missing)} "
                                    f"column (NICC layout change?); headers seen: {labels}")
            elif colmap is None or len(rows[0]) != width:
                continue
            key = area or "unassigned"
            tag = GACC_AREA_NAMES.get(key, key)
            bucket = out.setdefault(key, [])
            for cells in rows[n_header:]:
                row, problem = parse_large_fire_cells(cells, colmap)
                if row:
                    bucket.append(row)
                if problem:
                    warnings.append(f"large_fires[{tag}] page {pno}: {problem}")
    return out


def _large_fires_from_text(gacc: str, block: str, warnings: list) -> list:
    """Fallback for a section whose table wasn't found as cells: parse its text lines."""
    lines = block.splitlines()
    start = next((i for i, ln in enumerate(lines) if "Incident Name" in ln), None)
    if start is None:
        return []
    rows = []
    for ln in lines[start + 1:]:
        row, problem = parse_large_fire_line(ln)
        if row:
            rows.append(row)
        if problem:
            warnings.append(f"large_fires[{gacc}]: {problem}")
    warnings.append(f"large_fires[{gacc}]: table not found as PDF cells; read {len(rows)} row(s) "
                    f"from text lines, where names or units wrapped onto two lines are lost")
    return rows


def _section_large_fires(section: dict, block: str, tables, warnings: list) -> list:
    gacc = section["gacc"]
    try:
        rows = tables.get(section["area"]) if tables is not None else None
        if rows is None:
            rows = _large_fires_from_text(gacc, block, warnings)
    except Exception as e:                          # one bad table must not sink the feed
        warnings.append(f"large_fires[{gacc}]: {type(e).__name__}: {e}")
        rows = []
    n = section.get("uncontained_large_fires")
    if n and not rows:
        warnings.append(f"large_fires[{gacc}]: section reports {n} uncontained large fire(s) "
                        f"but no table rows were parsed")
    return rows


def parse_yesterday_table(text: str) -> dict:
    """Page 5 — Fires and Acres Yesterday by Protection."""
    out = {"by_area": [], "totals": {}}
    # Stretch from "Fires and Acres Yesterday" header to the next "Fires and
    # Acres Year-to-Date" header.
    yest_m = re.search(r"^Fires and Acres Yesterday \(by Protection\):", text, re.MULTILINE)
    ytd_m = re.search(r"^Fires and Acres Year-to-Date \(by Protection\):", text, re.MULTILINE)
    if not yest_m:
        return out
    block = text[yest_m.end():ytd_m.start() if ytd_m else len(text)]
    cur = {}
    for area in PROTECTION_AREAS:
        # Match "Northern California FIRES 0 0 0 0 14 2 16" then "Northern California ACRES …"
        for kind in ("FIRES", "ACRES"):
            m = re.search(
                rf"^{re.escape(area)}\s+{kind}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$",
                block, re.MULTILINE,
            )
            if not m:
                continue
            row = {
                "BIA": _to_int(m.group(1)),
                "BLM": _to_int(m.group(2)),
                "FWS": _to_int(m.group(3)),
                "NPS": _to_int(m.group(4)),
                "ST_OT": _to_int(m.group(5)),
                "USFS": _to_int(m.group(6)),
                "TOTAL": _to_int(m.group(7)),
            }
            if area not in cur:
                cur[area] = {}
            cur[area][kind.lower()] = row
    for area, kinds in cur.items():
        out["by_area"].append({
            "area": area,
            "fires": kinds.get("fires", {}),
            "acres": kinds.get("acres", {}),
        })
    # Totals
    m = re.search(
        r"TOTAL FIRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["fires"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    m = re.search(
        r"TOTAL ACRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["acres"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    return out


def parse_ytd_table(text: str) -> dict:
    """Page 6 — Year-to-Date by Protection + 10-year averages."""
    out = {"by_area": [], "totals": {}, "ten_year_average": {}}
    ytd_m = re.search(r"^Fires and Acres Year-to-Date \(by Protection\):", text, re.MULTILINE)
    avg_m = re.search(r"^Ten Year Average Quantity", text, re.MULTILINE)
    if not ytd_m:
        return out
    block = text[ytd_m.end():avg_m.start() if avg_m else len(text)]
    cur = {}
    for area in PROTECTION_AREAS:
        for kind in ("FIRES", "ACRES"):
            m = re.search(
                rf"^{re.escape(area)}\s+{kind}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$",
                block, re.MULTILINE,
            )
            if not m:
                continue
            row = {
                "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
                "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
                "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
                "TOTAL": _to_int(m.group(7)),
            }
            if area not in cur:
                cur[area] = {}
            cur[area][kind.lower()] = row
    for area, kinds in cur.items():
        out["by_area"].append({
            "area": area,
            "fires": kinds.get("fires", {}),
            "acres": kinds.get("acres", {}),
        })
    m = re.search(
        r"TOTAL FIRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["fires"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    m = re.search(
        r"TOTAL ACRES:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        block,
    )
    if m:
        out["totals"]["acres"] = {
            "BIA": _to_int(m.group(1)), "BLM": _to_int(m.group(2)),
            "FWS": _to_int(m.group(3)), "NPS": _to_int(m.group(4)),
            "ST_OT": _to_int(m.group(5)), "USFS": _to_int(m.group(6)),
            "TOTAL": _to_int(m.group(7)),
        }
    # 10-year averages — separator can be hyphen, en-dash, em-dash, or
    # surrounding whitespace variations; match liberally.
    m = re.search(r"Fires \(\d{4}\s*[-–—]\s*\d{4}\s*as of today\)\s+([\d,]+)", text)
    if m:
        out["ten_year_average"]["fires"] = _to_int(m.group(1))
    m = re.search(r"Acres \(\d{4}\s*[-–—]\s*\d{4}\s*as of today\)\s+([\d,]+)", text)
    if m:
        out["ten_year_average"]["acres"] = _to_int(m.group(1))
    return out


def parse_predictive_services(text: str) -> str:
    """Page 7 — Predictive Services Discussion (free text)."""
    m = re.search(r"^Predictive Services Discussion:\s*$", text, re.MULTILINE)
    if not m:
        return ""
    end_m = re.search(
        r"^When a Predictive Services Discussion is not available", text, re.MULTILINE,
    )
    body = text[m.end():end_m.start() if end_m else len(text)]
    # Collapse line wraps in the source PDF — paragraphs are flowed across
    # multiple lines; rejoin whitespace so the AI gets clean prose.
    paragraphs = []
    cur = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            if cur:
                paragraphs.append(" ".join(cur))
                cur = []
            continue
        cur.append(s)
    if cur:
        paragraphs.append(" ".join(cur))
    return "\n\n".join(paragraphs).strip()


def parse_imsr_pdf(pdf_bytes: bytes) -> dict:
    tmp = DATA_DIR / "_tmp.pdf"
    tmp.write_bytes(pdf_bytes)
    lf_warnings = []
    try:
        with pdfplumber.open(tmp) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            page1 = pdf.pages[0].extract_text() or ""
            try:
                lf_tables = extract_large_fire_tables(pdf, lf_warnings)
            except Exception as e:
                lf_tables = None                    # every section falls back to its text lines
                lf_warnings.append(f"large_fires: table extraction failed ({type(e).__name__}: {e})")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    sections = parse_gacc_sections(full_text, lf_tables, lf_warnings)
    # Every section's rows by GACC code, plus any table found under a section
    # header this parser doesn't recognise (keyed by the header's area text),
    # so no table is lost if NICC renames a section.
    large_fires_by_gacc = {s["gacc"]: s["large_fires"] for s in sections}
    known_areas = {s["area"] for s in sections}
    for area, rows in (lf_tables or {}).items():
        if area in known_areas or not rows:
            continue
        key = GACC_AREA_NAMES.get(area, area)
        large_fires_by_gacc.setdefault(key, []).extend(rows)
        lf_warnings.append(f"large_fires: {len(rows)} row(s) under section header {area!r}, which has no "
                           f"gacc_sections entry; kept only in large_fires_by_gacc[{key!r}]")
    return {
        "summary": parse_top_summary(page1),
        "resource_summary": parse_resource_summary(page1),
        "gacc_sections": sections,
        "large_fires_by_gacc": large_fires_by_gacc,
        "yesterday_by_protection": parse_yesterday_table(full_text),
        "ytd_by_protection": parse_ytd_table(full_text),
        "predictive_services_discussion": parse_predictive_services(full_text),
        "parse_warnings": lf_warnings,
    }


def write_outputs(parsed: dict, raw_bytes: bytes, fetch_meta: dict) -> None:
    now = datetime.now(timezone.utc)
    parsed["generated_utc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed["source_url"] = PDF_URL
    parsed["pdf_bytes"] = len(raw_bytes)
    parsed["fetch"] = fetch_meta
    JSON_PATH.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
    # Also keep a copy of the most recent raw PDF so the frontend "open IMSR"
    # link can serve from raw.githubusercontent if NIFC is briefly down.
    PDF_PATH.write_bytes(raw_bytes)


def update_health(success: bool, notes: str = "") -> None:
    """Watchdog tracking — consecutive failure count is what we care about.
    If consecutive_failures crosses a threshold we want to dial cron back."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if HEALTH_PATH.exists():
        try:
            health = json.loads(HEALTH_PATH.read_text())
        except Exception:
            health = {}
    else:
        health = {}
    if success:
        health["last_success_utc"] = now
        health["consecutive_failures"] = 0
        health["status"] = "ok"
    else:
        health["last_failure_utc"] = now
        health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
        health["status"] = "degraded" if health["consecutive_failures"] >= 4 else "ok"
        # 4 consecutive 15-min misses = ~1 hour silent; that's the dial-back signal.
    health["last_attempt_utc"] = now
    health["last_notes"] = notes
    health["cadence_minutes"] = 15
    HEALTH_PATH.write_text(json.dumps(health, indent=2))


def main() -> int:
    fetch_started = datetime.now(timezone.utc)
    try:
        raw = fetch_pdf()
    except Exception as e:
        print(f"FETCH FAIL: {e}", file=sys.stderr)
        update_health(success=False, notes=f"fetch error: {e}")
        return 1
    fetch_meta = {
        "fetched_utc": fetch_started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": round((datetime.now(timezone.utc) - fetch_started).total_seconds(), 2),
    }
    try:
        parsed = parse_imsr_pdf(raw)
    except Exception as e:
        print(f"PARSE FAIL: {e}", file=sys.stderr)
        update_health(success=False, notes=f"parse error: {e}")
        return 1
    if not parsed.get("summary"):
        print("PARSE WARN: empty summary — header parser missed", file=sys.stderr)
        update_health(success=False, notes="empty summary block")
        return 1
    # Row-count guards. The failure mode that actually bit us was NOT an
    # exception or a dead URL — it was a structurally-valid PDF whose page-1
    # table silently stopped matching after NICC added a column. The pipeline
    # reported healthy with resource_summary == [] for ~2 months. Never again:
    # write the payload anyway (the other sections stay useful) but record a
    # FAILURE so data/health.json degrades and `health feed` catches it.
    warnings = []
    if not parsed.get("resource_summary"):
        warnings.append("resource_summary EMPTY — page-1 GACC table matched no rows (NICC layout change?)")
    if not parsed.get("gacc_sections"):
        warnings.append("gacc_sections EMPTY — page 2-4 narrative parser matched no sections")
    # Large-fire table rows that didn't parse (raw text included), and sections
    # that report uncontained fires but yielded no rows. Same rule as above.
    warnings.extend(parsed.get("parse_warnings", []))
    parsed["parse_warnings"] = warnings

    write_outputs(parsed, raw, fetch_meta)

    if warnings:
        print("PARSE WARN: " + " | ".join(warnings), file=sys.stderr)
        update_health(success=False, notes="; ".join(warnings))
        return 1

    update_health(success=True, notes=f"parsed pl={parsed['summary'].get('national_preparedness_level')} large_fires={parsed['summary'].get('uncontained_large_fires')}")
    print(f"OK: PL={parsed['summary'].get('national_preparedness_level')} "
          f"uncontained_large={parsed['summary'].get('uncontained_large_fires')} "
          f"gacc_sections={len(parsed['gacc_sections'])} "
          f"resource_rows={len(parsed['resource_summary'])} "
          f"large_fire_rows={sum(len(r) for r in parsed['large_fires_by_gacc'].values())}")
    return 0


def _selftest() -> int:
    """`python fetch_imsr.py --selftest`: offline checks of the large-fire parsers
    on rows captured from real IMSRs (2025-07-17, 2026-03-20, 2026-08-11, 2026-10-20)."""
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    def fields(row, *keys):
        return None if row is None else tuple(row[k] for k in keys)

    # parse_large_fire_line: (line, expected subset or None for "not a fire row", expect a problem)
    norte = {"name": "Norte", "unit": "CA-ORC", "acres": 570, "acres_change": 0, "pct": 80,
             "ctn_or_comp": "Ctn", "est_date": "11/6", "personnel": 319, "personnel_change": -179,
             "crews": 5, "engines": 35, "helicopters": 0, "structures_lost": 6,
             "cost_to_date_usd": 7400000, "origin_owner": "CNTY", "is_new": False}
    grasshopper = {"name": "Grasshopper", "unit": "OR-MHF", "acres": 93972, "acres_change": 0,
                   "pct": 98, "ctn_or_comp": "Ctn", "est_date": "10/31", "personnel": 146,
                   "personnel_change": -21, "crews": 0, "engines": 0, "helicopters": 0,
                   "structures_lost": 33, "cost_to_date_usd": 131000000, "origin_owner": "FS",
                   "is_new": False}
    lines = [
        ("Grasshopper OR-MHF 93,972 0 98 Ctn 10/31 146 -21 0 0 0 33 131M FS", grasshopper, False),
        ("Norte CA-ORC 570 0 80 Ctn 11/6 319 -179 5 35 0 6 7.4M CNTY", norte, False),
        ("* Austin OR-MHF 710 --- 0 Ctn UNK 78 --- 3 3 0 0 500K FS",
         {"name": "Austin", "is_new": True, "acres_change": None, "est_date": "UNK",
          "personnel_change": None, "cost_to_date_usd": 500000}, False),
        ("Mount Pilgrim Road MS-MSS 150 0 100 Ctn --- 0 0 0 0 0 0 30K ST",
         {"name": "Mount Pilgrim Road", "pct": 100, "est_date": None, "cost_to_date_usd": 30000}, False),
        ("0587 Shingle OR-953S 73,016 -85 86 Ctn 8/20 445 -22 6 36 0 5 22.4M ST",
         {"name": "0587 Shingle", "unit": "OR-953S", "acres_change": -85, "cost_to_date_usd": 22400000}, False),
        ("MM115 HWY 20 ID-SWS 4,936 0 24 Ctn 8/12 265 67 8 18 2 0 3.7M ST",
         {"name": "MM115 HWY 20", "personnel": 265}, False),
        ("* Cold Springs WY-COX 1,000 --- 0 Ctn 8/12 69 --- 0 11 2 0 NR CNTY",
         {"name": "Cold Springs", "is_new": True, "cost_to_date_usd": None, "origin_owner": "CNTY"}, False),
        ("Big Fire AK-FAS 33,107 18 4 Comp 9/1 475 -6 11 15 2 2 850K ST",
         {"ctn_or_comp": "Comp", "cost_to_date_usd": 850000}, False),
        # Est cell blank: only one placement fits, so nothing is lost.
        ("Norte CA-ORC 570 0 80 Ctn 319 -179 5 35 0 6 7.4M CNTY", dict(norte, est_date=None), False),
        # Heli cell blank: Crw/Eng/Heli/Strc can't be told apart, so they go null and get reported.
        ("Norte CA-ORC 570 0 80 Ctn 11/6 319 -179 5 35 6 7.4M CNTY",
         {"name": "Norte", "personnel_change": -179, "crews": None, "structures_lost": None,
          "cost_to_date_usd": 7400000}, True),
        # Unit wrapped onto the lines above/below: a full 13-value tail is still read.
        ("* Fred Mt 3,482 --- 0 Ctn 8/23 0 --- 0 0 0 0 600K CNTY",
         {"name": "Fred Mt", "unit": None, "acres": 3482, "cost_to_date_usd": 600000}, True),
        # Name wrapped onto the lines above/below: reported, not guessed.
        ("OR-PRD 371,549 4,932 84 Ctn 10/1 1,660 45 39 107 10 33 60.5M BLM", None, True),
        ("Total CA-XXX 1,000 0 50 Ctn --- 10 0 0 0 0 0 1M ---", None, False),
        ("Total Chge in Total Chge in Strc Origin", None, False),
        ("Acres Acres PPL PPL Lost Own", None, False),
        ("PMQ – Marine Corps Base Camp Pendleton, DOW", None, False),
        ("JAX – Jackson County CRA – Cheyenne Agency, BIA MSX – Marshall County", None, False),
        ("Fires/Complexes Previously Reported: 3", None, False),
        ("Complex", None, False),
        ("NV-", None, False),
    ]
    for line, want, want_problem in lines:
        row, problem = parse_large_fire_line(line)
        if want is None:
            check(f"line {line!r} row", row, None)
        elif row is None:
            failures.append(f"line {line!r}: no row ({problem})")
        else:
            check(f"line {line!r} keys", tuple(row), LARGE_FIRE_FIELDS)
            check(f"line {line!r}", {k: row[k] for k in want}, want)
        check(f"line {line!r} problem?", problem is not None, want_problem)

    # Header rows as pdfplumber returns them, current layout and the 2025 / March-2026 one.
    hdr_now = [["", "", "Total", "Chge in", "", "", "", "Total", "Chge in", "", "", "", "Strc", "", "Origin"],
               ["Incident Name", "Unit", None, None, "%", "Ctn", "Est", None, None, "Crw", "Eng", "Heli",
                None, "$$ CTD", None],
               [None, None, "Acres", "Acres", None, None, None, "PPL", "PPL", None, None, None, "Lost",
                None, "Own"],
               ["", "", None, None, "", "", "", None, None, "", "", "", None, "", None]]
    hdr_old = [["Incident Name", "Unit", "Total", "Chge in", "%", "Ctn/", "Est", "Total", "Chge in", "Crw",
                "Eng", "Heli", "Strc", "$$ CTD", "Origin"],
               [None, None, "Acres", "Acres", None, "Comp", None, "PPL", "PPL", None, None, None, "Lost",
                None, "Own"]]
    identity = {f: i for i, f in enumerate(LARGE_FIRE_FIELDS[:15])}
    for label, hdr in (("current", hdr_now), ("2025", hdr_old)):
        check(f"header {label} all header rows", all(_lf_is_header_row(r) for r in hdr), True)
        check(f"header {label} column map", _lf_column_map(hdr)[0], identity)
    check("resource table is not a large-fire table",
          _lf_column_map([["GACC", "PL", "Incidents", "Cumulative\nAcres", "Crews"]])[0], None)

    cells = [
        (["Rowe Creek\nComplex", "OR-PRD", "371,549", "4,932", "84", "Ctn", "10/1", "1,660", "45", "39",
          "107", "10", "33", "60.5M", "BLM"], ("Rowe Creek Complex", "OR-PRD", 371549, 60500000, False)),
        (["* Fred Mt", "NV-\nTMFX", "3,482", "---", "0", "Ctn", "8/23", "0", "---", "0", "0", "0", "0",
          "600K", "CNTY"], ("Fred Mt", "NV-TMFX", 3482, 600000, True)),
        (["Bee Haven Bay 24-\n06", "FL-FLS", "170", "---", "73", "Ctn", "UNK", "3", "---", "0", "1", "0",
          "0", "NR", "ST"], ("Bee Haven Bay 24-06", "FL-FLS", 170, None, False)),
        (["Grasshopper", "OR-MHF", "93,972", "0", "98", "Ctn", "10/31", "146", "-21", "0", "0", "0", "33",
          "131M", "FS"], ("Grasshopper", "OR-MHF", 93972, 131000000, False)),
        (["Fires/Complexes Being Managed for Resource Benefit"] + [None] * 14, None),
        (["", ""] + [None] * 13, None),
        (["Total", "", "552,897", "", "", "", "", "3,206", "", "", "", "", "", "", ""], None),
    ]
    for c, want in cells:
        row, problem = parse_large_fire_cells(c, identity)
        check(f"cells {c[0]!r}", fields(row, "name", "unit", "acres", "cost_to_date_usd", "is_new"), want)
        check(f"cells {c[0]!r} problem", problem, None)
    row, problem = parse_large_fire_cells(["Norte", "CA-ORC", "570", "0", "eighty", "Ctn", "11/6", "319",
                                           "-179", "5", "35", "0", "6", "7.4M", "CNTY"], identity)
    check("cells bad pct", (fields(row, "name", "pct", "personnel"), problem is not None), (("Norte", None, 319), True))

    # Section fallback when no table cells exist: rows come from text, the fallback is reported.
    block = "\n".join(["Northwest Area (PL 1)", "Uncontained large fires: 3", "Austin, Mt. Hood NF, USFS.",
                       "Total Chge in Total Chge in Strc Origin",
                       "Incident Name Unit % Ctn Est Crw Eng Heli $$ CTD", "Acres Acres PPL PPL Lost Own",
                       "Austin OR-MHF 12,888 0 98 Ctn 10/31 262 -9 0 2 0 0 79.2M FS",
                       "Grasshopper OR-MHF 93,972 0 98 Ctn 10/31 146 -21 0 0 0 33 131M FS",
                       "Sisi WA-OWF 4,743 128 34 Ctn 10/31 28 0 0 0 0 0 11.9M FS",
                       "MHF – Mt. Hood National Forest"])
    warns = []
    rows = _section_large_fires({"gacc": "NWCC", "area": "Northwest Area", "uncontained_large_fires": 3},
                                block, {}, warns)
    check("fallback rows", [r["name"] for r in rows], ["Austin", "Grasshopper", "Sisi"])
    check("fallback reported", len(warns), 1)
    warns = []
    check("guard: cells table empty", _section_large_fires(
        {"gacc": "EACC", "area": "Eastern Area", "uncontained_large_fires": 2}, block,
        {"Eastern Area": []}, warns), [])
    check("guard reported", len(warns), 1)

    for f in failures:
        print("SELFTEST FAIL:", f, file=sys.stderr)
    print(f"selftest: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    sys.exit(main())
