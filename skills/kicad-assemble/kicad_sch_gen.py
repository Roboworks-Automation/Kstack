#!/usr/bin/env python3
"""
kicad_sch_gen.py — Generate a KiCad 9.0 schematic from a design spec.

Reads design.yaml, looks up component symbols from the PRASAD library,
consults the knowledge graph for historical MCU pin connections, and writes
a ready-to-open KiCad project:
    <out_dir>/<project>.kicad_pro
    <out_dir>/<project>.kicad_sch

Usage:
    python3 kicad_sch_gen.py design.yaml [options]
    python3 kicad_sch_gen.py design.yaml --list-pins STM32F103C8T6

design.yaml shape:
    project: my_rs485_board
    mcu:
      part: STM32F103C8T6
      ref: U1                 # optional, default U1

    peripherals:
      - role: rs485/sn65hvd   # knowledge-graph role (for pin hints)
        part: SN65HVD3082EDR
        ref: U2               # optional, default U2, U3...
        name: RS485_BUS       # label shown in schematic (optional)
        connections:          # MCU-pin -> peripheral-pin  (optional)
          PA12: D
          PA11: R
          PA9: DE
          PA10: ~{RE}

      - role: opto/tlp
        part: TLP281-4
        ref: U3
        name: OPTO_IN

Options:
    --out DIR           Output directory  (default ~/Documents/kicad/<project>)
    --knowledge-dir DIR Knowledge graph   (default ~/kc/kicad-knowledge)
    --footprint-dir DIR PRASAD footprints (default ~/Documents/PRASAD/05326/Footprint)
    --download-dir DIR  Downloaded parts  (default ~/Documents/footprints)
    --list-pins PART    Print pin names for a part and exit (for connection planning)
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import uuid as _uuid
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed.  Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ─── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_PROJECT_DIR    = Path.home() / "Documents/kicad"
DEFAULT_KNOWLEDGE_DIR  = Path.home() / "kc/kicad-knowledge"
DEFAULT_PRASAD_DIR     = Path.home() / "Documents/PRASAD/05326/Footprint"
DEFAULT_DOWNLOAD_DIR   = Path.home() / "Documents/footprints"
DEFAULT_FP_INDEX       = Path.home() / "kc/kicad-footprints/index.yaml"
# Multi-symbol libraries (one .kicad_sym contains many parts, e.g. KiCad stock)
DEFAULT_MULTI_LIB_DIRS = [Path("/usr/share/kicad/symbols")]
# Stock/user .pretty directories to scan for footprints matching ki_fp_filters.
DEFAULT_STOCK_FP_DIRS  = [
    Path("/usr/share/kicad/footprints"),
    Path.home() / "Documents/PRASAD/05326/Footprint",
]


# ─── Symbol file lookup ────────────────────────────────────────────────────────

def _sym_contains(path: Path, part: str) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    return f'(symbol "{part}"' in text


def find_sym_file(part: str,
                  prasad_dir: Path,
                  extra_dirs: list[Path],
                  multi_lib_dirs: list[Path] | None = None,
                  ) -> tuple[Path, str, str] | None:
    """
    Locate the .kicad_sym file for `part`.
    Returns (file_path, symbol_name, lib_name) or None.

    Search order (each root checked in turn):
      1. <root>/<part>/<part>.kicad_sym            — standard SnapEDA layout
      2. <root>/<part>.kicad_sym                   — flat layout
      3. <root>/<part>/<anything>.kicad_sym        — any sym in part subdir
      4. <root>/<part>/KiCad/<anything>.kicad_sym  — vendor KiCADv* subfolder
      5. multi-symbol libs: scan each .kicad_sym in multi_lib_dirs
         and check for `(symbol "<part>"`. Returns (file, part, lib_stem).
    """
    for root in [prasad_dir] + extra_dirs:
        # 1. Exact: subdir/part.kicad_sym
        c = root / part / f"{part}.kicad_sym"
        if c.exists():
            return c, part, part
        # 2. Flat
        c = root / f"{part}.kicad_sym"
        if c.exists():
            return c, part, part
        # 3. Any inside part subdir
        part_dir = root / part
        if part_dir.is_dir():
            for sub in ["KiCad", "KiCADv6", "KiCADv5", "."]:
                search_dir = part_dir / sub if sub != "." else part_dir
                if search_dir.is_dir():
                    syms = sorted(search_dir.glob("*.kicad_sym"))
                    if syms:
                        return syms[0], part, part
    # 5. Multi-symbol libraries
    for root in (multi_lib_dirs or []):
        if not root.is_dir():
            continue
        for sym_path in sorted(root.glob("*.kicad_sym")):
            if _sym_contains(sym_path, part):
                return sym_path, part, sym_path.stem
    return None


def extract_symbol_text(sym_file: Path, symbol_name: str) -> tuple[str, str] | None:
    """
    Return (raw_s_expr, actual_symbol_name) for the symbol inside the .kicad_sym file.
    Tries exact name match first, then falls back to the first symbol in the file.
    """
    content = sym_file.read_text(encoding="utf-8")

    def _extract_at(idx: int) -> str:
        depth = 0
        for i, c in enumerate(content[idx:], idx):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return content[idx:i + 1]
        return content[idx:]

    # Try exact name
    pattern = f'(symbol "{symbol_name}"'
    idx = content.find(pattern)
    if idx != -1:
        return _extract_at(idx), symbol_name

    # Fallback: first (symbol "...") that is NOT a sub-symbol (no _\d+_\d+ suffix)
    for m in re.finditer(r'\(symbol "([^"]+)"', content):
        name = m.group(1)
        if re.search(r'_\d+_\d+$', name):
            continue  # skip sub-symbols
        return _extract_at(m.start()), name

    return None


def embed_symbol(sym_text: str, bare_name: str, lib_name: str) -> str:
    """
    Rename ONLY the parent symbol for embedding in lib_symbols.
    "NAME" -> "lib:NAME". Sub-symbols like "NAME_0_1" must keep their bare
    names, because KiCad expects sub-symbols inside a lib_symbols entry to be
    referenced without the library prefix.
    """
    full = f"{lib_name}:{bare_name}"
    # Rename only the first occurrence (the parent declaration).
    return sym_text.replace(f'(symbol "{bare_name}"', f'(symbol "{full}"', 1)


def get_symbol_pins(sym_text: str) -> dict[str, tuple[float, float, float]]:
    """
    Extract {pin_name: (local_x, local_y, pin_angle)} from a symbol S-expression.

    The (at X Y ANGLE) inside a pin block is the electrical connection point
    in the symbol's local coordinate system (before world placement transform).
    Pins are indexed by both name and number.
    """
    pins: dict[str, tuple[float, float, float]] = {}

    # Match: (pin TYPE STYLE (at X Y ANGLE) (length L) ... (name "N" ...) (number "N" ...))
    pin_re = re.compile(
        r'\(pin\s+\S+\s+\S+\s+'              # (pin type style
        r'\(at\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\)\s+'  # (at X Y ANGLE)
        r'\(length\s+([-\d.e+]+)\)'           # (length L)
        r'.*?'
        r'\(name\s+"([^"]*)"'                 # (name "N"
        r'.*?'
        r'\(number\s+"([^"]*)"',              # (number "N"
        re.DOTALL,
    )
    for m in pin_re.finditer(sym_text):
        px      = float(m.group(1))
        py      = float(m.group(2))
        pangle  = float(m.group(3))
        pname   = m.group(5).strip()
        pnum    = m.group(6).strip()

        key = pname if pname else pnum
        if key and key not in pins:
            pins[key] = (px, py, pangle)
        if pnum and pnum not in pins:
            pins[pnum] = (px, py, pangle)

    return pins


def get_footprint_from_symbol(sym_text: str) -> str:
    """Extract the Footprint property value from a symbol definition."""
    m = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', sym_text)
    return m.group(1) if m else ""


# ─── Footprint-usage index (historical mapping from past PCBs) ────────────────

_FP_INDEX_CACHE: dict | None = None

def load_fp_index(path: Path) -> dict:
    global _FP_INDEX_CACHE
    if _FP_INDEX_CACHE is not None:
        return _FP_INDEX_CACHE
    if not path.exists():
        _FP_INDEX_CACHE = {}
        return _FP_INDEX_CACHE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        _FP_INDEX_CACHE = (data or {}).get("entries", {}) or {}
    except Exception:
        _FP_INDEX_CACHE = {}
    return _FP_INDEX_CACHE


def lookup_footprint(part: str, value: str, fp_index: dict) -> str:
    """
    Return the most-used footprint lib_id for a given part/value, or "".
    Tries exact match on value, then on part, then case-insensitive, then
    length>=3 substring in either direction.
    """
    if not fp_index:
        return ""
    for key in (value, part):
        if key and key in fp_index and fp_index[key]:
            return fp_index[key][0].get("footprint", "")
    for key in (value, part):
        if not key:
            continue
        kl = key.lower()
        # exact case-insensitive
        for k, rows in fp_index.items():
            if k.lower() == kl and rows:
                return rows[0].get("footprint", "")
        # substring >= 3 chars
        if len(kl) >= 3:
            best = None; best_count = 0
            for k, rows in fp_index.items():
                if not rows:
                    continue
                lk = k.lower()
                if (kl in lk and len(kl) >= 3) or (lk in kl and len(lk) >= 3):
                    c = rows[0].get("count", 0)
                    if c > best_count:
                        best_count = c; best = rows[0].get("footprint", "")
            if best:
                return best
    return ""


# ─── Stock footprint library scan (KiCad 9 + user .pretty dirs) ──────────────
#
# The history index covers parts used in past projects. For brand-new parts
# we fall back to scanning all `*.pretty` directories (stock + user) and
# matching the symbol's `ki_fp_filters` globs against footprint filenames.

_STOCK_FP_INDEX_CACHE: list[tuple[str, str]] | None = None   # [(lib_id, fp_name_lower)]

def build_stock_fp_index(dirs: list[Path]) -> list[tuple[str, str]]:
    """Return [(lib_id, fp_name_lower), ...] across all *.pretty dirs."""
    global _STOCK_FP_INDEX_CACHE
    if _STOCK_FP_INDEX_CACHE is not None:
        return _STOCK_FP_INDEX_CACHE
    entries: list[tuple[str, str]] = []
    for root in dirs:
        if not root.exists():
            continue
        for pretty in root.rglob("*.pretty"):
            if not pretty.is_dir():
                continue
            lib = pretty.stem                             # dir name minus .pretty
            for mod in pretty.glob("*.kicad_mod"):
                fp = mod.stem
                entries.append((f"{lib}:{fp}", fp.lower()))
    _STOCK_FP_INDEX_CACHE = entries
    return entries


def get_fp_filters_from_symbol(sym_text: str) -> list[str]:
    m = re.search(r'\(property\s+"ki_fp_filters"\s+"([^"]*)"', sym_text)
    if not m:
        return []
    return [p for p in m.group(1).split() if p]


def _glob_to_regex(pat: str) -> re.Pattern:
    """KiCad fp filter globs: '*' any, '?' single char; case-insensitive."""
    rx = ""
    for ch in pat:
        if ch == "*":
            rx += ".*"
        elif ch == "?":
            rx += "."
        else:
            rx += re.escape(ch)
    return re.compile("^" + rx + "$", re.IGNORECASE)


def lookup_stock_footprint(sym_text: str,
                           stock_fp_dirs: list[Path] | None = None) -> str:
    """
    Match a symbol's ki_fp_filters against stock/user .pretty libraries.
    Returns the first matching "Lib:Footprint" lib_id, or "".
    """
    filters = get_fp_filters_from_symbol(sym_text)
    if not filters:
        return ""
    dirs = stock_fp_dirs if stock_fp_dirs is not None else DEFAULT_STOCK_FP_DIRS
    entries = build_stock_fp_index(dirs)
    if not entries:
        return ""
    for pat in filters:
        rx = _glob_to_regex(pat)
        for lib_id, fp_lower in entries:
            if rx.match(fp_lower) or rx.match(lib_id.split(":", 1)[1]):
                return lib_id
    return ""


def pin_world(px: float, py: float,
              cx: float, cy: float,
              sym_rotation: float = 0.0) -> tuple[float, float]:
    """Transform a pin's local symbol coordinates to schematic world coordinates."""
    a = math.radians(sym_rotation)
    cos_a, sin_a = math.cos(a), math.sin(a)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    return round(cx + rx, 2), round(cy - ry, 2)   # KiCad: world Y is flipped


def pin_world_with_outward(px: float, py: float, pangle: float,
                           cx: float, cy: float,
                           sym_rotation: float = 0.0
                           ) -> tuple[float, float, float, float]:
    """
    Return (world_x, world_y, outward_dx, outward_dy) in schematic world coords.
    `outward_*` is a unit vector pointing AWAY from the symbol body (into free
    space). In KiCad symbol files, a pin's own angle points from the electrical
    tip INTO the body, so outward = pangle + 180° in local coords, then we
    apply the symbol rotation and flip Y for the world frame.
    """
    a = math.radians(sym_rotation)
    cos_a, sin_a = math.cos(a), math.sin(a)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    wx = round(cx + rx, 2)
    wy = round(cy - ry, 2)

    out_a = math.radians(pangle + 180.0)
    dx_loc = math.cos(out_a)
    dy_loc = math.sin(out_a)
    rdx = dx_loc * cos_a - dy_loc * sin_a
    rdy = dx_loc * sin_a + dy_loc * cos_a
    # Snap to cardinal to avoid floating-point wobble (pins are always 0/90/180/270).
    wdx = 1.0 if rdx >  0.5 else (-1.0 if rdx < -0.5 else 0.0)
    wdy = -(1.0 if rdy >  0.5 else (-1.0 if rdy < -0.5 else 0.0))  # flip Y for world
    return wx, wy, wdx, wdy


def gen_wire(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f'\n  (wire\n'
        f'    (pts (xy {x1} {y1}) (xy {x2} {y2}))\n'
        f'    (stroke (width 0) (type default))\n'
        f'    (uuid "{_uid()}")\n'
        f'  )'
    )


def gen_stub_and_label(net: str,
                       pin_x: float, pin_y: float,
                       wdx: float, wdy: float,
                       stub_len: float = 2.54) -> str:
    """
    Emit a short wire stub from (pin_x, pin_y) outward and a net label at the
    stub's far end. Label rotation is chosen so text reads away from the symbol.
    """
    end_x = round(pin_x + stub_len * wdx, 2)
    end_y = round(pin_y + stub_len * wdy, 2)
    # KiCad label angle: 0 = text goes right, 90 = up, 180 = left, 270 = down.
    # World Y grows downward, so "outward-down" (wdy=+1) wants angle 270.
    if wdx > 0:
        lab_angle = 0
    elif wdx < 0:
        lab_angle = 180
    elif wdy > 0:
        lab_angle = 270
    else:
        lab_angle = 90
    return gen_wire(pin_x, pin_y, end_x, end_y) + \
           gen_net_label(net, end_x, end_y, lab_angle)


# ─── Knowledge graph ──────────────────────────────────────────────────────────

def load_block_knowledge(knowledge_dir: Path, role: str) -> dict | None:
    """Load block YAML.  role = 'rs485/sn65hvd'."""
    fname = role.replace("/", "_") + ".yaml"
    path = knowledge_dir / "blocks" / fname
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return None


def get_suggested_mcu_pins(knowledge: dict, mcu_part: str) -> list[str]:
    """Return most-used MCU pins for this peripheral+MCU combination."""
    if not knowledge:
        return []
    for conn in knowledge.get("mcu_connections", []):
        if mcu_part in conn.get("mcu", ""):
            return conn.get("common_pins", [])
    conns = knowledge.get("mcu_connections", [])
    return conns[0].get("common_pins", []) if conns else []


# ─── S-expression generators ──────────────────────────────────────────────────

def _uid() -> str:
    return str(_uuid.uuid4())


def gen_placed_symbol(lib_id: str, ref: str, value: str, footprint: str,
                      x: float, y: float, root_uuid: str,
                      unit: int = 1) -> str:
    sym_uuid = _uid()
    ref_dy  = -3.0   # reference label offset above centre
    val_dy  =  3.0   # value label offset below centre
    return (
        f'\n  (symbol (lib_id "{lib_id}")\n'
        f'    (at {x} {y} 0)\n'
        f'    (unit {unit})\n'
        f'    (exclude_from_sim no)\n'
        f'    (in_bom yes)\n'
        f'    (on_board yes)\n'
        f'    (dnp no)\n'
        f'    (fields_autoplaced yes)\n'
        f'    (uuid "{sym_uuid}")\n'
        f'    (property "Reference" "{ref}"\n'
        f'      (at {x} {round(y + ref_dy, 2)} 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (property "Value" "{value}"\n'
        f'      (at {x} {round(y + val_dy, 2)} 0)\n'
        f'      (effects (font (size 1.27 1.27)))\n'
        f'    )\n'
        f'    (property "Footprint" "{footprint}"\n'
        f'      (at {x} {y} 0)\n'
        f'      (effects (font (size 1.27 1.27)) (hide yes))\n'
        f'    )\n'
        f'    (instances\n'
        f'      (project "project"\n'
        f'        (path "/{root_uuid}"\n'
        f'          (reference "{ref}") (unit {unit})\n'
        f'        )\n'
        f'      )\n'
        f'    )\n'
        f'  )'
    )


def gen_net_label(net: str, x: float, y: float, angle: float = 0.0) -> str:
    return (
        f'\n  (label "{net}"\n'
        f'    (at {x} {y} {int(angle)})\n'
        f'    (fields_autoplaced yes)\n'
        f'    (effects (font (size 1.27 1.27)) (justify left bottom))\n'
        f'    (uuid "{_uid()}")\n'
        f'  )'
    )


def gen_text(text: str, x: float, y: float, size: float = 1.27) -> str:
    return (
        f'\n  (text "{text}"\n'
        f'    (at {x} {y} 0)\n'
        f'    (effects (font (size {size} {size})))\n'
        f'    (uuid "{_uid()}")\n'
        f'  )'
    )


def gen_no_connect(x: float, y: float) -> str:
    return (
        f'\n  (no_connect (at {x} {y}) (uuid "{_uid()}"))'
    )


# ─── Schematic file ───────────────────────────────────────────────────────────

def build_schematic(root_uuid: str, lib_syms: str, body: str) -> str:
    return (
        f'(kicad_sch\n'
        f'  (version 20250114)\n'
        f'  (generator "kicad_sch_gen")\n'
        f'  (generator_version "9.0")\n'
        f'  (uuid "{root_uuid}")\n'
        f'  (paper "A3")\n'
        f'  (lib_symbols\n'
        f'{lib_syms}\n'
        f'  )\n'
        f'{body}\n'
        f'  (sheet_instances\n'
        f'    (path "/"\n'
        f'      (page "1")\n'
        f'    )\n'
        f'  )\n'
        f')\n'
    )


# ─── Project file ─────────────────────────────────────────────────────────────

def build_pro(project_name: str) -> str:
    return (
        '{\n'
        '  "board": { "3dviewports": [], "design_settings": {}, '
        '"ipc2581": {}, "layer_presets": [], "viewports": [] },\n'
        '  "boards": [],\n'
        '  "cvpcb": { "equivalence_files": [] },\n'
        '  "libraries": { "pinned_footprint_libs": [], "pinned_symbol_libs": [] },\n'
        '  "meta": {\n'
        f'    "filename": "{project_name}.kicad_pro",\n'
        '    "version": 1\n'
        '  },\n'
        '  "net_settings": {\n'
        '    "classes": [{"bus_width": 12, "clearance": 0.2, '
        '"diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25, "diff_pair_width": 0.2, '
        '"line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1, '
        '"name": "Default", "pcb_color": "rgba(0, 0, 0, 0.000)", '
        '"schematic_color": "rgba(0, 0, 0, 0.000)", "track_width": 0.25, '
        '"via_diameter": 0.8, "via_drill": 0.4, "wire_width": 6}],\n'
        '    "net_colors": null, "netclass_assignments": null, "netclass_patterns": []\n'
        '  },\n'
        '  "pcbnew": { "last_paths": {}, "page_layout_descr_file": "" },\n'
        '  "schematic": {\n'
        '    "annotate_start_num": 0, "bom_export_filename": "", '
        '"bom_fmt_presets": [], "bom_fmt_settings": {}, "bom_presets": [], '
        '"bom_settings": {}, "connection_grid_size": 50, '
        '"default_bus_thickness": 12, "default_junction_size": 40, '
        '"default_line_thickness": 6, "default_net_thickness": 6, '
        '"default_text_size": 50, "drawing_sheet_file": "", '
        '"field_names": [], "intersheets_ref_own_page": false, '
        '"intersheets_ref_prefix": "", "intersheets_ref_short": false, '
        '"intersheets_ref_show": false, "intersheets_ref_suffix": "", '
        '"junction_size_choice": 3, "label_size_ratio": 0.375, '
        '"net_format_name": "", "ngspice_settings": {}, '
        '"op_point_scale": 1.0, "page_layout_descr_file": "", '
        '"pin_symbol_size": 25, "plot_directory": "", '
        '"show_hidden_fields": false, "show_hidden_pins": false, '
        '"show_net_name_of_no_connect": true, "show_page_limits": true, '
        '"sim_plugin_name": "ngspice", "spice_current_sheet_as_root": false, '
        '"subpart_first_choice": 1, "subpart_id_separator": 0, '
        '"theme_name": "", "zoom_last_window_state": {}\n'
        '  },\n'
        '  "sheets": [],\n'
        '  "text_variables": {}\n'
        '}\n'
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

def cmd_list_pins(part: str, prasad_dir: Path, extra_dirs: list[Path],
                  multi_lib_dirs: list[Path] | None = None) -> int:
    result = find_sym_file(part, prasad_dir, extra_dirs, multi_lib_dirs)
    if not result:
        print(f"Symbol not found for part: {part}")
        return 1
    sym_file, sym_name, _ = result
    r = extract_symbol_text(sym_file, sym_name)
    if not r:
        print(f"Could not parse symbol in {sym_file}")
        return 1
    sym_text, actual_name = r
    pins = get_symbol_pins(sym_text)
    # De-duplicate: show each unique (x,y) once, with name and number
    seen_pos: dict[tuple, list[str]] = {}
    for name, pos in sorted(pins.items()):
        if pos not in seen_pos:
            seen_pos[pos] = []
        seen_pos[pos].append(name)
    print(f"\nPins for {actual_name} ({sym_file.name}):\n")
    print(f"  {'Pin name':<22} {'Local (x, y)':<22} {'angle'}")
    print(f"  {'-'*22} {'-'*22} {'-'*5}")
    printed: set[tuple] = set()
    for name, (px, py, pangle) in sorted(pins.items()):
        # Only print each unique position once (keyed by name, skip pure number aliases)
        if not re.match(r'^\d+$', name):
            print(f"  {name:<22} ({px:8.3f}, {py:8.3f})  {pangle:6.1f}°")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Generate a KiCad schematic from a design spec + knowledge graph.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("design", nargs="?", help="Path to design.yaml")
    p.add_argument("--out", default="", help="Output directory (default: ~/Documents/kicad/<project>)")
    p.add_argument("--knowledge-dir", dest="knowledge_dir", default="",
                   help="Knowledge graph dir (default: ~/kc/kicad-knowledge)")
    p.add_argument("--footprint-dir", dest="footprint_dir", default="",
                   help="PRASAD footprint dir")
    p.add_argument("--download-dir", dest="download_dir", default="",
                   help="Downloaded parts dir (for parts not in PRASAD)")
    p.add_argument("--multi-lib-dir", dest="multi_lib_dirs", action="append",
                   default=[], metavar="DIR",
                   help="Directory of shared .kicad_sym libraries (e.g. "
                        "/usr/share/kicad/symbols). Repeatable.")
    p.add_argument("--fp-index", dest="fp_index", default="",
                   help=f"Footprint-usage YAML (default {DEFAULT_FP_INDEX})")
    p.add_argument("--stock-fp-dir", dest="stock_fp_dirs", action="append",
                   default=[], metavar="DIR",
                   help="Directory containing *.pretty footprint libs. "
                        "Repeatable. Defaults: /usr/share/kicad/footprints + "
                        "~/Documents/PRASAD/05326/Footprint.")
    p.add_argument("--list-pins", dest="list_pins", default="",
                   metavar="PART",
                   help="Print pins for PART and exit (use to plan connections)")
    args = p.parse_args()

    prasad_dir    = Path(args.footprint_dir) if args.footprint_dir else DEFAULT_PRASAD_DIR
    download_dir  = Path(args.download_dir)  if args.download_dir  else DEFAULT_DOWNLOAD_DIR
    knowledge_dir = Path(args.knowledge_dir) if args.knowledge_dir else DEFAULT_KNOWLEDGE_DIR
    extra_dirs    = [download_dir]
    multi_lib_dirs = ([Path(d) for d in args.multi_lib_dirs]
                      if args.multi_lib_dirs else list(DEFAULT_MULTI_LIB_DIRS))
    fp_index_path = Path(args.fp_index) if args.fp_index else DEFAULT_FP_INDEX
    fp_index      = load_fp_index(fp_index_path)
    stock_fp_dirs = ([Path(d) for d in args.stock_fp_dirs]
                     if args.stock_fp_dirs else list(DEFAULT_STOCK_FP_DIRS))

    # --list-pins mode
    if args.list_pins:
        return cmd_list_pins(args.list_pins, prasad_dir, extra_dirs, multi_lib_dirs)

    if not args.design:
        p.error("design.yaml is required (or use --list-pins PART)")

    design = yaml.safe_load(Path(args.design).read_text(encoding="utf-8"))
    project_name = design["project"]

    out_dir = Path(args.out) if args.out else DEFAULT_PROJECT_DIR / project_name
    out_dir.mkdir(parents=True, exist_ok=True)

    root_uuid = _uid()
    lib_sym_blocks: list[str] = []        # embedded symbol definitions
    lib_sym_seen:   set[str]  = set()     # dedup by lib_id
    body_parts:     list[str] = []        # placed symbols, labels, text
    missing:        list[str] = []        # parts not found in any library
    connection_notes: list[str] = []      # human-readable summary

    # ── MCU ──────────────────────────────────────────────────────────────────
    mcu_cfg  = design.get("mcu", {})
    mcu_part = mcu_cfg.get("part", "")
    mcu_ref  = mcu_cfg.get("ref", "U1")
    mcu_x, mcu_y = 70.0, 148.0           # placed left-centre on A3
    mcu_pins: dict[str, tuple[float, float, float]] = {}

    if mcu_part:
        r = find_sym_file(mcu_part, prasad_dir, extra_dirs, multi_lib_dirs)
        if r:
            sym_file, sym_name, lib_name = r
            extracted = extract_symbol_text(sym_file, sym_name)
            if extracted:
                sym_text, actual_name = extracted
                lib_id = f"{lib_name}:{actual_name}"
                if lib_id not in lib_sym_seen:
                    lib_sym_seen.add(lib_id)
                    lib_sym_blocks.append(embed_symbol(sym_text, actual_name, lib_name))
                footprint = get_footprint_from_symbol(sym_text)
                if not footprint:
                    footprint = lookup_footprint(mcu_part, mcu_part, fp_index)
                    if footprint:
                        print(f"    (footprint from history: {footprint})")
                if not footprint:
                    footprint = lookup_stock_footprint(sym_text, stock_fp_dirs)
                    if footprint:
                        print(f"    (footprint from stock libs: {footprint})")
                body_parts.append(gen_placed_symbol(
                    lib_id, mcu_ref, mcu_part, footprint,
                    mcu_x, mcu_y, root_uuid,
                ))
                mcu_pins = get_symbol_pins(sym_text)
                body_parts.append(gen_text(f"MCU: {mcu_part}", mcu_x, mcu_y - 48, 1.5))
                print(f"  [OK] MCU {mcu_part} -> {lib_id}")
        else:
            missing.append(mcu_part)
            body_parts.append(gen_text(
                f"MCU: {mcu_part} [SYMBOL MISSING — download to {download_dir}]",
                mcu_x, mcu_y - 48,
            ))
            print(f"  [MISSING] MCU {mcu_part}")

    # ── Peripherals ───────────────────────────────────────────────────────────
    peri_x      = 290.0     # right column
    peri_y_base = 50.0
    peri_y_step = 90.0

    for i, peri_cfg in enumerate(design.get("peripherals", [])):
        peri_part  = peri_cfg.get("part", "")
        peri_ref   = peri_cfg.get("ref", f"U{i + 2}")
        peri_role  = peri_cfg.get("role", "")
        peri_name  = peri_cfg.get("name", peri_part)
        explicit   = peri_cfg.get("connections", {})   # {mcu_pin: peri_pin}

        px = peri_x
        py = peri_y_base + i * peri_y_step

        body_parts.append(gen_text(peri_name, px, py - 22, 1.5))

        r = find_sym_file(peri_part, prasad_dir, extra_dirs, multi_lib_dirs)
        if r:
            sym_file, sym_name, lib_name = r
            extracted = extract_symbol_text(sym_file, sym_name)
            if extracted:
                sym_text, actual_name = extracted
                lib_id = f"{lib_name}:{actual_name}"
                if lib_id not in lib_sym_seen:
                    lib_sym_seen.add(lib_id)
                    lib_sym_blocks.append(embed_symbol(sym_text, actual_name, lib_name))
                footprint = get_footprint_from_symbol(sym_text)
                if not footprint:
                    footprint = lookup_footprint(peri_part, peri_part, fp_index)
                    if footprint:
                        print(f"    (footprint from history: {footprint})")
                if not footprint:
                    footprint = lookup_stock_footprint(sym_text, stock_fp_dirs)
                    if footprint:
                        print(f"    (footprint from stock libs: {footprint})")
                body_parts.append(gen_placed_symbol(
                    lib_id, peri_ref, peri_part, footprint,
                    px, py, root_uuid,
                ))
                peri_pins = get_symbol_pins(sym_text)
                print(f"  [OK] {peri_name} ({peri_part}) -> {lib_id}")

                if explicit:
                    # Place net labels at both ends for every explicit connection
                    for mcu_pin, peri_pin in explicit.items():
                        net_name = mcu_pin   # use MCU pin name as net name

                        # Wire stub + label at MCU pin (label offset outward for readability)
                        if mcu_pin in mcu_pins:
                            lpx, lpy, lpa = mcu_pins[mcu_pin]
                            wx, wy, wdx, wdy = pin_world_with_outward(
                                lpx, lpy, lpa, mcu_x, mcu_y)
                            body_parts.append(gen_stub_and_label(
                                net_name, wx, wy, wdx, wdy))

                        # Wire stub + label at peripheral pin
                        if peri_pin in peri_pins:
                            ppx, ppy, ppa = peri_pins[peri_pin]
                            wx, wy, wdx, wdy = pin_world_with_outward(
                                ppx, ppy, ppa, px, py)
                            body_parts.append(gen_stub_and_label(
                                net_name, wx, wy, wdx, wdy))
                        else:
                            print(f"    WARN: pin '{peri_pin}' not found in {peri_part} "
                                  f"(available: {', '.join(sorted(peri_pins)[:10])})")

                        connection_notes.append(f"  {peri_ref}.{peri_pin} <-> {mcu_ref}.{mcu_pin}  [{net_name}]")
                else:
                    # No explicit connections — use knowledge graph hints and add a note
                    knowledge = load_block_knowledge(knowledge_dir, peri_role) if peri_role else None
                    suggested = get_suggested_mcu_pins(knowledge, mcu_part) if knowledge else []
                    if suggested:
                        hint_text = "Suggested MCU pins: " + ", ".join(suggested[:8])
                        body_parts.append(gen_text(hint_text, px, py + 25, 0.9))
                        connection_notes.append(
                            f"  {peri_ref} ({peri_part}): {hint_text}"
                        )

                # List available peripheral pins as a note
                pin_list = ", ".join(
                    n for n in sorted(peri_pins)
                    if not re.match(r'^\d+$', n)
                )
                body_parts.append(gen_text(f"Pins: {pin_list}", px, py + 18, 0.8))

        else:
            missing.append(peri_part)
            body_parts.append(gen_text(
                f"{peri_part} [SYMBOL MISSING — download to {download_dir}]",
                px, py,
            ))
            print(f"  [MISSING] {peri_name} ({peri_part})")

            # Still show knowledge-graph hints
            if peri_role:
                knowledge = load_block_knowledge(knowledge_dir, peri_role)
                suggested = get_suggested_mcu_pins(knowledge, mcu_part) if knowledge else []
                if suggested:
                    hint_text = "Suggested MCU pins: " + ", ".join(suggested[:8])
                    body_parts.append(gen_text(hint_text, px, py + 8, 0.9))

    # ── Assemble ──────────────────────────────────────────────────────────────
    lib_sym_section = "\n".join(f"  {s.strip()}" for s in lib_sym_blocks)
    body            = "\n".join(body_parts)

    sch_content = build_schematic(root_uuid, lib_sym_section, body)
    pro_content = build_pro(project_name)

    sch_path = out_dir / f"{project_name}.kicad_sch"
    pro_path = out_dir / f"{project_name}.kicad_pro"
    sch_path.write_text(sch_content, encoding="utf-8")
    pro_path.write_text(pro_content, encoding="utf-8")

    print(f"\nProject created:")
    print(f"  {pro_path}")
    print(f"  {sch_path}")

    if connection_notes:
        print(f"\nNet connections applied:")
        for note in connection_notes:
            print(note)

    if missing:
        print(f"\nMISSING symbols ({len(missing)}) — not found in library:")
        for m in missing:
            print(f"  {m}")
        print(f"\nDownload symbols to: {download_dir}")
        print("Then re-run this script.  The kicad-lib-add skill can register them.")
        return 2   # partial success

    print("\nOpen the .kicad_pro in KiCad 9.0 to review and refine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
