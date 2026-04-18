#!/usr/bin/env python3
"""
footprint_index.py — build a {component-value -> footprint} lookup
from every .kicad_pcb in a tree.

Answers questions like:
  "What footprint has the user historically picked for SN65HVD3082EDR?"
  "Which package have they used for AMS1117?"

Output: YAML at ~/kc/kicad-footprints/index.yaml

    entries:
      SN65HVD3082EDR:
        - footprint: Package_SO:SOIC-8_3.9x4.9mm_P1.27mm
          count: 14
          projects: [Neo, VacuumRS485, ...]
          refs:     [U3, U2, ...]
        - footprint: ...
      AMS1117-3.3:
        - ...

Also emits a markdown report at ~/kc/kicad-footprints/FOOTPRINTS.md.

Usage:
    python3 footprint_index.py <path> [--out DIR]

<path> can be /home/pc/Documents (scans both kicad/ and PRASAD/ trees).
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict, Counter
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pip install pyyaml", file=sys.stderr); sys.exit(2)

try:
    from kiutils.board import Board
except ImportError:
    print("ERROR: pip install kiutils", file=sys.stderr); sys.exit(2)


DEFAULT_OUT = Path.home() / "kc" / "kicad-footprints"


def _norm_value(v: str) -> str:
    v = (v or "").strip()
    if not v or v in {"~", "?"}:
        return ""
    return v


def scan(root: Path) -> dict[str, list[dict]]:
    # value -> (footprint_lib_id -> {count, projects, refs})
    idx: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"count": 0, "projects": set(), "refs": []}))
    pcbs = sorted(p for p in root.rglob("*.kicad_pcb")
                  if "backup" not in str(p).lower()
                  and "__MACOSX" not in str(p)
                  and "-backups" not in str(p))
    ok = 0; fail = 0
    for pcb in pcbs:
        try:
            board = Board.from_file(str(pcb))
        except Exception as e:
            print(f"  FAIL {pcb.name}: {e}", file=sys.stderr)
            fail += 1
            continue
        project = pcb.stem
        for fp in board.footprints:
            # kiutils: Footprint.libraryNickname + Footprint.entryName
            lib = getattr(fp, "libraryNickname", "") or ""
            ent = getattr(fp, "entryName", "") or ""
            lib_id = f"{lib}:{ent}" if lib else ent
            if not lib_id:
                continue
            props = fp.properties or {}
            if isinstance(props, dict):
                value = _norm_value(props.get("Value", ""))
                ref = props.get("Reference", "") or ""
            else:
                # Older kiutils: list of Property objects
                value = ""; ref = ""
                for prop in props:
                    key = getattr(prop, "key", None) or getattr(prop, "name", None)
                    if key == "Value":
                        value = _norm_value(getattr(prop, "value", ""))
                    elif key == "Reference":
                        ref = getattr(prop, "value", "") or ""
            if not value:
                continue
            rec = idx[value][lib_id]
            rec["count"] += 1
            rec["projects"].add(project)
            if ref:
                rec["refs"].append(f"{project}:{ref}")
        ok += 1

    # Finalize
    out: dict[str, list[dict]] = {}
    for value, fps in idx.items():
        rows = []
        for lib_id, rec in fps.items():
            rows.append({
                "footprint": lib_id,
                "count": rec["count"],
                "projects": sorted(rec["projects"]),
                "refs": sorted(rec["refs"])[:20],
            })
        rows.sort(key=lambda r: -r["count"])
        out[value] = rows

    print(f"\n{ok} PCBs scanned, {fail} failed.")
    return out


def emit_markdown(idx: dict[str, list[dict]]) -> str:
    lines = ["# Footprint usage index", "",
             f"{len(idx)} distinct component values across scanned PCBs.",
             "",
             "For each value, footprints are listed in descending usage order.",
             "The **top row** is the historical default.",
             ""]
    # Sort by total uses descending
    totals = {v: sum(r["count"] for r in rows) for v, rows in idx.items()}
    for value in sorted(idx, key=lambda v: (-totals[v], v.lower())):
        rows = idx[value]
        lines.append(f"## {value}   _(used {totals[value]}×)_")
        lines.append("")
        lines.append("| Footprint | Count | Projects |")
        lines.append("|---|---|---|")
        for r in rows:
            projs = ", ".join(r["projects"][:5])
            if len(r["projects"]) > 5:
                projs += f" …(+{len(r['projects']) - 5})"
            lines.append(f"| `{r['footprint']}` | {r['count']} | {projs} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    pb = sub.add_parser("build", help="scan a tree and rebuild the index")
    pb.add_argument("path", type=Path)
    pb.add_argument("--out", type=Path, default=DEFAULT_OUT)

    pl = sub.add_parser("lookup", help="find best footprint for a value")
    pl.add_argument("value", help="component value, e.g. 'SN65HVD3082EDR'")
    pl.add_argument("--out", type=Path, default=DEFAULT_OUT)
    pl.add_argument("--all", action="store_true",
                    help="show all candidates, not just top")

    args, extras = p.parse_known_args()
    # Back-compat: if no subcommand but first arg is a path, behave as 'build'.
    if args.cmd is None:
        p2 = argparse.ArgumentParser()
        p2.add_argument("path", type=Path)
        p2.add_argument("--out", type=Path, default=DEFAULT_OUT)
        a = p2.parse_args(sys.argv[1:])
        args = argparse.Namespace(cmd="build", path=a.path, out=a.out)

    if args.cmd == "build":
        return _run_build(args.path, args.out)
    if args.cmd == "lookup":
        return _run_lookup(args.value, args.out, all_=args.all)
    p.print_help()
    return 1


def _run_build(path: Path, out: Path) -> int:
    root = path.expanduser().resolve()
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    idx = scan(root)
    (out / "index.yaml").write_text(
        yaml.safe_dump({"entries": idx}, sort_keys=False), encoding="utf-8")
    (out / "FOOTPRINTS.md").write_text(emit_markdown(idx), encoding="utf-8")

    totals = sorted(
        ((v, sum(r["count"] for r in rows), len(rows))
         for v, rows in idx.items()),
        key=lambda t: -t[1])
    print(f"\nTop 10 values by usage:")
    for v, n, k in totals[:10]:
        print(f"  {v:30}  {n:4}×   ({k} distinct footprints)")
    print(f"\nIndex: {out / 'index.yaml'}")
    print(f"Report: {out / 'FOOTPRINTS.md'}")
    return 0


def _run_lookup(value: str, out: Path, all_: bool = False) -> int:
    path = (out.expanduser().resolve()) / "index.yaml"
    if not path.exists():
        print(f"No index at {path}. Run `build` first.", file=sys.stderr)
        return 1
    idx = yaml.safe_load(path.read_text(encoding="utf-8"))["entries"]
    needle = value.strip()
    # exact → prefix → case-insensitive substring
    cand: list[tuple[str, list[dict]]] = []
    if needle in idx:
        cand = [(needle, idx[needle])]
    else:
        lower = needle.lower()
        for k, rows in idx.items():
            if k.lower() == lower:
                cand.append((k, rows))
        if not cand:
            for k, rows in idx.items():
                kl = k.lower()
                # Only accept substring matches where the shorter string
                # is at least 3 chars — avoids "L" matching everything.
                if (lower in kl and len(lower) >= 3) or \
                   (kl in lower and len(kl) >= 3):
                    cand.append((k, rows))
    if not cand:
        print(f"No footprint history for {value!r}.", file=sys.stderr)
        return 2
    # Merge candidates; sort all rows across matches
    rows_by_fp: dict[str, dict] = {}
    for _k, rows in cand:
        for r in rows:
            rec = rows_by_fp.setdefault(r["footprint"], {
                "footprint": r["footprint"], "count": 0,
                "projects": set(), "matched_values": set()})
            rec["count"] += r["count"]
            rec["projects"].update(r["projects"])
            rec["matched_values"].update([_k for _k, _ in cand if r in idx[_k]])
    merged = sorted(rows_by_fp.values(), key=lambda r: -r["count"])
    if not all_:
        merged = merged[:1]
    for r in merged:
        print(f"{r['count']:4}× {r['footprint']}")
        print(f"      matched: {', '.join(sorted(r['matched_values']))}")
        print(f"      projects: {', '.join(sorted(r['projects'])[:8])}"
              + (f" …(+{len(r['projects']) - 8})" if len(r['projects']) > 8 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
