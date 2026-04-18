# kicad-edgecut

Extract board outlines (**Edge.Cuts** layer) from existing KiCad PCBs and
drop them into a new `.kicad_pcb` file. No manual redraw.

## Why

Every board you've already shipped is a worked example of a viable
mechanical envelope — hole spacing, connector reach, mounting tab sizes.
This skill turns that history into a reusable library.

## Fixed paths

| What | Path |
|---|---|
| Source scans | `/home/pc/Documents/kicad`, `/home/pc/Documents/PRASAD/PCB` |
| Library (default `--out`) | `~/kc/kicad-edgecuts/lib/` |

Each outline is stored as a normalized YAML (origin translated to `(0,0)`),
so placement origin is always a single `--at X,Y`.

## Usage

```bash
# 1. Build / refresh the library (scan both trees at once)
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py extract \
    /home/pc/Documents --out ~/kc/kicad-edgecuts/lib

# 2. Browse it
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py list
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py list --filter lora

# 3. Drop an outline into a new (or existing) PCB
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py place \
    --from LoRa_wroom \
    --to  /home/pc/Documents/kicad/my_new_board/my_new_board.kicad_pcb \
    --at  100,100 --clear
```

`--clear` removes existing items on the target layer first (so you can
re-seed without doubling-up). A `.bak` is written alongside the target
before modification unless `--no-backup` is passed.

## YAML schema

```yaml
name: Neo-2
project: Neo
source: /home/pc/Documents/PRASAD/PCB/Neo/Neo.kicad_pcb
layer: Edge.Cuts
bbox:
  orig_min: [X, Y]    # original coordinates (for reference)
  orig_max: [X, Y]
  width: mm
  height: mm
item_count: N
items:
  - {type: line,   start: [x,y], end: [x,y], width: 0.12}
  - {type: arc,    start: [x,y], mid: [x,y], end: [x,y], width: 0.12}
  - {type: circle, center: [x,y], end: [x,y], width: 0.12, fill: "no"}
  - {type: rect,   start: [x,y], end: [x,y], width: 0.12, fill: "no"}
  - {type: poly,   points: [[x,y], ...], width: 0.12, fill: "no"}
```

All coordinates are in **mm**, relative to the outline's lower-left corner.

## Name resolution for `--from`

1. Exact path to `*.yaml` — used as-is.
2. Name = YAML stem inside `--lib` (e.g. `Neo-2`).
3. Case-insensitive match.
4. Unique substring match.
5. Ambiguous / missing → error with candidate list.

## How `kicad-assemble` uses it

After generating a schematic + PCB stub, invoke `place` with the outline
the user picked (or inferred from the block library):

```bash
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py place \
    --from <outline_name> --to <new_project>.kicad_pcb --at 100,100 --clear
```

If the user says *"use the edgecut from LoRa_wroom"*, look it up by stem.
If the user specifies dimensions like *"100×70 mm"*, `list` the library
and match by `Size(mm)`.

## Limits

- Layer defaults to `Edge.Cuts`; pass `--layer User.Drawings` etc. to
  snapshot other layers (same schema).
- Kiutils occasionally fails on old/malformed PCBs (`list index out of
  range`) — those are reported as FAIL and skipped; re-save them in KiCad
  9.0 once to normalize.
- Holes/mounting drills live in footprints, not on Edge.Cuts. This skill
  intentionally only touches outline geometry.
