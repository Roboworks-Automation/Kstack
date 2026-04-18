#!/usr/bin/env python3
"""
KiCad Schematic Edit Helper — applies simple connectivity changes.

All edits are done as targeted raw text insertions/deletions so the file
is never round-tripped through kiutils (which would corrupt property
visibility flags and reset reference designators).

Usage:
    python kicad_apply.py <schematic.kicad_sch> <command> [options]

Commands:
    add-label   --text NAME --x X --y Y [--angle 0]
                  Add a net label at (X, Y).

    remove-label  --text NAME --x X --y Y
                  Remove a label with text NAME near position (X, Y) (±0.5 mm).

    add-wire    --x1 X1 --y1 Y1 --x2 X2 --y2 Y2
                  Add a wire segment from (X1,Y1) to (X2,Y2).

    remove-wire --x1 X1 --y1 Y1 --x2 X2 --y2 Y2
                  Remove the wire segment between those endpoints (±0.1 mm).

    list-labels
                  Print all label positions and texts.

    list-wires
                  Print all wire segments.

Examples:
    python kicad_apply.py board.kicad_sch add-label --text GND --x 214.63 --y 102.87
    python kicad_apply.py board.kicad_sch add-wire --x1 100 --y1 50 --x2 120 --y2 50

Notes:
    - Always backs up the schematic to <name>.kicad_sch.bak before writing.
    - KiCad uses millimetres. Grid is typically 1.27 mm or 2.54 mm.
    - After editing, open in KiCad and run ERC to validate.
"""

import sys
import re
import shutil
import argparse
import uuid as _uuid
from pathlib import Path


def _backup(path: Path):
    bak = path.with_suffix(".kicad_sch.bak")
    shutil.copy2(path, bak)
    print(f"Backed up to {bak}")


def _fresh_uuid() -> str:
    return str(_uuid.uuid4())


# ---------------------------------------------------------------------------
# Raw-text helpers  (no kiutils round-trip)
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str):
    path.write_text(content, encoding="utf-8")


def _insert_before_last_paren(content: str, block: str) -> str:
    """Insert `block` just before the final closing ) of the file."""
    pos = content.rfind("\n)")
    return content[:pos] + block + content[pos:]


# ---------------------------------------------------------------------------
# Label helpers
# ---------------------------------------------------------------------------

def _label_sexpr(text: str, x: float, y: float, angle: float) -> str:
    return (
        f'\n\t(label "{text}"\n'
        f'\t\t(at {x} {y} {angle})\n'
        f'\t\t(fields_autoplaced yes)\n'
        f'\t\t(effects\n'
        f'\t\t\t(font\n'
        f'\t\t\t\t(size 1.27 1.27)\n'
        f'\t\t\t)\n'
        f'\t\t\t(justify left bottom)\n'
        f'\t\t)\n'
        f'\t\t(uuid "{_fresh_uuid()}")\n'
        f'\t)'
    )


def _parse_labels(content: str):
    """Return list of (text, x, y, start_pos, end_pos) for all labels."""
    results = []
    for m in re.finditer(r'\(label\s+"([^"]+)"\s+\(at\s+([\d.]+)\s+([\d.]+)', content):
        text = m.group(1)
        x = float(m.group(2))
        y = float(m.group(3))
        # Find extent of this label block
        start = content.rfind("\n\t(label", 0, m.start() + 1)
        depth = 0
        end = start
        for i, c in enumerate(content[start:], start):
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        results.append((text, x, y, start, end))
    return results


def cmd_add_label(content: str, args) -> tuple:
    tol = 0.26
    for text, x, y, *_ in _parse_labels(content):
        if text == args.text and abs(x - args.x) < tol and abs(y - args.y) < tol:
            print(f"Label '{args.text}' already at ({args.x}, {args.y}). No change.")
            return content, False
    block = _label_sexpr(args.text, args.x, args.y, args.angle)
    content = _insert_before_last_paren(content, block)
    print(f"Added label '{args.text}' at ({args.x}, {args.y})")
    return content, True


def cmd_remove_label(content: str, args) -> tuple:
    tol = 0.5
    labels = _parse_labels(content)
    removed = 0
    # Process in reverse order so positions stay valid
    for text, x, y, start, end in reversed(labels):
        if text == args.text and abs(x - args.x) < tol and abs(y - args.y) < tol:
            content = content[:start] + content[end:]
            removed += 1
    if removed:
        print(f"Removed {removed} label(s) '{args.text}' near ({args.x}, {args.y})")
        return content, True
    print(f"No label '{args.text}' found near ({args.x}, {args.y})")
    return content, False


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------

def _wire_sexpr(x1: float, y1: float, x2: float, y2: float) -> str:
    return (
        f'\n\t(wire\n'
        f'\t\t(pts\n'
        f'\t\t\t(xy {x1} {y1})\n'
        f'\t\t\t(xy {x2} {y2})\n'
        f'\t\t)\n'
        f'\t\t(stroke\n'
        f'\t\t\t(width 0)\n'
        f'\t\t\t(type default)\n'
        f'\t\t)\n'
        f'\t\t(uuid "{_fresh_uuid()}")\n'
        f'\t)'
    )


def _parse_wires(content: str):
    """Return list of (x1,y1,x2,y2, start, end) for all wires."""
    results = []
    pattern = re.compile(
        r'\(wire\s+\(pts\s+\(xy\s+([\d.]+)\s+([\d.]+)\)\s+\(xy\s+([\d.]+)\s+([\d.]+)\)'
    )
    for m in pattern.finditer(content):
        x1, y1, x2, y2 = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))
        start = content.rfind("\n\t(wire", 0, m.start() + 1)
        depth = 0
        end = start
        for i, c in enumerate(content[start:], start):
            if c == "(": depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        results.append((x1, y1, x2, y2, start, end))
    return results


def cmd_add_wire(content: str, args) -> tuple:
    tol = 0.1
    for x1, y1, x2, y2, *_ in _parse_wires(content):
        if (abs(x1 - args.x1) < tol and abs(y1 - args.y1) < tol and
                abs(x2 - args.x2) < tol and abs(y2 - args.y2) < tol):
            print(f"Wire already exists. No change.")
            return content, False
        if (abs(x1 - args.x2) < tol and abs(y1 - args.y2) < tol and
                abs(x2 - args.x1) < tol and abs(y2 - args.y1) < tol):
            print(f"Wire already exists (reversed). No change.")
            return content, False
    block = _wire_sexpr(args.x1, args.y1, args.x2, args.y2)
    content = _insert_before_last_paren(content, block)
    print(f"Added wire ({args.x1},{args.y1}) → ({args.x2},{args.y2})")
    return content, True


def cmd_remove_wire(content: str, args) -> tuple:
    tol = 0.1
    wires = _parse_wires(content)
    removed = 0
    for x1, y1, x2, y2, start, end in reversed(wires):
        fwd = (abs(x1 - args.x1) < tol and abs(y1 - args.y1) < tol and
               abs(x2 - args.x2) < tol and abs(y2 - args.y2) < tol)
        rev = (abs(x1 - args.x2) < tol and abs(y1 - args.y2) < tol and
               abs(x2 - args.x1) < tol and abs(y2 - args.y1) < tol)
        if fwd or rev:
            content = content[:start] + content[end:]
            removed += 1
    if removed:
        print(f"Removed {removed} wire(s)")
        return content, True
    print("No matching wire found")
    return content, False


# ---------------------------------------------------------------------------
# List commands
# ---------------------------------------------------------------------------

def cmd_list_labels(content: str, _args) -> tuple:
    for text, x, y, *_ in sorted(_parse_labels(content), key=lambda r: (r[0], r[1])):
        print(f"  '{text}' at ({x:.2f}, {y:.2f})")
    return content, False


def cmd_list_wires(content: str, _args) -> tuple:
    for x1, y1, x2, y2, *_ in _parse_wires(content):
        print(f"  ({x1},{y1}) → ({x2},{y2})")
    return content, False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    sch_path = Path(sys.argv[1])
    if not sch_path.exists():
        print(f"ERROR: {sch_path} not found", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[2]
    rest = sys.argv[3:]
    content = _read(sch_path)

    p = argparse.ArgumentParser()
    if command == "list-labels":
        cmd_list_labels(content, None)
        return
    if command == "list-wires":
        cmd_list_wires(content, None)
        return

    if command == "add-label":
        p.add_argument("--text",  required=True)
        p.add_argument("--x",     required=True, type=float)
        p.add_argument("--y",     required=True, type=float)
        p.add_argument("--angle", default=0,     type=float)
        args = p.parse_args(rest)
        content, changed = cmd_add_label(content, args)
    elif command == "remove-label":
        p.add_argument("--text", required=True)
        p.add_argument("--x",   required=True, type=float)
        p.add_argument("--y",   required=True, type=float)
        args = p.parse_args(rest)
        content, changed = cmd_remove_label(content, args)
    elif command == "add-wire":
        p.add_argument("--x1", required=True, type=float)
        p.add_argument("--y1", required=True, type=float)
        p.add_argument("--x2", required=True, type=float)
        p.add_argument("--y2", required=True, type=float)
        args = p.parse_args(rest)
        content, changed = cmd_add_wire(content, args)
    elif command == "remove-wire":
        p.add_argument("--x1", required=True, type=float)
        p.add_argument("--y1", required=True, type=float)
        p.add_argument("--x2", required=True, type=float)
        p.add_argument("--y2", required=True, type=float)
        args = p.parse_args(rest)
        content, changed = cmd_remove_wire(content, args)
    else:
        print(f"ERROR: unknown command '{command}'", file=sys.stderr)
        sys.exit(1)

    if changed:
        _backup(sch_path)
        _write(sch_path, content)
        print(f"Saved: {sch_path}")


if __name__ == "__main__":
    main()
