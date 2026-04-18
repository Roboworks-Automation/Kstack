#!/usr/bin/env python3
"""
kicad_assemble.py — design-file driven assembler.

Reads a design.yaml that declares:
    mcu:        path or name of an MCU yaml (see mcus/*.yaml)
    blocks:     list of block instances referencing curated block YAMLs
    constraints (optional): pin: signal manual overrides
    project:    output project name

Emits into <out>/:
    pinmap.yaml          — full resolved pin table
    pinmap.md            — human-readable pin assignment report
    platformio.ini       — PlatformIO project file
    src/pins.h           — #defines for every assigned signal
    src/main.cpp         — skeleton with Serial.begin and block init hints
    sheets/              — copies of the source block .kicad_sch files (for
                           you to later wire as hier sheets in KiCad GUI)

This is intentionally NOT a full schematic generator. KiCad hierarchical-
sheet wiring is done in eeschema: the fastest path is to open a fresh
project, right-click > Add Sheet, and point it at each file in sheets/.
The assembler's real value is the pinmap + firmware scaffold.

Usage:
    python3 kicad_assemble.py <design.yaml> [--out <dir>] [--blocks-dir <dir>]
                                            [--mcus-dir <dir>]
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Minimal YAML loader (avoid pyyaml dependency on user systems).
# Supports the subset we emit/consume: mappings, lists, scalars, # comments,
# block style with indentation, flow mappings {k: v, ...}.
# For complex files users should install PyYAML — we try it first.
# ---------------------------------------------------------------------------

try:
    import yaml  # type: ignore
    def load_yaml(path: Path) -> Any:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Pin-assignment solver (greedy)
# ---------------------------------------------------------------------------

class Assigner:
    def __init__(self, mcu: dict, constraints: dict[str, str]):
        self.mcu = mcu
        self.constraints = constraints or {}
        # Map gpio-name -> (pin_number, capabilities list, flags)
        self.gpios: dict[str, dict] = {}
        for pin_num, pd in mcu.get("pins", {}).items():
            g = pd.get("gpio")
            if not g or not g.startswith("IO"):
                continue
            self.gpios[g] = {
                "pin": pin_num,
                "capabilities": pd.get("capabilities", []),
                "input_only": pd.get("input_only", False),
                "strapping": pd.get("strapping", False),
                "notes": pd.get("notes", ""),
            }
        self.safe = list(mcu.get("safe_user_gpio", []))
        self.safe_in = list(mcu.get("safe_input_only", []))
        self.strap = set(mcu.get("strapping_pins", []))
        self.reserved = set(mcu.get("reserved", []))
        # Currently assigned: gpio -> signal
        self.assigned: dict[str, str] = {}
        # Output: signal -> (gpio, pin, reason)
        self.map: dict[str, tuple[str, str, str]] = {}
        self.warnings: list[str] = []

    def _try_assign(self, signal: str, gpio: str, reason: str) -> bool:
        if gpio in self.assigned:
            return False
        if gpio in self.reserved:
            return False
        self.assigned[gpio] = signal
        pin = self.gpios[gpio]["pin"]
        self.map[signal] = (gpio, pin, reason)
        if gpio in self.strap:
            self.warnings.append(
                f"{signal} -> {gpio} is a strapping pin — verify external pull"
            )
        return True

    def assign(self, signal: str, hint: dict) -> bool:
        """hint = {direction, type, prefer_capability}"""
        # 1. Manual constraint wins
        if signal in self.constraints:
            g = self.constraints[signal]
            if g not in self.gpios:
                self.warnings.append(f"{signal}: constraint {g} not a known GPIO")
                return False
            return self._try_assign(signal, g, "user-constrained")

        want_cap = hint.get("prefer_capability")
        direction = hint.get("direction", "unknown")
        is_input = direction == "in"

        # 2. Capability-preferred candidates
        if want_cap:
            for g, info in self.gpios.items():
                if want_cap in info["capabilities"] and g not in self.assigned \
                        and g not in self.reserved:
                    if self._try_assign(signal, g, f"capability={want_cap}"):
                        return True

        # 3. Input-only pool for inputs (keeps output-capable pins for outputs)
        if is_input:
            for g in self.safe_in:
                if g in self.gpios and self._try_assign(signal, g, "safe input-only"):
                    return True

        # 4. Safe GPIO pool
        for g in self.safe:
            if g in self.gpios and self._try_assign(signal, g, "safe gpio"):
                return True

        # 5. Any remaining non-reserved, non-strap IO
        for g in self.gpios:
            if g in self.strap or g in self.assigned or g in self.reserved:
                continue
            if self._try_assign(signal, g, "fallback"):
                return True

        # 6. Last resort: strapping pins
        for g in self.strap:
            if g in self.gpios and self._try_assign(signal, g, "strapping (last resort)"):
                return True

        return False


# ---------------------------------------------------------------------------
# Signal collection from blocks
# ---------------------------------------------------------------------------

# Names that look like they wire to the MCU (heuristic — user can tag
# explicitly by naming direction='mcu' in the block YAML).
MCU_SIGNAL_HINTS = (
    "_TX", "_RX", "SCL", "SDA", "SCK", "MOSI", "MISO", "CS", "INT", "RST",
    "IO", "GPIO", "PWM", "ADC", "DAC", "EN", "TXD", "RXD",
)


def load_block(path: Path) -> dict:
    return load_yaml(path)


def collect_signals(instance: dict, block: dict) -> list[dict]:
    """Return a list of {signal, direction, type, prefer_capability} for
    every signal in this block instance that should bind to the MCU."""
    out = []
    iface = block.get("interface") or {}
    if isinstance(iface, dict):
        for name, meta in iface.items():
            meta = meta or {}
            if meta.get("type") == "power":
                continue
            # If block author tagged mcu_side: false, skip (external-only signal)
            if meta.get("mcu_side") is False:
                continue
            # Heuristic: skip obvious off-board signals
            if not (meta.get("mcu_side") is True
                    or any(h in name.upper() for h in MCU_SIGNAL_HINTS)):
                continue
            # Name in final design = <instance>_<signal>
            full = f"{instance['name']}_{name}"
            out.append({
                "signal": full,
                "block_signal": name,
                "direction": meta.get("direction", "unknown"),
                "type": meta.get("type", "signal"),
                "prefer_capability": meta.get("capability"),
            })
    return out


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------

def emit_pinmap_md(design: dict, mcu: dict, assigner: Assigner) -> str:
    lines = [
        f"# Pinmap — {design.get('project', 'design')}",
        "",
        f"MCU: **{mcu.get('part')}**",
        "",
        "| Signal | GPIO | Pin | Reason |",
        "|---|---|---|---|",
    ]
    for sig, (g, pin, reason) in sorted(assigner.map.items()):
        lines.append(f"| {sig} | {g} | {pin} | {reason} |")
    if assigner.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in assigner.warnings]
    return "\n".join(lines) + "\n"


def emit_pins_h(design: dict, assigner: Assigner) -> str:
    project = design.get("project", "design").upper().replace("-", "_")
    lines = [
        f"// Auto-generated by kicad_assemble.py. Do not edit.",
        f"#ifndef {project}_PINS_H",
        f"#define {project}_PINS_H",
        f"",
    ]
    for sig, (g, _pin, _r) in sorted(assigner.map.items()):
        num = g.replace("IO", "")
        lines.append(f"#define PIN_{sig.upper()} {num}   // {g}")
    lines += ["", f"#endif // {project}_PINS_H", ""]
    return "\n".join(lines)


def emit_platformio_ini(design: dict, mcu: dict, libraries: set[str]) -> str:
    fam = mcu.get("family", "esp32")
    board = design.get("board", "esp32dev" if fam == "esp32" else "genericSTM32F103C8")
    lib_deps = "\n    ".join(sorted(libraries)) if libraries else ""
    return (
        f"; Auto-generated by kicad_assemble.py\n"
        f"[env:{design.get('project', 'main')}]\n"
        f"platform = espressif32\n"
        f"board = {board}\n"
        f"framework = arduino\n"
        f"monitor_speed = 115200\n"
        + (f"lib_deps =\n    {lib_deps}\n" if lib_deps else "")
    )


def emit_main_cpp(design: dict, assigner: Assigner, instances: list[dict]) -> str:
    lines = [
        "// Auto-generated skeleton — kicad_assemble.py",
        "#include <Arduino.h>",
        '#include "pins.h"',
        "",
        "void setup() {",
        "    Serial.begin(115200);",
        "    while (!Serial) {}",
    ]
    for inst in instances:
        hint = inst.get("_driver_hint", "")
        if hint:
            lines.append(f"    // {inst['name']}: {hint}")
    lines += ["}", "", "void loop() {", "    delay(1000);", "}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("design", type=Path)
    p.add_argument("--out", type=Path, default=Path("./build"))
    p.add_argument("--blocks-dir", type=Path,
                   default=Path.home() / "kicad-blocks")
    p.add_argument("--mcus-dir", type=Path,
                   default=Path(__file__).parent / "mcus")
    args = p.parse_args()

    design = load_yaml(args.design)
    mcu_ref = design["mcu"]
    mcu_path = Path(mcu_ref) if Path(mcu_ref).exists() else args.mcus_dir / f"{mcu_ref}.yaml"
    if not mcu_path.exists():
        print(f"ERROR: MCU file not found: {mcu_path}", file=sys.stderr)
        return 1
    mcu = load_yaml(mcu_path)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "src").mkdir(exist_ok=True)
    (args.out / "sheets").mkdir(exist_ok=True)

    # Load each block instance
    instances: list[dict] = []
    all_signals: list[dict] = []
    libraries: set[str] = set()

    for inst in design.get("blocks", []):
        bref = inst["type"]
        bpath = Path(bref) if Path(bref).exists() else args.blocks_dir / f"{bref}.yaml"
        if not bpath.exists():
            print(f"ERROR: block not found: {bpath}", file=sys.stderr)
            return 1
        block = load_block(bpath)
        inst_full = dict(inst)
        inst_full["_block"] = block
        pio = block.get("platformio") or {}
        if pio.get("library"):
            libraries.add(pio["library"])
        inst_full["_driver_hint"] = pio.get("driver_hint", "")
        instances.append(inst_full)
        all_signals.extend(collect_signals(inst_full, block))

        # Copy source sheet for later GUI wire-up
        src_sheet = block.get("source_sheet")
        if src_sheet and Path(src_sheet).exists():
            shutil.copy2(src_sheet, args.out / "sheets" / Path(src_sheet).name)

    # Run assignment
    assigner = Assigner(mcu, design.get("constraints", {}))
    unassigned: list[str] = []
    for sig in all_signals:
        ok = assigner.assign(sig["signal"], sig)
        if not ok:
            unassigned.append(sig["signal"])

    # Write outputs
    (args.out / "pinmap.md").write_text(emit_pinmap_md(design, mcu, assigner),
                                        encoding="utf-8")
    (args.out / "pinmap.yaml").write_text(
        "assignments:\n" + "".join(
            f"  {s}: {{gpio: {g}, pin: '{p}', reason: {r!r}}}\n"
            for s, (g, p, r) in sorted(assigner.map.items())
        ) + (f"\nwarnings:\n" + "".join(f"  - {w}\n" for w in assigner.warnings)
             if assigner.warnings else ""),
        encoding="utf-8",
    )
    (args.out / "src" / "pins.h").write_text(emit_pins_h(design, assigner),
                                             encoding="utf-8")
    (args.out / "src" / "main.cpp").write_text(emit_main_cpp(design, assigner, instances),
                                               encoding="utf-8")
    (args.out / "platformio.ini").write_text(emit_platformio_ini(design, mcu, libraries),
                                             encoding="utf-8")

    # Report
    print(f"Assigned {len(assigner.map)} signals, {len(unassigned)} unassigned")
    for u in unassigned:
        print(f"  UNASSIGNED: {u}")
    for w in assigner.warnings:
        print(f"  WARN: {w}")
    print(f"Output: {args.out.resolve()}")
    return 0 if not unassigned else 1


if __name__ == "__main__":
    sys.exit(main())
