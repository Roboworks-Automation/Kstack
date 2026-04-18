---
name: kicad-assemble
description: |
  Create a KiCad schematic with components placed and nets labelled,
  starting from a high-level spec (MCU + peripherals list).

  Resolves symbols from the user's configured library paths
  (see `kstack_config`) — typically a personal symbol folder, KiCad 9
  stock libraries, or a downloaded-parts staging area. Consults a
  knowledge graph of past designs for pin conventions and footprint
  history. Generates a ready-to-open KiCad 9.0 project.

  Invoke for: "create a schematic for STM32 + RS485",
  "generate a KiCad project with ESP32 and optocoupler",
  "new board for this MCU and these parts",
  "assemble a schematic with these components".

allowed-tools:
  - Bash
  - Read
  - Write
  - Glob
  - Grep
  - AskUserQuestion
---

You are creating a KiCad schematic project. Follow these steps exactly.

## Step 0 — Confirm configuration and target directory

1. **Check Kstack config**. Run:
   ```bash
   python3 ~/.claude/skills/common/kstack_config.py show
   ```
   If any path marked `✗` is needed for this request (e.g. `prasad_dir` missing
   but user asked for a part that's only there), tell the user and offer:
   ```bash
   python3 ~/.claude/skills/common/kstack_config.py init
   ```
   If the user has never run init, **always offer it first**. Paths resolved
   there become defaults for every subsequent command.

2. **Ask where the project goes.** Unless the user already named a folder,
   ask: *"Is this a **new** project or should I extend an **existing** one?
   Which directory?"*
   - New default: `<kicad_projects_dir>/<project_name>/`
   - Existing: the user's path. If you detect a `.kicad_pro` already there
     and they didn't mean to overwrite, stop and confirm.

   Pass the answer via `--out <DIR>` and `--mode {new|existing|auto}`.
   `auto` (default) decides based on whether the target `.kicad_pro` exists
   and prints which mode it chose.

## Fixed paths (overridable — see kstack-config)

| Key | Default | Purpose |
|---|---|---|
| `kicad_projects_dir` | `~/Documents/kicad` | parent of each project folder |
| `prasad_dir` | `~/Documents/PRASAD/05326/Footprint` | personal sym/fp folder |
| `stock_symbols_dir` | `/usr/share/kicad/symbols` | KiCad 9 stock syms |
| `stock_footprints_dir` | `/usr/share/kicad/footprints` | KiCad 9 stock `.pretty` |
| `knowledge_dir` | `~/kc/kicad-knowledge` | mined knowledge graph |
| `edgecut_lib` | `~/kc/kicad-edgecuts/lib` | board-outline library |
| `fp_index_path` | `~/kc/kicad-footprints/index.yaml` | footprint history |
| `download_dir` | `~/Documents/footprints` | freshly downloaded parts |

If `knowledge_dir` is empty (no past projects mined), the generator still
works — it just can't suggest historical pin conventions; footprints
then resolve via (a) the symbol's `Footprint` property, (b) the stock `.pretty`
libs via `ki_fp_filters`.

---

## Step 1 — Understand the design

Parse the user's request into:
- **Project name** — snake_case, short
- **MCU** — exact part number (e.g. `STM32F103C8T6`, `ESP32-WROOM-32`)
- **Peripherals** — list of parts with their functional role

If the request is vague ("STM32 with RS485"), infer the most common variant
from the knowledge graph (`rs485/sn65hvd` → `SN65HVD3082EDR`).

---

## Step 2 — Consult the knowledge graph

For each peripheral, read its block YAML from
`<knowledge_dir>/blocks/<role>.yaml` (e.g. `rs485_sn65hvd.yaml`).
Resolve `<knowledge_dir>` with
`python3 ~/.claude/skills/common/kstack_config.py path knowledge_dir`.

Look at `mcu_connections[].per_project` to find which MCU pins were
historically used with this peripheral. Use the `per_project` entry that
matches the closest MCU or project. This tells you which MCU pins to wire.

**Also print the peripheral's pin names** to plan the connection mapping:

```bash
python3 ~/.claude/skills/kicad-assemble/kicad_sch_gen.py \
    --list-pins SN65HVD3082EDR
```

Match MCU pins to peripheral pins using UART/SPI conventions:
- RS-485: TX→D, RX→R, DE→DE, ~RE→~{RE}
- UART RTS/DE share the same MCU pin (e.g. PA9 = TX = DE driver enable)
- Opto inputs: MCU output pins → opto IN1/IN2...
- I2C: SCL→SCL, SDA→SDA

---

## Step 3 — Check which symbols exist

```bash
ls "$(python3 ~/.claude/skills/common/kstack_config.py path prasad_dir)/<PartName>/"
```

If the directory (or a `.kicad_sym` file) exists → symbol is available.
If not → mark it as **MISSING**, handle in Step 4.

---

## Step 4 — Download missing symbols (if any)

For any part not found in PRASAD, use the **browse skill** to find the
KiCad symbol file:

1. Search SnapEDA, Component Search Engine (componentsearchengine.com),
   Ultra Librarian, or the manufacturer's website.
2. Download the `.kicad_sym` (and `.kicad_mod` if footprint also needed).
3. Save to `<download_dir>/<PartName>/` (resolve with
   `kstack_config.py path download_dir`).
4. Register with kicad-lib-add:
   ```bash
   DL="$(python3 ~/.claude/skills/common/kstack_config.py path download_dir)"
   python3 ~/.claude/skills/kicad-lib-add/kicad_lib_add.py \
       "$DL/<PartName>" --kicad-version 9.0
   ```

---

## Step 5 — Build design.yaml

Write a `design.yaml` to `/tmp/<project_name>_design.yaml`:

```yaml
project: my_rs485_board

mcu:
  part: STM32F103C8T6   # exact part number as it appears in PRASAD library
  ref: U1

peripherals:
  - role: rs485/sn65hvd          # knowledge-graph role
    part: SN65HVD3082EDR         # exact part number in PRASAD
    ref: U2
    name: RS485_BUS              # label shown in schematic
    connections:                 # mcu_pin: peripheral_pin
      PA9:  D                    # USART1_TX  -> data input
      PA10: R                    # USART1_RX  -> receiver output
      PA12: DE                   # direction enable
      PA11: "~{RE}"              # receiver enable (active-low)

  - role: opto/tlp
    part: TLP281-4
    ref: U3
    name: OPTO_INPUTS
    connections:
      PB0: IN1
      PB1: IN2
      PB2: IN3
      PB3: IN4
```

**connections** keys are MCU pin names (PA9, PB0, IO17, etc.),
values are peripheral pin names exactly as shown in `--list-pins` output.

If you are unsure of pin names, run `--list-pins` first (Step 2).
Leave `connections` empty and add a comment if you cannot determine pins
— the script will still place symbols and show knowledge-graph suggestions.

---

## Step 6 — Generate the schematic

```bash
python3 ~/.claude/skills/kicad-assemble/kicad_sch_gen.py \
    /tmp/<project_name>_design.yaml \
    --kicad-version 9.0
```

This creates `<kicad_projects_dir>/<project_name>/`:
- `<project_name>.kicad_pro` — KiCad project file
- `<project_name>.kicad_sch` — schematic with placed symbols + net labels

The script exits 0 (all symbols found) or 2 (some symbols missing but
schematic still written with placeholder text).

---

## Step 7 — Report to user

Tell the user:
1. **Project path** — `~/Documents/kicad/<project_name>/`
2. **Connections applied** — list each MCU pin ↔ peripheral pin net label
3. **Knowledge-graph hints** — any suggested pins shown in the schematic
4. **Missing symbols** — what still needs to be downloaded
5. **Next steps** — open in KiCad 9.0, run ERC, add power flags, wire
   VCC/GND to each IC

---

## Step 8 — Footprint mapping from history

Before PCB layout, fill in each component's footprint property using the
historical usage index (what you've used before for this exact or similar
part):

```bash
# lookup best footprint for a value
python3 ~/.claude/skills/kicad-assemble/footprint_index.py lookup \
    SN65HVD3082EDR
# → 12× SN65HVD3082EDR:SOIC127P599X175-8N

python3 ~/.claude/skills/kicad-assemble/footprint_index.py lookup \
    AMS1117 --all
# → 40× Package_TO_SOT_SMD:SOT-223-3_TabPin2   (AMS1117-3.3)
#      1× AMS1117:SOT229P700X180-4N            (AMS1117-5.0)
```

The index is built once (and whenever new projects have been laid out)
with:
```bash
python3 ~/.claude/skills/kicad-assemble/footprint_index.py build \
    "$(python3 ~/.claude/skills/common/kstack_config.py path kicad_projects_dir)" \
    --out "$(dirname "$(python3 ~/.claude/skills/common/kstack_config.py path fp_index_path)")"
```

When generating the schematic, for any component whose `footprint` is
empty, query the index and use the **top** (most-used) result. If the
lookup finds no match, leave footprint empty and flag it for the user.

---

## Step 9 — Board outline (Edge.Cuts)

If the user picked an outline from a past project (*"use the edgecut from
LoRa_wroom"*), drop it onto the new `.kicad_pcb` after it is generated:

```bash
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py list --filter lora
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py place \
    --from LoRa_wroom \
    --to   "<kicad_projects_dir>/<project_name>/<project_name>.kicad_pcb" \
    --at   100,100 --clear
```

If no historical outline fits, generate one instead (see kicad-edgecut
SKILL.md — ask the user for shape, width×height, corner radius, and
mounting-hole count).

The library is built once via:
```bash
python3 ~/.claude/skills/kicad-edgecut/kicad_edgecut.py extract \
    "$(python3 ~/.claude/skills/common/kstack_config.py path kicad_projects_dir)"
```

If the user specifies dimensions instead of a name (*"about 100×70"*),
run `list` and pick the closest-matching outline.

---

## Step 10 — Summarize to user

Tell the user:
1. **Project path** — `~/Documents/kicad/<project_name>/`
2. **Connections applied** — list each MCU pin ↔ peripheral pin net label
3. **Knowledge-graph hints** — any suggested pins shown in the schematic
4. **Footprint mapping** — how many components got historical footprints
   assigned, which ones need manual selection
5. **Edge.Cuts** — which outline was placed (if any)
6. **Missing symbols** — what still needs to be downloaded
7. **Next steps** — open in KiCad 9.0, run ERC, add power flags, wire
   VCC/GND to each IC

---

## Quick reference

### List pins for a part
```bash
python3 ~/.claude/skills/kicad-assemble/kicad_sch_gen.py \
    --list-pins <PART_NUMBER>
```

### Re-generate after adding missing symbols
```bash
python3 ~/.claude/skills/kicad-assemble/kicad_sch_gen.py \
    /tmp/<project>_design.yaml
```

### Generate firmware scaffold (pinmap + PlatformIO)
```bash
python3 ~/.claude/skills/kicad-assemble/kicad_assemble.py \
    /tmp/<project>_design.yaml \
    --out "<kicad_projects_dir>/<project>/firmware" \
    --blocks-dir "$(python3 ~/.claude/skills/common/kstack_config.py path knowledge_dir)/blocks"
```

---

## Layout produced

```
A3 page (420×297 mm)
┌────────────────────────────────────────────────────┐
│  MCU: STM32F103C8T6                                │
│  ┌──────────────┐      RS485_BUS      OPTO_INPUTS  │
│  │  U1          │      ┌──────┐       ┌──────┐    │
│  │  STM32...    │      │  U2  │       │  U3  │    │
│  │  PA9 ──────────── D │      │       │      │    │
│  │  PA10 ─────────── R │      │       │      │    │
│  │  PA12 ─────────── DE│      │       │      │    │
│  │  PA11 ──────────~RE │      │       │      │    │
│  └──────────────┘      └──────┘       └──────┘    │
└────────────────────────────────────────────────────┘
```

Net labels with the MCU pin name appear at both the MCU pin endpoint and
the peripheral pin endpoint — KiCad connects them automatically by name.

---

## Knowledge graph roles available

Run to see all extracted blocks:
```bash
ls "$(python3 ~/.claude/skills/common/kstack_config.py path knowledge_dir)/blocks/"
```

Common roles:
- `rs485/sn65hvd` — RS-485 transceiver (SN65HVD, MAX485)
- `opto/tlp`      — Optocoupler (TLP281-4, TLP291-4)
- `opto/pc817`    — PC817 optocoupler
- `driver/uln28xx` — ULN2803 relay/solenoid driver
- `adc/hx711`     — HX711 load cell ADC
- `ethernet_mac/w5x00` — W5500 Ethernet
- `display_drv/tm1637` — TM1637 7-segment display
- `level_shifter/bss138` — BSS138 logic-level shifter
- `connector/conn` — Connector/terminal block

---

## Notes

- **Schematic accuracy**: symbols are placed with net labels at pin endpoints
  (calculated from the symbol's local pin coordinates). Open in KiCad and
  run ERC to catch any label/pin mismatches.
- **Multi-unit symbols** (e.g. TLP281-4 quad opto): the script places unit 1
  only. Manually place additional units in KiCad for IC2, IC3, IC4 of the same ref.
- **Power pins**: VCC and GND pins of each IC are not auto-wired. Add power
  symbols manually in KiCad after opening.
- **Footprints**: The `Footprint` property is carried from the original PRASAD
  symbol. Run PCB > Update PCB from Schematic after layout work.
