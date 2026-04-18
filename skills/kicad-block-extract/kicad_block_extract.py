#!/usr/bin/env python3
"""
kicad_block_extract.py — functional peripheral knowledge graph builder.

For each KiCad project found under <path>:
  1. Parses full net connectivity (including hierarchical sheets) via kicad_parse.py
  2. Classifies every component using component_roles.yaml
  3. For each MCU, finds which non-power nets reach each classified peripheral,
     recording which MCU pin names those connections use

Cross-project aggregation:
  - Groups peripherals by (role, family) — e.g., all RS485 SN65HVD instances
  - Only retains peripheral types seen in >= --min-projects projects (default 2)
  - Per MCU->peripheral edge: typical pin count + per-project pin name lists

Outputs:
    <out>/knowledge_graph.json    — nodes + edges (machine-readable)
    <out>/KNOWLEDGE_GRAPH.md      — human-readable graph with pin detail
    <out>/INDEX.md                — per-project component summary

Usage:
    conda run -n kicad-agent python3 kicad_block_extract.py <path> [--out DIR]
    python3 kicad_block_extract.py <path> [--out DIR]   (if kicad_parse works without conda)

<path> may be:
    - a .kicad_pro file           (single project)
    - a project directory         (contains .kicad_pro)
    - a parent directory          (scanned recursively for .kicad_pro)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Locate shared skill directories
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent
KICAD_SKILL_DIR = SKILL_DIR.parent / "kicad"

sys.path.insert(0, str(KICAD_SKILL_DIR))

try:
    import kicad_parse  # type: ignore
except Exception as e:
    print(f"ERROR: cannot import kicad_parse from {KICAD_SKILL_DIR}: {e}", file=sys.stderr)
    print("       Try: conda run -n kicad-agent python3 kicad_block_extract.py ...", file=sys.stderr)
    sys.exit(2)

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Role classification  (rule set lives in rules/component_roles.yaml)
# ---------------------------------------------------------------------------

DEFAULT_RULES_PATH = SKILL_DIR / "rules" / "component_roles.yaml"


@dataclass
class Rule:
    role: str
    family: str
    regex: re.Pattern


def load_rules(path: Path) -> list[Rule]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        Rule(
            role=r["role"],
            family=r.get("family", r["role"]),
            regex=re.compile(r["match"], re.IGNORECASE),
        )
        for r in data.get("rules", [])
    ]


def classify(value: str, lib_id: str, rules: list[Rule]) -> tuple[str, str]:
    """Return (role, family). Checks value first, then lib_id bare part."""
    lib_part = lib_id.split(":", 1)[1] if ":" in lib_id else lib_id
    for probe in (value, lib_part, lib_id):
        if not probe:
            continue
        for r in rules:
            if r.regex.search(probe):
                return r.role, r.family
    return "unknown", "unknown"


# ---------------------------------------------------------------------------
# Power net detection
# ---------------------------------------------------------------------------

_POWER_RE = re.compile(
    r"^[/+-]*(GND[A-Z0-9_]*"
    r"|V(CC|DD|SS|EE|BUS)[A-Z0-9_]*"
    r"|AGND|DGND|PGND|EARTH|SHIELD"
    r"|[+-]?\d+V\d*"
    r"|[0-9]+V[0-9]+)$"
)


def is_power_net(name: str) -> bool:
    if not name:
        return False
    n = name.split("::", 1)[1] if "::" in name else name
    return bool(_POWER_RE.match(n.lstrip("/+-")))


def strip_net_prefix(net: str) -> str:
    """Remove sheet scope prefix for display: 'DIO::TXD' -> 'TXD'."""
    return net.split("::", 1)[1] if "::" in net else net


def clean_net(net: str) -> str:
    """Strip leading slash and sheet prefix."""
    n = strip_net_prefix(net)
    return n.lstrip("/")


# ---------------------------------------------------------------------------
# Which peripheral roles are "interesting" for the knowledge graph
# ---------------------------------------------------------------------------

PERIPHERAL_ROLES = {
    "rs485", "can", "ethernet_mac", "ethernet_mag",
    "usb_serial", "wireless_mod",
    "adc", "dac", "rtc", "eeprom", "flash",
    "display_drv", "display",
    "opto", "driver", "mosfet", "relay",
    "sensor",
    "opamp", "logic", "level_shifter",
    "ldo", "buck", "boost", "dcdc_isolated", "charger",
    "connector",
}


# ---------------------------------------------------------------------------
# Per-project extraction
# ---------------------------------------------------------------------------

def _pin_display_name(pin_key: str) -> str:
    """'30:IO18' -> 'IO18',  '1:1' -> '1',  '5' -> '5'."""
    if ":" in pin_key:
        num, name = pin_key.split(":", 1)
        # If both parts are pure numbers, just use the name side
        return name if name != num else num
    return pin_key


def find_projects(root: Path) -> list[Path]:
    if root.is_file() and root.suffix == ".kicad_pro":
        return [root]
    if root.is_dir():
        pros = [
            p for p in root.rglob("*.kicad_pro")
            if "backup" not in str(p).lower() and "__MACOSX" not in str(p)
        ]
        return sorted(pros)
    return []


def extract_project(pro_path: Path, rules: list[Rule]) -> dict[str, Any]:
    """
    Parse one KiCad project and return MCU->peripheral edges with pin details.

    Returns dict with keys:
        project, sheets, edges (list of edge dicts), stats, error (on failure)
    """
    # Locate root schematic
    root_sch = pro_path.with_suffix(".kicad_sch")
    if not root_sch.exists():
        candidates = [
            c for c in pro_path.parent.glob("*.kicad_sch")
            if ".bak" not in c.name and "-backups" not in str(c)
        ]
        if len(candidates) == 1:
            root_sch = candidates[0]
        else:
            return {
                "project": pro_path.stem,
                "error": f"no root .kicad_sch (found {len(candidates)} candidates)",
            }

    try:
        data = kicad_parse.parse(root_sch)
    except Exception as exc:
        return {"project": pro_path.stem, "error": f"parse failed: {exc}"}

    project = pro_path.stem

    # --- Build component map: ref -> {value, lib_id, pins: {pin_key: net}} -
    comp_by_ref: dict[str, dict] = {}
    for c in data.get("components", []):
        ref = c.get("ref", "")
        if not ref or ref.startswith("#"):
            continue
        if ref in comp_by_ref:
            # Multi-unit symbol: merge pins from repeated entries
            comp_by_ref[ref]["pins"].update(c.get("pins", {}))
        else:
            comp_by_ref[ref] = {
                "ref":    ref,
                "value":  c.get("value", "") or "",
                "lib_id": c.get("lib_id", "") or "",
                "pins":   dict(c.get("pins", {})),
            }

    # --- Classify every component ------------------------------------------
    classified: dict[str, tuple[str, str]] = {
        ref: classify(c["value"], c["lib_id"], rules)
        for ref, c in comp_by_ref.items()
    }

    # --- Net -> refs index (signal nets only) -------------------------------
    net_to_refs: dict[str, list[str]] = defaultdict(list)
    for ref, c in comp_by_ref.items():
        for net in c["pins"].values():
            if net and not is_power_net(net):
                net_to_refs[net].append(ref)

    # --- Identify MCU and peripheral components ----------------------------
    mcu_refs = [r for r, (role, _) in classified.items() if role == "mcu"]
    peri_refs = {r for r, (role, _) in classified.items() if role in PERIPHERAL_ROLES}

    # --- Build MCU->peripheral edges ---------------------------------------
    edges: list[dict] = []
    for mcu_ref in mcu_refs:
        mcu_comp = comp_by_ref[mcu_ref]
        _, mcu_family = classified[mcu_ref]

        # net -> mcu_pin_key  (for each signal net the MCU touches)
        mcu_net_pins: dict[str, str] = {
            net: pin_key
            for pin_key, net in mcu_comp["pins"].items()
            if net and not is_power_net(net)
        }

        # Group by peripheral ref: peri_ref -> {net: mcu_pin_key}
        peri_to_nets: dict[str, dict[str, str]] = defaultdict(dict)
        for net, pin_key in mcu_net_pins.items():
            for ref in net_to_refs.get(net, []):
                if ref != mcu_ref and ref in peri_refs:
                    peri_to_nets[ref][net] = pin_key

        for peri_ref, net_to_pin in peri_to_nets.items():
            peri_comp = comp_by_ref[peri_ref]
            peri_role, peri_family = classified[peri_ref]

            mcu_pin_names = sorted(
                _pin_display_name(pkey) for pkey in net_to_pin.values()
            )
            net_names = sorted(clean_net(n) for n in net_to_pin.keys())

            edges.append({
                "project":          project,
                "mcu_ref":          mcu_ref,
                "mcu_value":        mcu_comp["value"],
                "mcu_family":       mcu_family,
                "peri_ref":         peri_ref,
                "peri_value":       peri_comp["value"],
                "peri_role":        peri_role,
                "peri_family":      peri_family,
                "mcu_pins":         mcu_pin_names,
                "nets":             net_names,
            })

    # --- Catalog ALL peripherals (regardless of MCU connection) -----------
    # This captures power regulators, relays, etc. that never touch an MCU pin.
    all_peripherals: list[dict] = [
        {
            "role":   classified[r][0],
            "family": classified[r][1],
            "value":  comp_by_ref[r]["value"],
        }
        for r in peri_refs
    ]

    stats = {
        "components": len(comp_by_ref),
        "mcus":        len(mcu_refs),
        "peripherals": len(peri_refs),
        "edges":       len(edges),
    }

    return {
        "project":         project,
        "sheets":          data.get("sheets", []),
        "edges":           edges,
        "all_peripherals": all_peripherals,
        "stats":           stats,
    }


# ---------------------------------------------------------------------------
# Cross-project knowledge graph builder
# ---------------------------------------------------------------------------

def build_knowledge_graph(projects: list[dict], min_projects: int = 1) -> dict:
    """
    Aggregate MCU->peripheral edges AND a standalone catalog of ALL peripheral
    blocks (including power regulators, relays, etc. not connected to any MCU).

    Returns:
        {
            "nodes":              [mcu_node, ..., peri_node, ...],
            "edges":              [edge, ...],
            "standalone_blocks":  [block, ...],   # all peripherals, no MCU pin data
        }
    """
    # ---- MCU-connected edge aggregation ------------------------------------
    # (mcu_value, peri_family, peri_role) -> bucket
    edge_agg: dict[tuple, dict] = defaultdict(lambda: {
        "projects": set(),
        "peri_values": set(),
        "pins_by_project": {},
    })

    mcu_projects: dict[str, set] = defaultdict(set)
    mcu_families: dict[str, str] = {}

    for proj in projects:
        if "error" in proj:
            continue
        for e in proj.get("edges", []):
            key = (e["mcu_value"], e["peri_family"], e["peri_role"])
            b = edge_agg[key]
            b["projects"].add(e["project"])
            b["peri_values"].add(e["peri_value"])
            existing = set(b["pins_by_project"].get(e["project"], []))
            existing.update(e["mcu_pins"])
            b["pins_by_project"][e["project"]] = sorted(existing)
            mcu_projects[e["mcu_value"]].add(e["project"])
            mcu_families[e["mcu_value"]] = e["mcu_family"]

    qualified = {k: v for k, v in edge_agg.items() if len(v["projects"]) >= min_projects}

    # ---- Standalone peripheral catalog (ALL peripherals, no MCU filter) ----
    # (family, role) -> {projects: set, values: set}
    all_peri_agg: dict[tuple, dict] = defaultdict(lambda: {
        "projects": set(),
        "values": set(),
    })
    for proj in projects:
        if "error" in proj:
            continue
        for p in proj.get("all_peripherals", []):
            key = (p["family"], p["role"])
            all_peri_agg[key]["projects"].add(proj["project"])
            all_peri_agg[key]["values"].add(p["value"])

    # Apply min_projects filter to standalone catalog too
    standalone_blocks = []
    for (family, role), agg in sorted(all_peri_agg.items()):
        if len(agg["projects"]) < min_projects:
            continue
        standalone_blocks.append({
            "id":            f"{role}/{family}",
            "role":          role,
            "family":        family,
            "known_parts":   sorted(agg["values"] - {"", "~"}),
            "projects":      sorted(agg["projects"]),
            "project_count": len(agg["projects"]),
            "has_mcu_connection": any(
                k[1] == family and k[2] == role for k in qualified
            ),
        })

    # ---- MCU nodes ---------------------------------------------------------
    mcu_values_in_graph = {k[0] for k in qualified}
    mcu_nodes = [
        {
            "id":            mv,
            "type":          "mcu",
            "family":        mcu_families.get(mv, ""),
            "projects":      sorted(mcu_projects[mv]),
            "project_count": len(mcu_projects[mv]),
        }
        for mv in sorted(mcu_values_in_graph)
    ]

    # ---- Peripheral nodes (MCU-connected only, for graph edges) ------------
    peri_info: dict[tuple, dict] = {}
    for (mcu_value, peri_family, peri_role), b in qualified.items():
        pk = (peri_family, peri_role)
        if pk not in peri_info:
            peri_info[pk] = {
                "id":            f"{peri_role}/{peri_family}",
                "type":          "peripheral",
                "role":          peri_role,
                "family":        peri_family,
                "known_parts":   set(),
                "projects":      set(),
            }
        peri_info[pk]["known_parts"].update(b["peri_values"])
        peri_info[pk]["projects"].update(b["projects"])

    peri_nodes = [
        {
            "id":            pd["id"],
            "type":          "peripheral",
            "role":          pd["role"],
            "family":        pd["family"],
            "known_parts":   sorted(pd["known_parts"] - {"", "~"}),
            "projects":      sorted(pd["projects"]),
            "project_count": len(pd["projects"]),
        }
        for _, pd in sorted(peri_info.items())
    ]

    # --- Edges --------------------------------------------------------------
    graph_edges = []
    for (mcu_value, peri_family, peri_role), b in sorted(
        qualified.items(), key=lambda kv: (-len(kv[1]["projects"]), kv[0])
    ):
        pin_counts = [len(pins) for pins in b["pins_by_project"].values()]
        typical = sorted(pin_counts)[len(pin_counts) // 2] if pin_counts else 0
        pin_range = (
            f"{min(pin_counts)}\u2013{max(pin_counts)}" if pin_counts else "?"
        )

        all_pins: Counter = Counter()
        for pins in b["pins_by_project"].values():
            for p in pins:
                all_pins[p] += 1
        common_pins = [pin for pin, _ in all_pins.most_common(20)]

        graph_edges.append({
            "mcu":                mcu_value,
            "peripheral":         f"{peri_role}/{peri_family}",
            "peripheral_role":    peri_role,
            "peripheral_family":  peri_family,
            "projects":           sorted(b["projects"]),
            "project_count":      len(b["projects"]),
            "typical_pin_count":  typical,
            "pin_range":          pin_range,
            "common_mcu_pins":    common_pins,
            "per_project":        {
                proj: pins
                for proj, pins in sorted(b["pins_by_project"].items())
            },
        })

    return {
        "nodes":             mcu_nodes + peri_nodes,
        "edges":             graph_edges,
        "standalone_blocks": standalone_blocks,
    }


# ---------------------------------------------------------------------------
# Markdown emitters
# ---------------------------------------------------------------------------

def emit_knowledge_graph_md(graph: dict, min_projects: int) -> str:
    nodes = graph["nodes"]
    edges = graph["edges"]
    mcu_nodes  = [n for n in nodes if n["type"] == "mcu"]
    peri_nodes = [n for n in nodes if n["type"] == "peripheral"]

    lines = [
        "# KiCad Functional Knowledge Graph",
        "",
        f"Peripheral blocks seen in \u2265{min_projects} projects, "
        "with MCU pin requirements.",
        "",
        f"**{len(mcu_nodes)} MCU type(s)** \u2014 "
        f"**{len(peri_nodes)} peripheral type(s)** \u2014 "
        f"**{len(edges)} edge(s)**",
        "",
    ]

    # MCU nodes
    lines += ["## MCU Nodes", ""]
    lines += ["| MCU Part | Family | Projects |", "|---|---|---|"]
    for n in mcu_nodes:
        lines.append(
            f"| **{n['id']}** | {n['family']} | "
            f"{n['project_count']}: {', '.join(n['projects'][:8])}"
            f"{'…' if n['project_count'] > 8 else ''} |"
        )
    lines.append("")

    # Peripheral nodes
    lines += ["## Peripheral Nodes", ""]
    lines += [
        "| Peripheral ID | Role | Known Parts | Projects |",
        "|---|---|---|---|",
    ]
    for n in peri_nodes:
        parts = ", ".join(n["known_parts"][:5]) or "\u2014"
        proj_tail = "\u2026" if n["project_count"] > 6 else ""
        lines.append(
            f"| **{n['id']}** | {n['role']} | {parts} | "
            f"{n['project_count']}: {', '.join(n['projects'][:6])}"
            f"{proj_tail} |"
        )
    lines.append("")

    # Edge summary table
    lines += [
        "## MCU \u2192 Peripheral Edges",
        "",
        "| MCU | Peripheral | Projects | MCU pins | Common pin names |",
        "|---|---|---|---|---|",
    ]
    for e in edges:
        pins_str = ", ".join(e["common_mcu_pins"][:8])
        if len(e["common_mcu_pins"]) > 8:
            pins_str += "…"
        lines.append(
            f"| {e['mcu']} | {e['peripheral']} | "
            f"{e['project_count']} | {e['pin_range']} | {pins_str} |"
        )
    lines.append("")

    # Per-MCU detail
    lines += ["## Detail by MCU", ""]
    by_mcu: dict[str, list] = defaultdict(list)
    for e in edges:
        by_mcu[e["mcu"]].append(e)

    for mcu_val in sorted(by_mcu):
        lines += [f"### {mcu_val}", ""]
        for e in sorted(by_mcu[mcu_val],
                        key=lambda x: (-x["project_count"], x["peripheral"])):
            lines.append(
                f"**{e['peripheral']}** \u2014 {e['project_count']} project(s), "
                f"{e['pin_range']} MCU pin(s)"
            )
            lines.append("")
            lines.append("| Project | MCU pins used |")
            lines.append("|---|---|")
            for proj, pins in sorted(e["per_project"].items()):
                lines.append(f"| {proj} | {', '.join(pins)} |")
            lines.append("")

    return "\n".join(lines) + "\n"


def emit_block_yaml(peri_node: dict, edges_for_peri: list[dict]) -> str:
    """
    Emit a YAML file for one reusable peripheral block.

    Captures:
      - what the block IS (role, family, known part numbers)
      - which projects use it
      - for each MCU it connects to: which MCU pins are needed and in how many projects
    """
    role   = peri_node["role"]
    family = peri_node["family"]
    parts  = peri_node["known_parts"]
    projs  = peri_node["projects"]

    lines = [
        f"# Reusable block: {role}/{family}",
        f"# Auto-extracted from {len(projs)} project(s). Edit to annotate.",
        f"block: {role}/{family}",
        f"role: {role}",
        f"family: {family}",
        f"status: draft",
        f"",
        f"known_parts:",
    ]
    if parts:
        for p in parts:
            lines.append(f"  - \"{p}\"")
    else:
        lines.append("  []")

    lines += [
        f"",
        f"projects_seen: {len(projs)}",
        f"projects:",
    ]
    for p in projs:
        lines.append(f"  - {p}")

    if edges_for_peri:
        lines += ["", "# MCU connections (this block is wired to MCU pins)", "mcu_connections:"]
        for e in sorted(edges_for_peri, key=lambda x: -x["project_count"]):
            pin_range = e["pin_range"]
            lines += [
                f"  - mcu: \"{e['mcu']}\"",
                f"    projects: {e['project_count']}",
                f"    pin_count_range: \"{pin_range}\"",
                f"    common_pins: {e['common_mcu_pins'][:10]}",
                f"    per_project:",
            ]
            for proj, pins in sorted(e["per_project"].items()):
                lines.append(f"      {proj}: {pins}")
    else:
        lines += [
            "",
            "# No direct MCU pin connections detected.",
            "# This block is standalone (power supply, relay driver, etc.)",
            "mcu_connections: []",
        ]

    lines += [
        "",
        "# Human annotations (fill in before using downstream):",
        "tested: false",
        "tags: []",
        "notes: \"\"",
    ]
    return "\n".join(lines) + "\n"


def emit_readme(graph: dict, min_projects: int) -> str:
    n_mcu  = sum(1 for n in graph["nodes"] if n["type"] == "mcu")
    n_peri = sum(1 for n in graph["nodes"] if n["type"] == "peripheral")
    peri_names = [n["id"] for n in graph["nodes"] if n["type"] == "peripheral"]

    lines = [
        "# KiCad Block Library — README",
        "",
        "Generated by `kicad_block_extract.py` from all projects under `~/Documents`.",
        "",
        "## What is in this folder",
        "",
        "| File / Folder | Description |",
        "|---|---|",
        "| `blocks/`            | One YAML per reusable peripheral block type |",
        "| `KNOWLEDGE_GRAPH.md` | Human-readable graph: MCU nodes → peripheral nodes with pin tables |",
        "| `knowledge_graph.json` | Same graph in JSON for tooling |",
        "| `INDEX.md`           | Per-project summary (components, MCUs, edges) |",
        "",
        "## Two skills — what each does",
        "",
        "### `/kicad-block-extract`  ← THIS is what ran",
        "",
        "Parses every KiCad project, classifies components, then finds",
        "which peripheral ICs (RS485 transceiver, HX711, optocoupler, …)",
        "connect to which MCU pins.  Only keeps peripherals seen in",
        f"**≥{min_projects} distinct projects** — those are the reusable blocks.",
        "",
        "Outputs:",
        "- `blocks/<role>_<family>.yaml` — one per block type (the extracted blocks)",
        "- `KNOWLEDGE_GRAPH.md` / `knowledge_graph.json` — the MCU→block graph",
        "- `INDEX.md` — per-project inventory",
        "",
        "### `/kicad-knowledge`  ← NOT run here",
        "",
        "A heavier analysis tool that produces cross-project reports:",
        "MCU×peripheral matrix, pin-use conventions, power topology, GraphML export.",
        "It is useful for deep analysis but does not produce block YAML files.",
        "",
        "## Extracted blocks",
        "",
        f"**{n_peri} peripheral block type(s)** found in ≥{min_projects} projects:",
        "",
    ]
    for name in sorted(peri_names):
        lines.append(f"- `blocks/{name.replace('/', '_')}.yaml`")

    lines += [
        "",
        "## MCU types seen",
        "",
        f"**{n_mcu} MCU type(s)**:",
        "",
    ]
    for n in graph["nodes"]:
        if n["type"] == "mcu":
            lines.append(f"- **{n['id']}** ({n['family']}) — {n['project_count']} project(s)")

    lines += [
        "",
        "## How to use a block",
        "",
        "1. Open the relevant `blocks/*.yaml`",
        "2. Check `mcu_connections` → `common_pins` to know which GPIO lines you need",
        "3. Copy the block schematic from one of the listed projects",
        "4. Set `tested: true` once validated on hardware",
    ]
    return "\n".join(lines) + "\n"


def emit_index(projects: list[dict]) -> str:
    lines = [
        "# Project index",
        "",
        "| Project | Sheets | Components | MCUs | Peripherals | Edges | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for p in projects:
        if "error" in p:
            lines.append(
                f"| {p.get('project', '?')} | \u2014 | \u2014 | "
                f"\u2014 | \u2014 | \u2014 | {p['error']} |"
            )
            continue
        stats = p.get("stats", {})
        sheet_names = ", ".join(
            Path(s).stem for s in p.get("sheets", [])
        )
        sheet_col = sheet_names or "\u2014"
        lines.append(
            f"| {p['project']} | {sheet_col} | "
            f"{stats.get('components', '?')} | "
            f"{stats.get('mcus', '?')} | "
            f"{stats.get('peripherals', '?')} | "
            f"{stats.get('edges', '?')} | |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("path", type=Path, help="Project dir, .kicad_pro, or parent dir")
    p.add_argument("--out", type=Path, default=Path("./blocks"),
                   help="Output directory (default: ./blocks)")
    p.add_argument("--min-projects", type=int, default=1,
                   help="Minimum distinct projects for a block to be included (default 1 = all)")
    p.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH,
                   help="Path to component_roles.yaml")
    args = p.parse_args()

    if not args.rules.exists():
        print(f"ERROR: rules file not found: {args.rules}", file=sys.stderr)
        return 1

    rules = load_rules(args.rules)
    project_files = find_projects(args.path.expanduser().resolve())
    if not project_files:
        print(f"No .kicad_pro found under {args.path}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    errors = 0

    print(f"Scanning {len(project_files)} project(s)...")
    for pro in project_files:
        r = extract_project(pro, rules)
        if "error" in r:
            print(f"  skip  {pro.stem}: {r['error']}")
            errors += 1
        else:
            s = r["stats"]
            print(f"  ok    {pro.stem}: {s['components']} comps, "
                  f"{s['mcus']} MCU(s), {s['peripherals']} periph, "
                  f"{s['edges']} edges")
        results.append(r)

    graph = build_knowledge_graph(results, args.min_projects)

    # --- blocks/ subfolder: one YAML per peripheral type ------------------
    # Uses the standalone_blocks catalog (ALL peripherals, not just MCU-connected)
    blocks_dir = args.out / "blocks"
    blocks_dir.mkdir(parents=True, exist_ok=True)

    for block in graph["standalone_blocks"]:
        bid = block["id"]   # e.g. "rs485/sn65hvd"
        edges_for_block = [e for e in graph["edges"] if e["peripheral"] == bid]
        fname = bid.replace("/", "_") + ".yaml"
        (blocks_dir / fname).write_text(
            emit_block_yaml(block, edges_for_block), encoding="utf-8"
        )

    n_blocks = len(graph["standalone_blocks"])
    n_with_mcu = sum(1 for b in graph["standalone_blocks"] if b["has_mcu_connection"])
    n_standalone = n_blocks - n_with_mcu

    # --- top-level files --------------------------------------------------
    (args.out / "knowledge_graph.json").write_text(
        json.dumps(graph, indent=2), encoding="utf-8"
    )
    (args.out / "KNOWLEDGE_GRAPH.md").write_text(
        emit_knowledge_graph_md(graph, args.min_projects), encoding="utf-8"
    )
    (args.out / "README.md").write_text(
        emit_readme(graph, args.min_projects), encoding="utf-8"
    )

    n_mcu = sum(1 for n in graph["nodes"] if n["type"] == "mcu")

    print()
    print(f"{len(results) - errors} project(s) parsed, {errors} skipped.")
    print(f"Blocks: {n_blocks} total  "
          f"({n_with_mcu} MCU-connected with pin data, "
          f"{n_standalone} standalone/power)")
    print(f"Graph:  {n_mcu} MCU type(s), {len(graph['edges'])} MCU\u2192peripheral edge(s)")
    print(f"Output: {args.out.resolve()}")
    print(f"  blocks/              {n_blocks} YAML file(s) \u2014 one per block type")
    print("  README.md            skill explanation + how to use blocks")
    print("  KNOWLEDGE_GRAPH.md   MCU\u2192block graph with per-project pin tables")
    print("  knowledge_graph.json same graph, machine-readable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
