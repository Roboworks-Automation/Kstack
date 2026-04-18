# Kstack — KiCad Automation Skills for Claude Code

A collection of [Claude Code](https://docs.claude.com/en/docs/claude-code) skills
that turn an existing library of KiCad projects into reusable automation:
parse schematics, mine a knowledge graph of components/pins/nets, generate new
schematics from high-level specs, reuse board outlines, and register third-party
libraries — all from natural-language prompts.

Built and tested against **KiCad 9.0** on Linux.

---

## What's in the box

### KiCad skills

| Skill | What it does |
|---|---|
| [`kicad`](skills/kicad/) | Parse any `*.kicad_sch`, resolve hierarchical net connectivity, answer pin/net/signal-path questions, and apply small edits (add labels, add wires). |
| [`kicad-block-extract`](skills/kicad-block-extract/) | Scan a tree of KiCad projects, classify components by role (MCU, RS485 transceiver, LDO, optocoupler, sensor, connector …), and aggregate MCU↔peripheral connectivity across projects into a knowledge graph (JSON + GraphML + Markdown reports). |
| [`kicad-edgecut`](skills/kicad-edgecut/) | Extract board outlines from the `Edge.Cuts` layer of every PCB in your history, normalize to `(0,0)` top-left, and drop any outline into a new `.kicad_pcb` with one command. |
| [`kicad-assemble`](skills/kicad-assemble/) | Generate a ready-to-open KiCad 9.0 project from a YAML spec (MCU + peripherals + connections). Resolves symbols from PRASAD libs + KiCad 9 stock libs, maps footprints from history + `ki_fp_filters` globs, and emits wire stubs with net labels placed away from the symbol body for readability. |
| [`kicad-lib-add`](skills/kicad-lib-add/) | Register downloaded symbol/footprint libraries into KiCad's `sym-lib-table` / `fp-lib-table` (global or per-project). |

### Browser helpers (optional, provided by [GStack](https://github.com/graphstack/gstack))

Kstack skills can call into these browser skills when they need to fetch a
datasheet or a vendor symbol/footprint that isn't already local:

| Skill | What it does |
|---|---|
| `browse` | Fast headless browser for QA / fetching datasheets. |
| `open-gstack-browser` (a.k.a. `connect-chrome`) | Launch a visible AI-controlled Chromium window. |
| `setup-browser-cookies` | Import cookies from your real browser (for paywalled datasheets / authenticated vendor portals). |

These ship as part of the separate **GStack** runtime and are **not vendored
here** — they live in `~/.claude/skills/gstack/{browse,connect-chrome,setup-browser-cookies}/`
after installing GStack. Kstack works without them; they just make the
"fetch a missing symbol" loop automatic.

---

## Repository layout

```
Kstack/
├── skills/
│   ├── kicad/                   # Parse + Q&A + small edits
│   ├── kicad-block-extract/     # Knowledge graph builder
│   ├── kicad-edgecut/           # Board-outline library
│   ├── kicad-assemble/          # Schematic generator
│   └── kicad-lib-add/           # Register libs into KiCad
├── install.sh                   # Symlink skills into ~/.claude/skills/
├── requirements.txt             # kiutils, PyYAML
├── LICENSE
└── README.md
```

Each skill folder contains a `SKILL.md` (the prompt Claude reads) and one or
more Python modules. Skills that need a runtime share a single conda env
called `kicad-agent` (see **Install**).

---

## Install

### Prerequisites

- **KiCad 9.0** with `kicad-cli` on `$PATH`
- **Python ≥ 3.10** (via conda/mamba recommended)
- **Claude Code** CLI installed and working (`claude --version`)

### 1 — Clone

```bash
git clone https://github.com/Roboworks-Automation/Kstack.git ~/kc/Kstack
cd ~/kc/Kstack
```

### 2 — Python environment

```bash
conda create -n kicad-agent python=3.11 -y
conda activate kicad-agent
pip install kiutils pyyaml
```

### 3 — Register skills with Claude Code

```bash
bash install.sh
```

This symlinks every folder in `skills/` into `~/.claude/skills/`.
Use `bash install.sh --copy` if you prefer copies over symlinks.

### 4 — Tell Kstack where your KiCad files live

Run the one-time interactive config wizard:

```bash
python3 skills/common/kstack_config.py init
```

You'll be asked for:
- `kicad_projects_dir` — root of your KiCad projects (default `~/Documents/kicad`)
- `prasad_dir` — optional vendor symbol/footprint libs
- `stock_symbols_dir` / `stock_footprints_dir` — KiCad 9 stock libs
  (default `/usr/share/kicad/{symbols,footprints}`)
- `knowledge_dir` — where to write the block-extract knowledge graph
- `edgecut_lib` — where to store extracted board outlines
- `fp_index_path` — footprint-usage history file
- `download_dir` — cache for fetched datasheets / libraries

Settings are saved to `~/.config/kstack/config.yaml`. Override any single
value on a run with a CLI flag, an env var (`KSTACK_KICAD_PROJECTS_DIR=...`),
or by editing the YAML directly. See [`skills/common/README.md`](skills/common/README.md)
for the full key reference and resolution order.

```bash
# Inspect resolved paths at any time:
python3 skills/common/kstack_config.py show
```

---

## Use cases

### 1. Ask questions about an existing schematic

In a KiCad project folder:

> **"Which pins of the ESP32 are connected to the RS485 transceiver?"**
>
> **"Trace the signal from J1 pin 2 to the MCU."**
>
> **"What components are on the 3V3 net?"**

→ Runs the [`kicad`](skills/kicad/) skill.

### 2. Build a design knowledge graph from every project you've ever made

> **"Catalog all my KiCad designs and tell me which peripherals I reuse."**

```bash
conda run -n kicad-agent python3 \
    skills/kicad-block-extract/kicad_block_extract.py \
    --root "$(python3 skills/common/kstack_config.py path kicad_projects_dir)" \
    --out  "$(python3 skills/common/kstack_config.py path knowledge_dir)"
```

Outputs:
- `knowledge_graph.json` — full graph
- `graph.graphml` — import into Gephi / Cytoscape
- `mcu-peripheral-matrix.md` — which MCUs appear with which peripherals
- `pin-conventions.md` — your personal pin-usage conventions (e.g.
  *"ESP32 IO21/22 → I²C 14× / RS485 6×"*)
- `blocks/` — YAML per functional block (rs485, power, sim7600, …)

### 3. Reuse a past board outline in a new project

```bash
# Build the outline library once (75 outlines from ~90 projects typical)
python3 skills/kicad-edgecut/kicad_edgecut.py extract ~/Documents/kicad

# List available outlines
python3 skills/kicad-edgecut/kicad_edgecut.py list

# Drop an outline into a new empty .kicad_pcb
python3 skills/kicad-edgecut/kicad_edgecut.py place \
    --from Andon_rs485 \
    --to ~/Documents/kicad/NewBoard/NewBoard.kicad_pcb
```

KiCad's PCB origin is top-left (Y grows downward); `--at X,Y` is the top-left
corner of the bbox and defaults to `(30, 30)` mm.

### 4. Generate a complete schematic from a spec

`design.yaml`:
```yaml
project: Lora_10output
mcu:
  part: ESP32-WROOM-32
  ref: U1
peripherals:
  - part: ULN2803A
    ref:  U2
    name: OUT1_5
    connections:
      IO21: I1
      IO22: I2
      IO23: I3
      IO25: I4
      IO26: I5
  - part: ULN2803A
    ref:  U3
    name: OUT6_10
    connections:
      IO27: I1
      IO32: I2
      IO33: I3
      IO18: I4
      IO19: I5
```

```bash
python3 skills/kicad-assemble/kicad_sch_gen.py design.yaml \
    --out ~/Documents/kicad/Lora_10output
```

→ Emits `Lora_10output.kicad_pro` + `.kicad_sch` with:
- Symbols resolved from KiCad 9 stock libs or PRASAD
- Footprints mapped via (a) symbol property, (b) past-project history, (c)
  `ki_fp_filters` glob against stock `.pretty` libs
- Short wire stubs with net labels placed at the stub end so the schematic
  reads cleanly
- Ready to open with `kicad-cli sch erc`

### 5. Register a third-party symbol/footprint download

After grabbing a library from SnapEDA / Ultra Librarian:

> **"Add this BSS138 library to KiCad."**

→ Runs [`kicad-lib-add`](skills/kicad-lib-add/), which writes entries into
`sym-lib-table` and `fp-lib-table` (global or project-scoped).

---

## How skills compose

```
┌──────────────────────┐  scans    ┌──────────────────────────┐
│  Your KiCad projects │──────────▶│  kicad-block-extract     │
└──────────────────────┘           │  (knowledge graph)       │
         │                         └──────────┬───────────────┘
         │ outlines                           │ pin conventions,
         ▼                                    │ MCU↔peripheral pairs
┌──────────────────────┐                      ▼
│  kicad-edgecut       │           ┌──────────────────────────┐
│  (outline library)   │──────────▶│  kicad-assemble          │
└──────────────────────┘  places   │  (generator)             │
                                   └──────────┬───────────────┘
                                              │ missing parts?
                                              ▼
                                   ┌──────────────────────────┐
                                   │  browse / setup-cookies  │
                                   │  (fetch symbols)         │
                                   └──────────┬───────────────┘
                                              ▼
                                   ┌──────────────────────────┐
                                   │  kicad-lib-add           │
                                   │  (register in KiCad)     │
                                   └──────────────────────────┘
```

---

## Verifying an install

```bash
# 1. Generator + stock-lib resolution works
python3 skills/kicad-assemble/kicad_sch_gen.py --help

# 2. Edge-cut extractor works
python3 skills/kicad-edgecut/kicad_edgecut.py list

# 3. Parser works on an existing schematic
python3 skills/kicad/kicad_parse.py path/to/project.kicad_sch
```

If Claude Code picks up the skills, a prompt like *"what is on pin 12 of U1"*
inside a KiCad project folder should trigger the `kicad` skill automatically.

---

## License

MIT — see [LICENSE](LICENSE).
