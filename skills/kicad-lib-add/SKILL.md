---
name: kicad-lib-add
description: |
  Register downloaded KiCad symbols and footprints into KiCad's library
  tables (sym-lib-table, fp-lib-table) so they become available in
  eeschema / pcbnew without manually using Preferences > Manage Libraries.

  Handles vendor-style folders such as:
      ~/Documents/footprints/
          BSS138/        (*.kicad_sym, *.kicad_mod)
          ULN2803AFW/    (*.kicad_sym, *.kicad_mod)
          MyLib.pretty/  (footprint library folder)

  Invoke for: "add this symbol to KiCad", "register these footprints",
  "import downloaded library", "make this library available", "set up libraries
  for this project".
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
---

You are registering a KiCad library. Follow these steps exactly.

## Step 1 — Confirm scope

Ask the user (or infer from context) whether the library should be registered:

- **global** — available in every project. Writes to
  `~/.config/kicad/<version>/{sym,fp}-lib-table`. This is the default.
- **project** — available only inside one project. Writes to
  `<project>/{sym,fp}-lib-table`, which goes into version control with the
  project. Preferred when the lib is project-specific or you want the team
  to get it automatically.

## Step 2 — Dry-run first

Always start with `--dry-run` so the user can see what will be registered
before any file is touched:

```bash
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py <PATH> --dry-run
```

Review the `[NEW]` / `[EXISTS]` / `[REPLACE]` report. `[EXISTS]` entries are
already registered and will be skipped — this is correct, no action needed.
Do NOT recommend `--prefix` to resolve collisions; it pollutes library names
with permanent prefixes that are hard to undo.

## Step 3 — Apply

```bash
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py <PATH> [--scope project --project <DIR>]
```

The script:
- Auto-detects the latest installed KiCad version under `~/.config/kicad/`
- Backs up each table to `*.bak` before writing
- Is idempotent: re-running is safe, already-registered names are skipped
- Accepts a single `.kicad_sym` file, a single `.pretty/` folder, or a
  directory tree containing many of either (plus loose `.kicad_mod` folders)

## Step 4 — Tell the user

- KiCad must be restarted (or at least the schematic/PCB editor reopened)
  to pick up new library-table rows.
- If they used `--scope global`, suggest committing the vendor folder
  itself to a stable location (not `~/Downloads`). `~/Documents/kicad-libs/`
  is a reasonable convention.

## Quick reference

List what's already registered:

```bash
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py --list
```

Override KiCad version:

```bash
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py <PATH> --kicad-version 9.0
```

Remove entries that were accidentally added with a prefix:

```bash
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py --remove-prefix PRASAD --kicad-version 9.0
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py --remove-prefix PRASAD --kicad-version 10.0
# Then re-add without prefix:
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py <PATH> --kicad-version 9.0
python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py <PATH> --kicad-version 10.0
```

## Notes

- Library NAME is derived from the filename stem (`foo.kicad_sym` → `foo`)
  or the folder name (`foo.pretty` → `foo`, `foo/` with loose .kicad_mod → `foo`).
- Footprint folders not named `*.pretty` are accepted because KiCad's
  `KiCad` plugin type treats any directory of `.kicad_mod` files as a library.
- The tool never moves or copies library files — it only registers URIs.
  Point at a permanent path, not `~/Downloads`.
