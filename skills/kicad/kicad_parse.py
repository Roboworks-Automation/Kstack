#!/usr/bin/env python3
"""
KiCad Schematic Parser — standalone CLI tool
Works with KiCad 6+ S-expression (.kicad_sch) format.
Supports hierarchical schematics (multi-sheet projects).

Usage:
    python kicad_parse.py <schematic.kicad_sch> [--format summary|json|nets|components]

Output formats:
    summary     (default) Human-readable full net + component table
    json        Machine-readable JSON for programmatic use
    nets        Nets only, compact
    components  Component list only
"""

import sys
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

try:
    from kiutils.schematic import Schematic
except ImportError:
    print("ERROR: kiutils not installed. Run: pip install kiutils", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

SNAP = 2  # Rounding precision for position comparison (~0.01 mm)


def rpos(x: float, y: float) -> tuple:
    return (round(x, SNAP), round(y, SNAP))


def pin_world_pos(px: float, py: float,
                  cx: float, cy: float, angle: float) -> tuple:
    """Transform a pin's local symbol coordinates to schematic world coordinates.

    KiCad library symbols use Y-up; schematic world uses Y-down.
    Transform: standard CCW rotation by `angle`, then negate Y.
    """
    a = math.radians(angle)
    cos_a, sin_a = math.cos(a), math.sin(a)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    return rpos(cx + rx, cy - ry)


def _on_segment(px: float, py: float,
                x1: float, y1: float, x2: float, y2: float,
                tol: float = 0.15) -> bool:
    """True if (px, py) lies on the line segment (x1,y1)-(x2,y2)."""
    if not (min(x1, x2) - tol <= px <= max(x1, x2) + tol and
            min(y1, y2) - tol <= py <= max(y1, y2) + tol):
        return False
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    seg_len = math.hypot(x2 - x1, y2 - y1)
    return abs(cross) <= tol * max(seg_len, 1.0)


# ---------------------------------------------------------------------------
# Single-sheet core parser
# ---------------------------------------------------------------------------

def parse_sheet(path: Path) -> Dict:
    """
    Parse a single .kicad_sch file and return a structured connectivity dict.

    Returns:
      {
        "schematic": str,           # filename
        "stats": {...},
        "labels": [str, ...],       # local net label texts
        "global_labels": [str, ...],# global label texts (without leading /)
        "power_nets": {str, ...},   # set of net names from power symbols
        "components": [
          {
            "ref": str, "value": str, "lib_id": str,
            "footprint": str, "description": str,
            "position": (x, y), "angle": float,
            "pins": {"num:name": "net_name", ...}
          }, ...
        ],
        "nets": {
          "net_name": ["REF.pin_num:pin_name", ...],   # sorted
          ...
        }
      }
    """
    sch = Schematic.from_file(str(path))

    # --- Build lib-symbol lookup: lib_id -> {pin_number -> (SymbolPin, unit_id)} ---
    lib_pin_map: Dict[str, Dict[str, tuple]] = {}
    for ls in sch.libSymbols:
        lib_id = (f"{ls.libraryNickname}:{ls.entryName}"
                  if ls.libraryNickname else ls.entryName)
        pins_by_num: Dict[str, tuple] = {}
        for unit in ls.units:
            uid = unit.unitId if unit.unitId is not None else 0
            for pin in unit.pins:
                pins_by_num[pin.number] = (pin, uid)
        lib_pin_map[lib_id] = pins_by_num

    # --- Pre-index wire segments ---
    wire_segments = []
    for item in sch.graphicalItems:
        if item.type == "wire" and len(item.points) >= 2:
            x1, y1 = item.points[0].X, item.points[0].Y
            x2, y2 = item.points[1].X, item.points[1].Y
            wire_segments.append((x1, y1, x2, y2, rpos(x1, y1), rpos(x2, y2)))

    # --- Union-Find ---
    parent: Dict[tuple, tuple] = {}

    def find(x):
        if x not in parent:
            parent[x] = x
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def attach_to_wires(px: float, py: float) -> tuple:
        """Union the point with any wire it lies on; return its rounded key."""
        key = rpos(px, py)
        find(key)
        for x1, y1, x2, y2, ep1, ep2 in wire_segments:
            if _on_segment(px, py, x1, y1, x2, y2):
                union(key, ep1)
                union(key, ep2)
                break
        return key

    # Seed wire endpoints
    for x1, y1, x2, y2, ep1, ep2 in wire_segments:
        find(ep1); find(ep2); union(ep1, ep2)

    # Junctions
    for j in sch.junctions:
        find(rpos(j.position.X, j.position.Y))

    # --- Labels + label-name unioning ---
    pos_to_label: Dict[tuple, str] = {}
    label_name_to_keys: Dict[str, List[tuple]] = defaultdict(list)

    def register_label(x: float, y: float, name: str):
        key = attach_to_wires(x, y)
        pos_to_label[key] = name
        label_name_to_keys[name].append(key)

    for lbl in sch.labels:
        register_label(lbl.position.X, lbl.position.Y, lbl.text)
    for glbl in sch.globalLabels:
        register_label(glbl.position.X, glbl.position.Y, f"/{glbl.text}")

    # Power symbols (#PWR*) — Value property is the net name
    power_nets: set = set()
    for sym in sch.schematicSymbols:
        ref = next((p.value for p in sym.properties if p.key == "Reference"), "")
        if not (ref.startswith("#PWR") or sym.libId.startswith("power:")):
            continue
        value = next((p.value for p in sym.properties if p.key == "Value"), None)
        if not value:
            continue
        lib_pins_e = lib_pin_map.get(sym.libId, {})
        sym_unit = sym.unit or 0
        for pin_num in sym.pins:
            entry = lib_pins_e.get(pin_num)
            if entry is None:
                continue
            lib_pin, pin_unit_id = entry
            if pin_unit_id != 0 and pin_unit_id != sym_unit:
                continue
            tip_raw = pin_world_pos(lib_pin.position.X, lib_pin.position.Y,
                                    sym.position.X, sym.position.Y,
                                    sym.position.angle or 0)
            register_label(tip_raw[0], tip_raw[1], value)
            power_nets.add(value)

    # Union same-name labels (KiCad rule: same name = same net on one sheet)
    for name, keys in label_name_to_keys.items():
        for k in keys[1:]:
            union(keys[0], k)

    # Map roots to net names
    root_to_net: Dict[tuple, str] = {}
    for pos, name in pos_to_label.items():
        root = find(pos)
        if root not in root_to_net:
            root_to_net[root] = name

    def net_name_for_root(root) -> str:
        if root is None:
            return "?"
        r = find(root)
        name = root_to_net.get(r)
        if name:
            return name
        return f"net_{r[0]}_{r[1]}"

    # --- First pass: collect all pin world positions ---
    all_sym_pins: List[tuple] = []
    for sym in sch.schematicSymbols:
        ref   = next((p.value for p in sym.properties if p.key == "Reference"), "?")
        value = next((p.value for p in sym.properties if p.key == "Value"), "?")
        fp    = next((p.value for p in sym.properties if p.key == "Footprint"), "")
        desc  = next((p.value for p in sym.properties if p.key == "Description"), "")
        cx, cy   = sym.position.X, sym.position.Y
        angle    = sym.position.angle or 0
        sym_unit = sym.unit or 0
        lib_pins = lib_pin_map.get(sym.libId, {})
        for pin_num in sym.pins:
            entry = lib_pins.get(pin_num)
            if entry is None:
                all_sym_pins.append((sym, ref, value, fp, desc, pin_num, None, None))
                continue
            lib_pin, pin_unit_id = entry
            if pin_unit_id != 0 and pin_unit_id != sym_unit:
                continue  # skip pins belonging to other units
            tip_raw = pin_world_pos(lib_pin.position.X, lib_pin.position.Y,
                                    cx, cy, angle)
            tip = attach_to_wires(tip_raw[0], tip_raw[1])
            all_sym_pins.append((sym, ref, value, fp, desc, pin_num, lib_pin, tip))

    # --- Build component + net structures ---
    net_to_pins: Dict[str, set] = defaultdict(set)
    sym_seen: Dict[int, Dict] = {}
    components: List[Dict] = []

    for (sym, ref, value, fp, desc, pin_num, lib_pin, tip) in all_sym_pins:
        sym_key = id(sym)
        if sym_key not in sym_seen:
            sym_seen[sym_key] = {
                "ref": ref, "value": value, "lib_id": sym.libId,
                "footprint": fp, "description": desc,
                "position": (sym.position.X, sym.position.Y),
                "angle": sym.position.angle or 0,
                "pins": {},
            }
            components.append(sym_seen[sym_key])

        comp = sym_seen[sym_key]
        if lib_pin is None:
            if pin_num not in comp["pins"]:
                comp["pins"][pin_num] = "?"
            continue

        root = find(tip)
        net_name = net_name_for_root(root)
        pin_label = lib_pin.name if lib_pin.name != "~" else pin_num
        pin_key = f"{pin_num}:{pin_label}"
        if pin_key not in comp["pins"]:
            comp["pins"][pin_key] = net_name
            net_to_pins[net_name].add(f"{ref}.{pin_key}")

    components.sort(key=lambda c: c["ref"])

    return {
        "schematic": path.name,
        "stats": {
            "components": len(components),
            "wires":      sum(1 for i in sch.graphicalItems if i.type == "wire"),
            "labels":     len(sch.labels),
            "junctions":  len(sch.junctions),
            "no_connects": len(sch.noConnects),
        },
        "labels":        sorted(set(l.text for l in sch.labels)),
        "global_labels": sorted(set(g.text for g in sch.globalLabels)),
        "power_nets":    power_nets,
        "components":    components,
        "nets":          {k: sorted(v) for k, v in net_to_pins.items()},
    }


# ---------------------------------------------------------------------------
# Hierarchical parser  (wraps parse_sheet for multi-sheet projects)
# ---------------------------------------------------------------------------

def _collect_subsheet_files(sch_path: Path, seen: set) -> List[Path]:
    """Recursively collect all sub-sheet file paths referenced by sch_path."""
    key = str(sch_path.resolve())
    if key in seen:
        return []
    seen.add(key)
    result = [sch_path]
    try:
        sch = Schematic.from_file(str(sch_path))
        for sheet in sch.sheets:
            fn = sheet.fileName.value if hasattr(sheet.fileName, "value") else str(sheet.fileName)
            sub = sch_path.parent / fn
            if sub.exists():
                result.extend(_collect_subsheet_files(sub, seen))
    except Exception as e:
        print(f"WARNING: could not read sub-sheet {sch_path}: {e}", file=sys.stderr)
    return result


def parse(path: Path) -> Dict:
    """
    Parse a .kicad_sch file.  If the file contains hierarchical sub-sheets,
    all sub-sheets are loaded and merged into a single flat connectivity model.

    Global labels (e.g. /GND, /3V3) and power-symbol nets (GND, 3V3) unify
    across sheets.  Local labels and unlabeled internal nodes are scoped to
    their sheet (net name prefixed with  <SheetName>::).

    Returns the same dict shape as before, plus:
      "sheets": [str, ...]   list of .kicad_sch filenames that were merged
    """
    sch_root = Schematic.from_file(str(path))
    has_subsheets = bool(sch_root.sheets)

    if not has_subsheets:
        data = parse_sheet(path)
        data["sheets"] = [path.name]
        return data

    # --- Hierarchical: parse every sheet ---
    seen: set = set()
    all_files = _collect_subsheet_files(path, seen)

    all_sheet_data: List[Dict] = []
    for sheet_path in all_files:
        try:
            d = parse_sheet(sheet_path)
        except Exception as e:
            print(f"WARNING: failed to parse {sheet_path.name}: {e}", file=sys.stderr)
            continue
        d["_sheet_stem"] = sheet_path.stem   # e.g. "DIO", "RS485"
        all_sheet_data.append(d)

    # --- Determine which net names are global (cross-sheet) ---
    # 1. Nets starting with "/" → always global (from global_label)
    # 2. Power symbol nets that appear in ≥2 sheets → global
    # 3. Power symbol nets that appear in only 1 sheet → still treat as global
    #    (KiCad power symbols are globally shared by definition)
    global_net_names: set = set()
    power_net_names: set = set()
    for d in all_sheet_data:
        power_net_names.update(d.get("power_nets", set()))
    global_net_names.update(power_net_names)

    # Any net beginning with "/" is from a global_label → global
    for d in all_sheet_data:
        for net_name in d["nets"]:
            if net_name.startswith("/"):
                global_net_names.add(net_name)

    # --- Merge sheets ---
    merged_components: List[Dict] = []
    merged_nets: Dict[str, set] = defaultdict(set)
    total_stats = {
        "components": 0, "wires": 0, "labels": 0,
        "junctions": 0, "no_connects": 0,
    }

    for d in all_sheet_data:
        stem = d["_sheet_stem"]
        total_stats["components"]  += d["stats"]["components"]
        total_stats["wires"]       += d["stats"]["wires"]
        total_stats["labels"]      += d["stats"]["labels"]
        total_stats["junctions"]   += d["stats"]["junctions"]
        total_stats["no_connects"] += d["stats"]["no_connects"]

        # Build rename map: local/unlabeled nets → scoped name
        net_rename: Dict[str, str] = {}
        for net_name in d["nets"]:
            if net_name in global_net_names:
                net_rename[net_name] = net_name          # keep global name
            elif net_name.startswith("net_"):
                net_rename[net_name] = f"{stem}::{net_name}"  # scope unlabeled
            else:
                net_rename[net_name] = f"{stem}::{net_name}"  # scope local label

        # Merge nets
        for net_name, pins in d["nets"].items():
            new_name = net_rename[net_name]
            merged_nets[new_name].update(pins)

        # Merge components (update pin→net mappings to use renamed net names)
        for comp in d["components"]:
            new_comp = dict(comp)
            new_comp["_sheet"] = stem
            new_comp["pins"] = {
                pin_key: net_rename.get(net_val, net_val)
                for pin_key, net_val in comp["pins"].items()
            }
            merged_components.append(new_comp)

    merged_components.sort(key=lambda c: (c["ref"], c.get("_sheet", "")))

    all_labels        = sorted(set(l for d in all_sheet_data for l in d.get("labels", [])))
    all_global_labels = sorted(set(l for d in all_sheet_data for l in d.get("global_labels", [])))

    return {
        "schematic": path.name,
        "sheets":    [d["schematic"] for d in all_sheet_data],
        "stats":     total_stats,
        "labels":         all_labels,
        "global_labels":  all_global_labels,
        "components":     merged_components,
        "nets":           {k: sorted(v) for k, v in merged_nets.items()},
    }


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def fmt_summary(data: Dict) -> str:
    sheets = data.get("sheets", [data["schematic"]])
    lines = [
        f"# KiCad Schematic: {data['schematic']}",
        "",
    ]
    if len(sheets) > 1:
        lines += [
            f"## Sheets ({len(sheets)} total)",
        ]
        for s in sheets:
            lines.append(f"  - {s}")
        lines.append("")

    lines += [
        "## Stats",
        f"  Components : {data['stats']['components']}",
        f"  Wires      : {data['stats']['wires']}",
        f"  Net labels : {data['stats']['labels']}",
        f"  Junctions  : {data['stats']['junctions']}",
        f"  No-connects: {data['stats']['no_connects']}",
        "",
        f"## Named nets ({len([k for k in data['nets'] if not k.startswith('net_') and '::net_' not in k])})",
    ]
    for net in sorted(k for k in data["nets"]
                      if not k.startswith("net_") and "::net_" not in k):
        pins = data["nets"][net]
        lines.append(f"  {net:20s}: {', '.join(pins)}")

    unlabeled = [k for k in data["nets"] if k.startswith("net_") or "::net_" in k]
    lines += [
        "",
        f"## Unlabeled internal nets ({len(unlabeled)})",
        "  (format: [Sheet::]net_X_Y where X,Y are schematic coordinates)",
    ]
    for net in sorted(unlabeled):
        pins = data["nets"][net]
        lines.append(f"  {net:36s}: {', '.join(pins)}")

    lines += ["", "## Components — Pin Connections", ""]
    for comp in data["components"]:
        pos = comp["position"]
        sheet_tag = f"  [sheet: {comp['_sheet']}]" if "_sheet" in comp else ""
        lines.append(f"### {comp['ref']}  ({comp['value']}){sheet_tag}")
        lines.append(f"  Library  : {comp['lib_id']}")
        if comp["description"]:
            lines.append(f"  Desc     : {comp['description']}")
        if comp["footprint"]:
            lines.append(f"  Footprint: {comp['footprint']}")
        lines.append(f"  Position : ({pos[0]:.2f}, {pos[1]:.2f})  angle={comp['angle']}°")
        for pin_key, net in sorted(comp["pins"].items()):
            lines.append(f"    {pin_key:30s} → {net}")
        lines.append("")

    return "\n".join(lines)


def fmt_nets(data: Dict) -> str:
    lines = [f"# Nets — {data['schematic']}", ""]
    for net in sorted(data["nets"]):
        lines.append(f"{net}: {', '.join(data['nets'][net])}")
    return "\n".join(lines)


def fmt_components(data: Dict) -> str:
    lines = [f"# Components — {data['schematic']}", ""]
    for c in data["components"]:
        pos = c["position"]
        sheet = f"  [{c['_sheet']}]" if "_sheet" in c else ""
        lines.append(
            f"{c['ref']:8s}  {c['value']:20s}  {c['lib_id']:40s}"
            f"  ({pos[0]:.1f}, {pos[1]:.1f}){sheet}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)
    if not path.suffix == ".kicad_sch":
        print(f"WARNING: expected .kicad_sch, got {path.suffix}", file=sys.stderr)

    fmt = "summary"
    for arg in sys.argv[2:]:
        if arg.startswith("--format="):
            fmt = arg.split("=", 1)[1]
        elif arg == "--format" and len(sys.argv) > sys.argv.index(arg) + 1:
            fmt = sys.argv[sys.argv.index(arg) + 1]
        elif arg in ("json", "summary", "nets", "components"):
            fmt = arg

    data = parse(path)

    if fmt == "json":
        # Convert tuples to lists for JSON serialisation
        for comp in data["components"]:
            comp["position"] = list(comp["position"])
        # power_nets set is not in top-level, safe to serialise
        print(json.dumps(data, indent=2))
    elif fmt == "nets":
        print(fmt_nets(data))
    elif fmt == "components":
        print(fmt_components(data))
    else:
        print(fmt_summary(data))


if __name__ == "__main__":
    main()
