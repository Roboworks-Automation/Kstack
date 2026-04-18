#!/usr/bin/env python3
"""
kicad_edgecut.py — extract and re-place PCB board outlines (Edge.Cuts)
across KiCad projects.

Subcommands:

    extract <path> [--out DIR]
        Walk every .kicad_pcb under <path>, pull Edge.Cuts graphics,
        write one YAML per project under <out> (default ~/kc/kicad-edgecuts/lib).

    list [--lib DIR]
        Print a table of extracted outlines: name, bbox, #items.

    place --from <name|path> --to <pcb> [--at X,Y] [--clear] [--layer LAYER]
        Append the saved outline to <pcb>. --at is the TOP-LEFT placement
        corner in mm (KiCad PCB origin is top-left; Y grows downward).
        point in mm (default 100,100 — safely inside the KiCad workspace).
        --clear first removes any existing items on the target layer.

Edge.Cuts primitives supported: line, arc, circle, rect, polygon.

Requires kiutils + PyYAML (already in env `kicad-agent`).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

try:
    from kiutils.board import Board
    from kiutils.items.common import Position
    from kiutils.items.gritems import GrLine, GrArc, GrCircle, GrRect, GrPoly
    from kiutils.items.brditems import LayerToken
except ImportError:
    print("ERROR: pip install kiutils", file=sys.stderr)
    sys.exit(2)


DEFAULT_LIB = Path.home() / "kc" / "kicad-edgecuts" / "lib"
DEFAULT_LAYER = "Edge.Cuts"

# Minimal 2-layer KiCad 9.0 stack — injected when target PCB has no
# `(layers ...)` block, which otherwise fails to open.
_MIN_LAYERS = [
    LayerToken(ordinal=0,  name="F.Cu",          type="signal"),
    LayerToken(ordinal=31, name="B.Cu",          type="signal"),
    LayerToken(ordinal=32, name="B.Adhes",       type="user",   userName="B.Adhesive"),
    LayerToken(ordinal=33, name="F.Adhes",       type="user",   userName="F.Adhesive"),
    LayerToken(ordinal=34, name="B.Paste",       type="user"),
    LayerToken(ordinal=35, name="F.Paste",       type="user"),
    LayerToken(ordinal=36, name="B.SilkS",       type="user",   userName="B.Silkscreen"),
    LayerToken(ordinal=37, name="F.SilkS",       type="user",   userName="F.Silkscreen"),
    LayerToken(ordinal=38, name="B.Mask",        type="user"),
    LayerToken(ordinal=39, name="F.Mask",        type="user"),
    LayerToken(ordinal=40, name="Dwgs.User",     type="user",   userName="User.Drawings"),
    LayerToken(ordinal=41, name="Cmts.User",     type="user",   userName="User.Comments"),
    LayerToken(ordinal=42, name="Eco1.User",     type="user",   userName="User.Eco1"),
    LayerToken(ordinal=43, name="Eco2.User",     type="user",   userName="User.Eco2"),
    LayerToken(ordinal=44, name="Edge.Cuts",     type="user"),
    LayerToken(ordinal=45, name="Margin",        type="user"),
    LayerToken(ordinal=46, name="B.CrtYd",       type="user",   userName="B.Courtyard"),
    LayerToken(ordinal=47, name="F.CrtYd",       type="user",   userName="F.Courtyard"),
    LayerToken(ordinal=48, name="B.Fab",         type="user"),
    LayerToken(ordinal=49, name="F.Fab",         type="user"),
]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _pos(p: Position) -> tuple[float, float]:
    return (float(p.X), float(p.Y))


def _serialize_item(g, offset: tuple[float, float]) -> dict[str, Any] | None:
    ox, oy = offset
    def norm(p):
        x, y = _pos(p)
        return [round(x - ox, 4), round(y - oy, 4)]

    t = type(g).__name__
    width = float(getattr(g, "width", 0.1) or 0.1)
    if isinstance(g, GrLine):
        return {"type": "line", "start": norm(g.start), "end": norm(g.end),
                "width": width}
    if isinstance(g, GrArc):
        return {"type": "arc", "start": norm(g.start), "mid": norm(g.mid),
                "end": norm(g.end), "width": width}
    if isinstance(g, GrCircle):
        return {"type": "circle", "center": norm(g.center), "end": norm(g.end),
                "width": width, "fill": (g.fill or "no")}
    if isinstance(g, GrRect):
        return {"type": "rect", "start": norm(g.start), "end": norm(g.end),
                "width": width, "fill": (g.fill or "no")}
    if isinstance(g, GrPoly):
        pts = [norm(p) for p in (g.coordinates or [])]
        return {"type": "poly", "points": pts, "width": width,
                "fill": (g.fill or "no")}
    return None


def _bbox_from_items(items: list[dict]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for it in items:
        if it["type"] == "line":
            xs += [it["start"][0], it["end"][0]]
            ys += [it["start"][1], it["end"][1]]
        elif it["type"] == "arc":
            xs += [it["start"][0], it["mid"][0], it["end"][0]]
            ys += [it["start"][1], it["mid"][1], it["end"][1]]
        elif it["type"] == "circle":
            cx, cy = it["center"]
            ex, ey = it["end"]
            r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        elif it["type"] == "rect":
            xs += [it["start"][0], it["end"][0]]
            ys += [it["start"][1], it["end"][1]]
        elif it["type"] == "poly":
            for p in it["points"]:
                xs.append(p[0]); ys.append(p[1])
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


def extract_from_pcb(pcb_path: Path, layer: str = DEFAULT_LAYER
                     ) -> dict[str, Any] | None:
    try:
        board = Board.from_file(str(pcb_path))
    except Exception as e:
        return {"error": f"parse failed: {e}", "source": str(pcb_path)}

    raw_items = [g for g in board.graphicItems
                 if getattr(g, "layer", None) == layer]
    if not raw_items:
        return None

    # First pass: collect absolute coords to find offset (board origin).
    tmp = [_serialize_item(g, (0.0, 0.0)) for g in raw_items]
    tmp = [t for t in tmp if t]
    if not tmp:
        return None
    min_x, min_y, max_x, max_y = _bbox_from_items(tmp)

    # Second pass: rewrite normalized to (0,0) lower-left.
    items = [_serialize_item(g, (min_x, min_y)) for g in raw_items]
    items = [t for t in items if t]

    width = round(max_x - min_x, 4)
    height = round(max_y - min_y, 4)
    return {
        "project": pcb_path.stem,
        "source": str(pcb_path),
        "layer": layer,
        "bbox": {
            "orig_min": [round(min_x, 4), round(min_y, 4)],
            "orig_max": [round(max_x, 4), round(max_y, 4)],
            "width": width,
            "height": height,
        },
        "item_count": len(items),
        "items": items,
    }


def cmd_extract(args: argparse.Namespace) -> int:
    root = args.path.expanduser().resolve()
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    if root.is_file() and root.suffix == ".kicad_pcb":
        pcbs = [root]
    else:
        pcbs = [p for p in root.rglob("*.kicad_pcb")
                if "backup" not in str(p).lower()
                and "__MACOSX" not in str(p)
                and "-backups" not in str(p)]
    pcbs.sort()

    ok = 0; empty = 0; fail = 0
    index: list[dict] = []
    seen_names: dict[str, int] = {}
    for pcb in pcbs:
        data = extract_from_pcb(pcb, args.layer)
        if data is None:
            empty += 1
            print(f"  (no {args.layer}) {pcb}")
            continue
        if "error" in data:
            fail += 1
            print(f"  FAIL {pcb}: {data['error']}")
            continue
        name = pcb.stem
        # Deduplicate on file name collisions
        if name in seen_names:
            seen_names[name] += 1
            name = f"{name}-{seen_names[name]}"
        else:
            seen_names[name] = 1
        data["name"] = name
        out_path = out / f"{name}.yaml"
        out_path.write_text(yaml.safe_dump(data, sort_keys=False),
                            encoding="utf-8")
        index.append({
            "name": name,
            "project": data["project"],
            "width_mm": data["bbox"]["width"],
            "height_mm": data["bbox"]["height"],
            "items": data["item_count"],
            "source": data["source"],
        })
        ok += 1
        b = data["bbox"]
        print(f"  {name}: {b['width']}×{b['height']} mm, "
              f"{data['item_count']} items")

    (out / "INDEX.yaml").write_text(yaml.safe_dump(
        {"edgecuts": index}, sort_keys=False), encoding="utf-8")

    # Human-readable index
    lines = ["# Edge.Cuts library",
             "",
             f"{len(index)} outlines extracted.",
             "",
             "| Name | Size (mm) | Items | Source |",
             "|---|---|---|---|"]
    for e in sorted(index, key=lambda x: x["name"]):
        lines.append(f"| {e['name']} | {e['width_mm']}×{e['height_mm']} |"
                     f" {e['items']} | `{e['source']}` |")
    (out / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print(f"{ok} extracted, {empty} without {args.layer}, {fail} failed.")
    print(f"Library: {out}")
    return 0


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    lib = args.lib.expanduser().resolve()
    index_path = lib / "INDEX.yaml"
    if not index_path.exists():
        print(f"No library at {lib}. Run `extract` first.", file=sys.stderr)
        return 1
    idx = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    entries = sorted(idx.get("edgecuts", []), key=lambda e: e["name"])
    if args.filter:
        needle = args.filter.lower()
        entries = [e for e in entries if needle in e["name"].lower()
                   or needle in e.get("project", "").lower()]
    print(f"{'Name':40}  {'Size(mm)':16}  {'Items':>5}")
    print("-" * 70)
    for e in entries:
        size = f"{e['width_mm']}×{e['height_mm']}"
        print(f"{e['name']:40}  {size:16}  {e['items']:>5}")
    print(f"\n{len(entries)} entries in {lib}")
    return 0


# ---------------------------------------------------------------------------
# Place
# ---------------------------------------------------------------------------

def _resolve_source(spec: str, lib: Path) -> Path:
    p = Path(spec).expanduser()
    if p.suffix == ".yaml" and p.exists():
        return p
    # name lookup
    cand = lib / f"{spec}.yaml"
    if cand.exists():
        return cand
    # case-insensitive
    matches = [f for f in lib.glob("*.yaml")
               if f.stem.lower() == spec.lower()]
    if matches:
        return matches[0]
    # substring
    matches = [f for f in lib.glob("*.yaml")
               if spec.lower() in f.stem.lower() and f.name != "INDEX.yaml"]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(f"Ambiguous '{spec}'. Matches: "
                         f"{', '.join(m.stem for m in matches)}")
    raise SystemExit(f"No edgecut '{spec}' in {lib}")


def _build_items(data: dict, origin: tuple[float, float], layer: str) -> list:
    ox, oy = origin
    out = []
    for it in data["items"]:
        t = it["type"]
        w = it.get("width", 0.1)
        if t == "line":
            sx, sy = it["start"]; ex, ey = it["end"]
            out.append(GrLine(
                start=Position(X=sx + ox, Y=sy + oy),
                end=Position(X=ex + ox, Y=ey + oy),
                layer=layer, width=w))
        elif t == "arc":
            sx, sy = it["start"]; mx, my = it["mid"]; ex, ey = it["end"]
            out.append(GrArc(
                start=Position(X=sx + ox, Y=sy + oy),
                mid=Position(X=mx + ox, Y=my + oy),
                end=Position(X=ex + ox, Y=ey + oy),
                layer=layer, width=w))
        elif t == "circle":
            cx, cy = it["center"]; ex, ey = it["end"]
            out.append(GrCircle(
                center=Position(X=cx + ox, Y=cy + oy),
                end=Position(X=ex + ox, Y=ey + oy),
                layer=layer, width=w, fill=it.get("fill", "no")))
        elif t == "rect":
            sx, sy = it["start"]; ex, ey = it["end"]
            out.append(GrRect(
                start=Position(X=sx + ox, Y=sy + oy),
                end=Position(X=ex + ox, Y=ey + oy),
                layer=layer, width=w, fill=it.get("fill", "no")))
        elif t == "poly":
            coords = [Position(X=p[0] + ox, Y=p[1] + oy) for p in it["points"]]
            out.append(GrPoly(layer=layer, coordinates=coords, width=w,
                              fill=it.get("fill", "no")))
    return out


def cmd_place(args: argparse.Namespace) -> int:
    src_path = _resolve_source(args.from_, args.lib.expanduser().resolve())
    data = yaml.safe_load(src_path.read_text(encoding="utf-8"))

    target = args.to.expanduser().resolve()
    if not target.exists():
        print(f"Target PCB not found: {target}", file=sys.stderr)
        return 1

    board = Board.from_file(str(target))
    layer = args.layer

    # Some near-empty PCBs (freshly created stubs) have no `(layers ...)`
    # block. KiCad refuses to open them. Inject a standard stack.
    if not board.layers:
        board.layers = list(_MIN_LAYERS)
        print(f"  injected {len(board.layers)} standard layers "
              f"(target had none)")

    if args.clear:
        before = len(board.graphicItems)
        board.graphicItems = [g for g in board.graphicItems
                              if getattr(g, "layer", None) != layer]
        print(f"  cleared {before - len(board.graphicItems)} existing "
              f"items on {layer}")

    ox, oy = args.at
    new_items = _build_items(data, (ox, oy), layer)
    board.graphicItems.extend(new_items)

    if not args.no_backup:
        bak = target.with_suffix(target.suffix + ".bak")
        if not bak.exists():
            bak.write_bytes(target.read_bytes())
            print(f"  backup: {bak}")

    board.to_file(str(target))
    b = data["bbox"]
    print(f"  placed '{data['name']}' ({b['width']}×{b['height']} mm, "
          f"{len(new_items)} items) at top-left=({ox}, {oy}) "
          f"bottom-right=({ox + b['width']}, {oy + b['height']}) on {layer}")
    print(f"  target: {target}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_xy(s: str) -> tuple[float, float]:
    parts = s.replace(" ", "").split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected X,Y, got {s!r}")
    return (float(parts[0]), float(parts[1]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="scan a tree of .kicad_pcb files")
    pe.add_argument("path", type=Path)
    pe.add_argument("--out", type=Path, default=DEFAULT_LIB)
    pe.add_argument("--layer", default=DEFAULT_LAYER)
    pe.set_defaults(func=cmd_extract)

    pl = sub.add_parser("list", help="list extracted outlines")
    pl.add_argument("--lib", type=Path, default=DEFAULT_LIB)
    pl.add_argument("--filter", default=None, help="substring filter")
    pl.set_defaults(func=cmd_list)

    pp = sub.add_parser("place", help="place an outline into a target .kicad_pcb")
    pp.add_argument("--from", dest="from_", required=True,
                    help="edgecut name (e.g. 'Neo') or path to yaml")
    pp.add_argument("--to", type=Path, required=True,
                    help="target .kicad_pcb to modify in place")
    pp.add_argument("--at", type=_parse_xy, default=(30.0, 30.0),
                    help="top-left corner of board bbox in mm, default 30,30 "
                         "(KiCad origin is top-left; Y grows downward)")
    pp.add_argument("--layer", default=DEFAULT_LAYER)
    pp.add_argument("--clear", action="store_true",
                    help="remove existing items on the target layer first")
    pp.add_argument("--lib", type=Path, default=DEFAULT_LIB)
    pp.add_argument("--no-backup", action="store_true")
    pp.set_defaults(func=cmd_place)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
