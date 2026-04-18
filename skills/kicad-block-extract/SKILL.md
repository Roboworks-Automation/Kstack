---
name: kicad-block-extract
description: |
  Build a functional peripheral knowledge graph from KiCad projects.
  Parses full net connectivity (hierarchical sheets included), classifies
  components by role (MCU, RS485, sensor, LDO, etc.), then aggregates
  across projects to find which peripheral blocks appear in ≥2 projects
  and which MCU pins they consume.

  Invoke for: "build a block library from my projects", "extract reusable
  blocks", "catalog my designs", "what peripherals do I reuse", "which
  pins does RS485 need on the ESP32".
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - Write
---

## Step 1 — Pick a scope

Ask the user whether to scan a single project or their whole KiCad tree.
Use `kstack_config` to discover the default projects directory:

```bash
python3 ~/.claude/skills/common/kstack_config.py path kicad_projects_dir
```

Typical choices:
- Single project:   `<kicad_projects_dir>/Neo`
- All projects:     `<kicad_projects_dir>`
- Any other parent directory containing multiple `.kicad_pro` files.

If `kstack_config show` reports missing keys, offer to run
`python3 ~/.claude/skills/common/kstack_config.py init` first.

## Step 2 — Run the extractor

```bash
OUT="$(python3 ~/.claude/skills/common/kstack_config.py path knowledge_dir)"
conda run -n kicad-agent python3 \
  ~/.claude/skills/kicad-block-extract/kicad_block_extract.py \
  <PATH> --out "$OUT"
```

If `kicad_parse.py` has been compiled to work without kiutils, plain `python3` also works.

The tool walks every `.kicad_pro`, parses hierarchical sheets via `kicad_parse.py`,
classifies components with `component_roles.yaml`, then writes:

    <out>/knowledge_graph.json    — machine-readable node/edge graph
    <out>/KNOWLEDGE_GRAPH.md      — human-readable graph with pin detail
    <out>/INDEX.md                — per-project component summary

## Step 3 — Read the outputs

### KNOWLEDGE_GRAPH.md contains three sections:

**MCU Nodes** — one row per distinct MCU part number found across all projects.

**Peripheral Nodes** — one row per functional peripheral type (role/family)
that appears in ≥2 distinct projects. Examples:
- `rs485/sn65hvd` — RS-485 transceivers (SN65HVD3082, MAX485, …)
- `sensor/hx711` — HX711 load-cell ADC
- `buck/lm2596` — LM2596 buck regulator
- `opto/tlp` — TLP-series optocouplers

**Edges** — each row is one MCU→peripheral pairing:
- How many projects use this pairing
- Typical pin count range
- The actual MCU pin names (IO17, RXD, GPIO5, …) used in each project

### knowledge_graph.json structure:

```json
{
  "nodes": [
    {
      "id": "ESP32-S3-WROOM-1", "type": "mcu",
      "family": "esp32-s3", "projects": ["Neo", "Neo-Eth"],
      "project_count": 2
    },
    {
      "id": "rs485/sn65hvd", "type": "peripheral",
      "role": "rs485", "family": "sn65hvd",
      "known_parts": ["SN65HVD3082", "MAX485"],
      "projects": ["Neo", "RS485WiFi", "Neo-Eth"],
      "project_count": 3
    }
  ],
  "edges": [
    {
      "mcu": "ESP32-S3-WROOM-1",
      "peripheral": "rs485/sn65hvd",
      "project_count": 2,
      "typical_pin_count": 3,
      "pin_range": "3–3",
      "common_mcu_pins": ["IO17", "IO18", "IO8"],
      "per_project": {
        "Neo":     ["IO17", "IO18", "IO8"],
        "Neo-Eth": ["U0TXD", "U0RXD", "IO5"]
      }
    }
  ]
}
```

## What the extractor captures

For each project:
- Uses `kicad_parse.py` for full net connectivity (handles hierarchical sheets)
- Classifies every component via `~/.claude/skills/kicad-knowledge/rules/component_roles.yaml`
- For each MCU pin: finds which classified peripherals share that net
- Records MCU pin names (not numbers) for human readability

Cross-project rules:
- Peripherals seen in only 1 project are **excluded** from the graph (not reusable)
- Use `--min-projects 1` to include all peripherals regardless

## Tuning classification

`component_roles.yaml` (in `~/.claude/skills/kicad-knowledge/rules/`) controls what
gets classified as rs485, sensor, ldo, etc. If a component is showing up as `unknown`,
add a rule. Pass a custom rules file with `--rules <path>`.

## Notes

- The extractor is **read-only** — it never modifies your projects.
- Hierarchical sheets are automatically resolved; sub-sheets contribute their
  components and nets to the parent project's connectivity graph.
- Power nets (GND, 3V3, 5V, 24V, VCC, …) are excluded from edges — only
  signal connections between MCU and peripheral are recorded.
- `connector` role is included in the graph; passive/crystal/protection/unknown
  are excluded from MCU edges (they clutter the graph without adding structure).
- Re-running overwrites all output files. If you curate the JSON, save a copy
  in a separate folder first.
