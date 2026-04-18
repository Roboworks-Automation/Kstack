# kicad-edgecut

Two sources of board outlines, one placement tool:

1. **Extract** outlines (Edge.Cuts layer) from every PCB you've ever shipped
   and drop any of them into a new `.kicad_pcb`.
2. **Generate** fresh rectangular / rounded-rect / circular outlines with
   mounting holes at user-supplied dimensions.

Both write the same normalized YAML schema, so `place` works either way.

## Why

Every board you've already shipped is a worked example of a viable
mechanical envelope. When history doesn't have the right shape, the
generator makes a clean parametric outline instead of redrawing by hand.

## Configured paths

The library lives at the path resolved by `kstack_config`
(key `edgecut_lib`, default `~/kc/kicad-edgecuts/lib`). Override with
`--lib DIR` or by editing `~/.config/kstack/config.yaml`.

Each outline is stored as normalized YAML (origin at `(0, 0)` top-left;
KiCad PCB origin is top-left, Y grows downward), so placement origin is
always a single `--at X,Y`.

## Usage

```bash
# 1a. Build the library from past projects (one-time)
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py extract \
    "$(python3 ~/.claude/skills/common/kstack_config.py path kicad_projects_dir)"

# 1b. Or generate a fresh outline — ALWAYS ask the user for dimensions first
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py generate \
    --name MyBoard --shape rect --width 100 --height 70 \
    --corner-radius 3 --holes 4 --hole-diameter 3.2

# 2. Browse the library
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py list
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py list --filter lora

# 3. Drop an outline into a new (or existing) PCB
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py place \
    --from MyBoard \
    --to  ~/Documents/kicad/my_new_board/my_new_board.kicad_pcb \
    --at  30,30 --clear

# One-shot: generate and place in a single command
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py generate \
    --name MyBoard --shape rect --width 100 --height 70 --holes 4 \
    --to ~/Documents/kicad/my_new_board/my_new_board.kicad_pcb --clear
```

`--clear` removes existing items on the target layer first. A `.bak` is
written alongside the target before modification unless `--no-backup`.

## When to use generate vs extract

| Situation | Command |
|---|---|
| User says "same shape as X" / "reuse the LoRa board" | `place --from <X>` |
| User gives explicit dimensions (W×H, or "round, ø60") | `generate` |
| User gives dimensions **plus** mounting-hole count | `generate --holes N` |
| User has CAD drawing with exact hole coords | `generate --holes "x1,y1;x2,y2;..."` |

## Asking the user (generate workflow)

Before running `generate`, always confirm:

1. **Shape** — `rect` (with optional corner radius) or `circle`.
2. **Dimensions** — width × height (rect) or diameter (circle), in mm.
3. **Mounting holes** — count (0, 2, 4, or 6 preset), or explicit X,Y list
   from the top-left corner.
4. **Hole diameter** — default 3.2 mm (M3 clearance). Ask if unsure.
5. **Corner radius** — default 0. Suggest 2–5 mm for rounded corners.

`--holes` presets:
- `0` — no holes
- `2` — centreline, short edges
- `4` — one per corner (at `--hole-margin` from each edge, default 3.5 mm)
- `6` — 4 corners + 2 mid-edge

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
If the user specifies dimensions like *"100×70 mm"*, first `list` the
library and match by `Size(mm)`; if nothing fits, fall back to
`generate --shape rect --width 100 --height 70`.

## Limits

- Layer defaults to `Edge.Cuts`; pass `--layer User.Drawings` etc. to
  snapshot other layers (same schema).
- Kiutils occasionally fails on old/malformed PCBs (`list index out of
  range`) — those are reported as FAIL and skipped; re-save them in KiCad
  9.0 once to normalize.
- `generate` emits mounting holes as circles on `Edge.Cuts` (so KiCad
  cuts them during fabrication). If you need plated pads around the
  holes, add mounting-hole footprints separately.
