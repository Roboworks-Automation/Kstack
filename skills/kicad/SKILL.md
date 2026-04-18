---
name: kicad
description: |
  KiCad schematic analysis and connectivity assistant. Parses .kicad_sch files,
  resolves full net connectivity, and answers questions about components, pins,
  signal routing, and power rails. Can also apply simple schematic changes
  (add/remove net labels, add wires).
  
  Invoke for: "what connects to X pin Y", "which net is R1 pin 1 on",
  "trace signal from IN1 to ESP32", "show GND connections", "connect X to Y",
  "what components are on the 3V3 net", schematic design review questions.
  
  Proactively invoke when the user is in a KiCad project directory (contains
  .kicad_sch) and asks about component connections, nets, or signal paths.
allowed-tools:
  - Bash
  - Read
  - Glob
  - Grep
  - Edit
  - Write
  - AskUserQuestion
---

You are analyzing a KiCad schematic. Follow these steps exactly.

## Step 1 — Find the schematic

Use Glob to find `*.kicad_sch` in the current directory.
- If none found: report "No .kicad_sch found in current directory" and stop.
- If multiple found: pick the one whose stem matches the directory name, or ask the user.

## Step 2 — Parse the schematic

Run the parser (uses the `kicad-agent` conda environment):

```bash
conda run -n kicad-agent python ~/.claude/skills/kicad/kicad_parse.py "<SCHEMATIC_PATH>" summary
```

This produces a structured text report with:
- **Sheets**: list of all `.kicad_sch` files that were merged (hierarchical projects)
- **Stats**: component count, wire count, label count (aggregated across all sheets)
- **Named nets**: nets with explicit labels. Global nets (from `global_label` or power symbols) are merged across sheets with their plain name (e.g. `/GND`, `/3V3`). Sheet-local nets are prefixed `SheetName::net` (e.g. `DIO::GND1`)
- **Unlabeled internal nets**: real electrical nodes without labels, in `[Sheet::]net_X_Y` format
- **Component table**: every component's ref, value, footprint, sheet, and full pin→net mapping

If the conda env is not available, try:
```bash
python3 ~/.claude/skills/kicad/kicad_parse.py "<SCHEMATIC_PATH>" summary
```

## Step 3 — Respond to the request

**The user's request is:** $ARGUMENTS

---

### If request is empty or "help" or "summary":
Print a compact overview:
- Schematic stats (components, nets, labels)
- Power rails and what connects to each
- Key signal nets grouped by function
- A brief "ask me about any component or net" invitation

---

### If request is a connectivity question ("what connects to X", "which net is Y on", "pins of Z"):
1. Find the component or net in the parsed data
2. Report the net name for each pin, and ALL other components on that net
3. For unlabeled nets (`net_X_Y` format): describe them as "internal node — no net label, connects [list the pins]"
4. Be specific: include pin numbers, pin names, and the full component ref+value

**Example answer format:**
```
R2 (Resistor, Device:R):
  Pin 1 → net_180.34_132.08  [internal node, shared with: Q3.2:B (base of Q3)]
  Pin 2 → D18                [also on: U1.30:IO18 (ESP32 GPIO18)]
```

---

### If request is a signal trace ("trace signal from X to Y"):
1. Find the source net (X)
2. Find all components on that net
3. For each component, find which other nets its pins connect to
4. Trace the path to the destination (Y), showing each hop
5. Describe the signal chain clearly (e.g., "IN1 → R11 → U5 LED anode → optocoupler → Rec1 → J7")

---

### If request is a change ("connect X to Y", "add label", "rename net", "add component"):

**Before applying any change:**
1. Parse the schematic to find the target pin's current net and world position
2. Explain what change is needed in plain terms
3. Show the exact command you'll run
4. Confirm with the user before executing (use AskUserQuestion if uncertain)

**To connect a floating pin to a named net** (e.g., connect R1 pin 1 at (214.63, 102.87) to GND):
```bash
python ~/.claude/skills/kicad/kicad_apply.py "<SCHEMATIC_PATH>" add-label --text GND --x 214.63 --y 102.87
```

**To add a wire between two points**:
```bash
python ~/.claude/skills/kicad/kicad_apply.py "<SCHEMATIC_PATH>" add-wire --x1 X1 --y1 Y1 --x2 X2 --y2 Y2
```

> `kicad_apply.py` uses direct raw-text editing — it does NOT round-trip through kiutils. Run it with the system `python3` (not `conda run`) since it no longer requires kiutils.

**After applying**: re-run the parser to confirm the pin now shows the expected net name.

**Safety rules for changes:**
- Never remove an existing label unless explicitly asked
- Always verify the pin world-position from the parsed data before passing to kicad_apply
- The apply script auto-backs up to `.kicad_sch.bak` before writing
- KiCad grid is 1.27 mm or 2.54 mm — coordinates must land on grid, or KiCad will show DRC errors
- After changes, tell the user to open in KiCad and run ERC (Electrical Rules Check) to validate

---

### If request is to add a new component (resistor, LED, capacitor, etc.)

Adding symbols requires a Python script — `kicad_apply.py` only handles labels and wires.
Write a dedicated script and apply all changes in **one raw-text pass** (no kiutils round-trip).

**Step-by-step workflow:**

**1. Look up the actual pin offsets** — do NOT assume them. Read the `lib_symbols` section of the schematic (or `/usr/share/kicad/symbols/Device.kicad_sym`) to find the exact `(pin ... (at X Y angle))` coordinates for each pin. Common ones (confirmed in this project):
- `Device:R` — pins at local **(0, +3.81)** and **(0, −3.81)**. Placed at angle=90 (horizontal): pin1=(cx−3.81, cy), pin2=(cx+3.81, cy)
- `Device:LED` — pin K at local **(−3.81, 0)**, pin A at **(+3.81, 0)**. Placed at angle=180 (reversed): anode=(cx−3.81, cy), cathode=(cx+3.81, cy)
- Pin world position formula: `world = (cx + px·cosA − py·sinA,  cy − (px·sinA + py·cosA))`

**2. Choose placement positions** on the 2.54 mm grid (100 mil):
- `cx / 2.54` and `cy / 2.54` must be integers
- Place new components in visually empty space — check existing component positions from the parser summary

**3. Always use a direct wire** from the source pin to the new component's pin.
Never rely on same-name labels across a gap for connections in the same area — it is confusing for humans reading the schematic.

**4. Required fields in every symbol S-expression:**
```
(symbol
    (lib_id "Device:R")
    (at CX CY ANGLE)
    (unit 1)
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (fields_autoplaced yes)
    (uuid "...fresh UUID...")
    (property "Reference" "R35" ...)   ← VISIBLE, no hide
    (property "Value"     "330R" ...)   ← VISIBLE, no hide
    (property "Footprint" "..." ...)    ← MUST have (hide yes) in effects
    (property "Datasheet" "~"  ...)    ← MUST have (hide yes) in effects
    (property "Description" "..." ...) ← MUST have (hide yes) in effects
    (pin "1" (uuid "..."))
    (pin "2" (uuid "..."))
    (instances                          ← REQUIRED — without this KiCad resets ref to "R?"
        (project "Neo"
            (path "/PROJECT-UUID"
                (reference "R35")
                (unit 1)
            )
        )
    )
)
```

**Critical: NEVER use kiutils `to_file()` after adding symbols.**  
kiutils round-trips corrupt manually inserted symbols:
- Reference designator is reset to `R?` (missing `instances` block or kiutils bug)
- `(hide yes)` flags are dropped from Footprint/Datasheet properties → clutters schematic
- Property `(at ...)` positions are recalculated and jumbled

All edits (symbols, wires, labels) must be done as **raw S-expression text insertions** before the final `\n)` of the file. The `kicad_apply.py` script already does this safely for labels and wires.

**5. Finding the project UUID** (needed for the `instances` block):
```python
import re
m = re.search(r'\(path "(/[0-9a-f-]+)"', content)
project_path = m.group(1)   # e.g. "/14ec2205-6f58-47dd-8ab3-c7dcafcac86a"
```

**6. Adding a lib symbol** (e.g. `Device:LED` not yet in the schematic):
Extract it from `/usr/share/kicad/symbols/Device.kicad_sym`, rename it `"Device:LED"`, and insert it inside the `(lib_symbols ...)` block before its closing `)`.

**7. Verify after every addition** by re-running the parser:
- The new ref (R35, D6, etc.) should appear in the component list
- Every pin should show the expected net name — not `?` or an unexpected unlabeled net
- No unlabeled floating wire ends (would show as a single-pin `net_X_Y` in the parser)

---

## Key facts about this schematic format (KiCad 6+ S-expression)

**Edge cases the parser handles automatically:**
1. **Hierarchical sheets** — the parser auto-detects sub-sheet references in the root `.kicad_sch`, loads every sub-sheet recursively, and merges the results into one flat connectivity model
2. **Global vs local label scoping** — `global_label` nets (prefix `/`) and power-symbol nets merge across sheets; local labels and unlabeled nodes are prefixed `SheetName::` to avoid false merges
3. **Mid-wire labels** — labels can be anywhere along a wire, not just endpoints
4. **Multi-unit components** (e.g., TLP291-4 quad optocoupler) — 4 units placed separately; parser only processes each unit's own pins
5. **Power symbols** (#PWR01, #PWR02) — their `Value` property (GND, +3V3, etc.) IS the net name; they don't use wire labels
6. **Direct pin-to-pin connections** — two pins at the same schematic coordinate are electrically connected with no wire
7. **Same-name label unioning** — two labels with the same text on the same sheet are one net even if not wired together

**Known pin offsets for common KiCad standard library symbols (verified in this project):**
- `Device:R` — pins at local (0, ±3.81). Default angle=0 (vertical): pin1 top, pin2 bottom. At angle=90 (horizontal): pin1 left=(cx−3.81,cy), pin2 right=(cx+3.81,cy)
- `Device:LED` — K at local (−3.81,0), A at local (+3.81,0). At angle=0: K left, A right. At angle=180: A left=(cx−3.81,cy), K right=(cx+3.81,cy)
- **Always verify** by reading the `lib_symbols` section of the schematic — embedded copies can differ from the system library

**`kicad_apply.py` implementation notes:**
- Uses **raw S-expression text editing** — no kiutils round-trip
- Safe to run with plain `python3` (no conda env required — no kiutils dependency)
- Previous kiutils-based version corrupted symbols on save: reset refs to `R?`, dropped `(hide yes)` flags, jumbled property positions

**Net naming conventions (Neo project):**
- `/GND`, `/3V3`, `/5V`, `/24V` — power rails (cross-sheet global labels)
- `SheetName::GND` — a sheet-local GND label NOT wired to the global GND label in that sheet (possible ERC issue)
- `/D18`–`/D35` — ESP32 GPIO signals (global labels)
- `/IN1`–`/IN4` — 24V digital inputs via optocouplers
- `/O1`–`/O4` — ULN2803 output signals
- `[Sheet::]net_X_Y` — unlabeled internal node at schematic coordinate (X, Y); these are real connections, not errors
