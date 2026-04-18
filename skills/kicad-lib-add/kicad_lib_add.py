#!/usr/bin/env python3
"""
kicad_lib_add.py — register downloaded symbols/footprints with KiCad.

Scans a file or folder for:
    *.kicad_sym                 -> symbol library (name = file stem)
    *.pretty/                   -> footprint library (name = dir stem)
    dir/ containing *.kicad_mod -> footprint library (name = dir name)

and appends missing entries to KiCad's library tables. Idempotent —
libraries already registered (by name) are skipped. Writes a .bak copy
of each table before modifying it.

Usage:
    python3 kicad_lib_add.py <path> [options]

Options:
    --scope global        Write to ~/.config/kicad/<ver>/{sym,fp}-lib-table (default)
    --scope project PROJ  Write to PROJ/{sym,fp}-lib-table (PROJ = project dir)
    --kicad-version VER   Override auto-detection (e.g. 9.0, 10.0)
    --prefix STR          Prefix every new library name with STR_ (namespacing)
    --remove-prefix STR   Remove all entries whose name starts with STR_ then exit
    --force               Overwrite existing entry with same name
    --dry-run             Show what would change, don't write
    --list                Only list existing libraries, don't add anything

Examples:
    python3 kicad_lib_add.py /home/pc/Documents/footprints
    python3 kicad_lib_add.py ./libs --scope project .
    python3 kicad_lib_add.py --remove-prefix PRASAD --kicad-version 9.0
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class LibEntry:
    kind: str        # "sym" or "fp"
    name: str
    uri: str
    descr: str = ""

    def sexpr(self) -> str:
        return (f'  (lib (name "{self.name}")(type "KiCad")'
                f'(uri "{self.uri}")(options "")(descr "{self.descr}"))')


# ---------------------------------------------------------------------------
# Table I/O
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r'\(lib\s+\(name\s+"([^"]+)"')


def _read_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return set(_NAME_RE.findall(path.read_text(encoding="utf-8")))


def _remove_prefix_entries(path: Path, prefix: str, dry_run: bool = False) -> int:
    """Remove all lib entries whose name starts with `prefix_`. Returns count removed."""
    if not path.exists():
        return 0
    pfx = f"{prefix}_"
    content = path.read_text(encoding="utf-8")
    pattern = re.compile(r'\s*\(lib\s+\(name\s+"' + re.escape(pfx) + r'[^"]*"[^\n]*\)')
    matches = pattern.findall(content)
    count = len(matches)
    if count == 0:
        return 0
    if dry_run:
        for m in matches:
            name_m = _NAME_RE.search(m)
            print(f"  [WOULD REMOVE] {name_m.group(1) if name_m else m.strip()}")
        return count
    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    new_content = pattern.sub("", content)
    path.write_text(new_content, encoding="utf-8")
    return count


def _insert_entries(path: Path, entries: list[LibEntry], force: bool) -> tuple[int, int]:
    """Return (added, replaced)."""
    existing = _read_names(path)
    added = 0
    replaced = 0

    if not path.exists():
        header = "sym_lib_table" if path.name.startswith("sym") else "fp_lib_table"
        path.write_text(f"({header}\n  (version 7)\n)\n", encoding="utf-8")

    content = path.read_text(encoding="utf-8")
    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

    new_lines: list[str] = []
    for e in entries:
        if e.name in existing:
            if not force:
                continue
            # Remove old row
            content = re.sub(
                rf'\s*\(lib\s+\(name\s+"{re.escape(e.name)}"[^\n]*\)',
                "",
                content,
            )
            replaced += 1
        else:
            added += 1
        new_lines.append(e.sexpr())

    if not new_lines:
        return 0, 0

    # Insert before the final closing )
    close = content.rfind(")")
    if close < 0:
        raise RuntimeError(f"Malformed table: {path}")
    block = "\n".join(new_lines) + "\n"
    content = content[:close] + block + content[close:]
    path.write_text(content, encoding="utf-8")
    return added, replaced


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _iter_sym_files(root: Path) -> Iterable[Path]:
    if root.is_file() and root.suffix == ".kicad_sym":
        yield root
        return
    if root.is_dir():
        yield from sorted(root.rglob("*.kicad_sym"))


def _iter_pretty_dirs(root: Path) -> Iterable[Path]:
    if root.is_dir() and root.suffix == ".pretty":
        yield root
        return
    if root.is_dir():
        for p in sorted(root.rglob("*.pretty")):
            if p.is_dir():
                yield p


GENERIC_DIR_NAMES = {
    "kicad", "kicadv5", "kicadv6", "kicadv7", "kicadv8", "kicadv9",
    "footprint", "footprints", "fp", "symbol", "symbols",
    "library", "libraries", "lib", "libs",
}

# Filename stems that are clearly not library names (timestamps, "untitled")
_BAD_STEM_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[_-]\d{2}-\d{2}-\d{2}$"  # 2024-05-17_12-43-54
    r"|^untitled(\d+)?$"
    r"|^new[-_]?library$",
    re.IGNORECASE,
)


def _meaningful_name(d: Path, root: Path) -> tuple[Path, str]:
    """Pick the nearest non-generic ancestor name as the library label.
    Returns (dir_to_register, library_name).

    If `d` itself has a generic name, walk up toward `root` until a
    non-generic ancestor is found, and register *that* ancestor as the
    library (so e.g. `.../LIB_IEB0105S05/IEB0105S05/KiCad/` becomes
    a library named `IEB0105S05` rooted at `.../IEB0105S05/`).
    """
    cur = d
    while cur != root and cur.parent != cur:
        if cur.name.lower() not in GENERIC_DIR_NAMES and cur != root:
            return cur, cur.name
        cur = cur.parent
    # Fell through to root — use root name
    return root, root.name


def _iter_loose_mod_dirs(root: Path) -> Iterable[tuple[Path, str]]:
    """Yield (dir_to_register, library_name) for dirs containing *.kicad_mod.

    Skips `*.pretty` dirs (already handled) and collapses nested generic
    dirs (KiCad/, Footprint/) up to their nearest meaningful ancestor so
    we don't produce libraries literally named "KiCad".
    """
    if not root.is_dir():
        return
    seen: set[Path] = set()
    for mod in sorted(root.rglob("*.kicad_mod")):
        d = mod.parent
        if d.suffix == ".pretty":
            continue
        register_dir, name = _meaningful_name(d, root)
        if register_dir in seen:
            continue
        # Don't register the scan root itself — it's almost always a
        # collection folder whose name is meaningless (e.g. "Footprint").
        if register_dir == root:
            # Register the immediate parent of the mod file instead,
            # even if its name is generic — better than polluting the
            # table with a library literally named "Footprint".
            if d == root or d.name.lower() in GENERIC_DIR_NAMES:
                continue
            register_dir, name = d, d.name
        seen.add(register_dir)
        yield register_dir, name


def _iter_sym_file_with_name(root: Path) -> Iterable[tuple[Path, str]]:
    """Yield (sym_file, library_name) applying the same ancestor-collapse
    rule as footprints so a `.../<Part>/KiCad/<Part>.kicad_sym` layout
    becomes a library named `<Part>`, not shadowed by the generic parent
    dir. Also rewrites timestamp/untitled stems to their parent name."""
    for sym in _iter_sym_files(root):
        parent = sym.parent
        stem = sym.stem
        if _BAD_STEM_RE.match(stem) or parent.name.lower() in GENERIC_DIR_NAMES:
            if parent != root:
                _, name = _meaningful_name(parent, root)
            else:
                name = stem
        else:
            name = stem
        yield sym, name


def _iter_pretty_dirs_with_name(root: Path) -> Iterable[tuple[Path, str]]:
    """Same ancestor-collapse for *.pretty folders whose stem is generic
    (e.g. `.../foo/KiCADv6/footprints.pretty` -> library name `foo`)."""
    for p in _iter_pretty_dirs(root):
        stem = p.stem
        if stem.lower() in GENERIC_DIR_NAMES and p.parent != root:
            _, name = _meaningful_name(p.parent, root)
        else:
            name = stem
        yield p, name


def discover(root: Path, prefix: str) -> list[LibEntry]:
    entries: list[LibEntry] = []
    pfx = f"{prefix}_" if prefix else ""

    for sym, name in _iter_sym_file_with_name(root):
        entries.append(LibEntry("sym", pfx + name, str(sym.resolve())))

    for pretty, name in _iter_pretty_dirs_with_name(root):
        entries.append(LibEntry("fp", pfx + name, str(pretty.resolve())))

    for moddir, name in _iter_loose_mod_dirs(root):
        entries.append(LibEntry("fp", pfx + name, str(moddir.resolve())))

    # De-duplicate:
    #   1. Exact (kind, name, uri) duplicates are silently dropped.
    #   2. Same (kind, name) with DIFFERENT uris get "-2", "-3" suffixes
    #      to stay unique. Warn on stderr so the user can clean up.
    by_key: dict[tuple[str, str], list[LibEntry]] = {}
    for e in entries:
        by_key.setdefault((e.kind, e.name), []).append(e)
    final: list[LibEntry] = []
    for (kind, name), group in by_key.items():
        seen_uri: set[str] = set()
        unique = [e for e in group if not (e.uri in seen_uri or seen_uri.add(e.uri))]
        if len(unique) == 1:
            final.append(unique[0])
            continue
        print(f"WARN: {len(unique)} different paths want library name "
              f"'{name}' ({kind}); suffixing with -2, -3, ...",
              file=sys.stderr)
        for i, e in enumerate(unique):
            suffix = "" if i == 0 else f"-{i + 1}"
            final.append(LibEntry(e.kind, e.name + suffix, e.uri, e.descr))
    return final


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

def _detect_kicad_version() -> str:
    base = Path.home() / ".config" / "kicad"
    if not base.is_dir():
        raise RuntimeError(f"No KiCad config at {base}")
    versions = sorted(
        [p.name for p in base.iterdir() if p.is_dir() and re.match(r"\d+\.\d+", p.name)],
        key=lambda v: tuple(int(x) for x in v.split(".")),
    )
    if not versions:
        raise RuntimeError("No KiCad version dir found")
    return versions[-1]


def resolve_tables(scope: str, project: Path | None, version: str | None) -> tuple[Path, Path]:
    if scope == "project":
        if project is None:
            raise ValueError("--scope project requires a project path")
        return project / "sym-lib-table", project / "fp-lib-table"
    ver = version or _detect_kicad_version()
    d = Path.home() / ".config" / "kicad" / ver
    return d / "sym-lib-table", d / "fp-lib-table"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Register KiCad symbols & footprints in lib tables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("path", nargs="?", help="File or folder to scan.")
    p.add_argument("--scope", default="global", choices=("global", "project"))
    p.add_argument("--project", type=Path, default=None,
                   help="Project dir (required when --scope project).")
    p.add_argument("--kicad-version", dest="kver", default=None)
    p.add_argument("--prefix", default="")
    p.add_argument("--remove-prefix", dest="remove_prefix", default="",
                   metavar="STR",
                   help="Remove all entries whose name starts with STR_ from the tables.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()

    sym_tbl, fp_tbl = resolve_tables(args.scope, args.project, args.kver)

    if args.remove_prefix:
        pfx = args.remove_prefix
        for label, tbl in [("Symbols", sym_tbl), ("Footprints", fp_tbl)]:
            n = _remove_prefix_entries(tbl, pfx, dry_run=args.dry_run)
            verb = "Would remove" if args.dry_run else "Removed"
            print(f"{label}: {verb} {n} entr{'y' if n == 1 else 'ies'} with prefix '{pfx}_'  ({tbl})")
        if not args.dry_run:
            print("Tables backed up to *.bak before writing.")
        return 0

    if args.list:
        print(f"Symbol libs in {sym_tbl}:")
        for n in sorted(_read_names(sym_tbl)):
            print(f"  {n}")
        print(f"\nFootprint libs in {fp_tbl}:")
        for n in sorted(_read_names(fp_tbl)):
            print(f"  {n}")
        return 0

    if not args.path:
        p.error("path is required unless --list")

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return 1

    entries = discover(root, args.prefix)
    if not entries:
        print(f"No symbol or footprint libraries found under {root}")
        return 0

    sym_entries = [e for e in entries if e.kind == "sym"]
    fp_entries  = [e for e in entries if e.kind == "fp"]

    existing_sym = _read_names(sym_tbl)
    existing_fp  = _read_names(fp_tbl)

    def _report(label: str, es: list[LibEntry], existing: set[str]):
        print(f"\n{label}  ->  {label == 'Symbols' and sym_tbl or fp_tbl}")
        for e in es:
            state = "EXISTS" if e.name in existing and not args.force else (
                    "REPLACE" if e.name in existing else "NEW")
            print(f"  [{state:7}] {e.name:40}  {e.uri}")

    _report("Symbols",   sym_entries, existing_sym)
    _report("Footprints", fp_entries, existing_fp)

    if args.dry_run:
        print("\n(dry-run: no files written)")
        return 0

    s_add, s_rep = _insert_entries(sym_tbl, sym_entries, args.force) if sym_entries else (0, 0)
    f_add, f_rep = _insert_entries(fp_tbl,  fp_entries,  args.force) if fp_entries  else (0, 0)

    print(f"\nSymbols:    +{s_add} new, {s_rep} replaced  ({sym_tbl})")
    print(f"Footprints: +{f_add} new, {f_rep} replaced  ({fp_tbl})")
    print("Tables backed up to *.bak before writing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
