"""
kstack_config — shared path resolver for every Kstack skill.

Resolution order (first hit wins):

    1. Explicit CLI flag passed in by the caller.
    2. Environment variable (KSTACK_<KEY>, uppercase).
    3. ~/.config/kstack/config.yaml
    4. A sensible default for common KiCad installs.

The config file is intentionally tiny YAML so non-Python users can edit it:

    # ~/.config/kstack/config.yaml
    kicad_projects_dir:    ~/Documents/kicad
    prasad_dir:            ~/Documents/PRASAD/05326/Footprint
    stock_symbols_dir:     /usr/share/kicad/symbols
    stock_footprints_dir:  /usr/share/kicad/footprints
    knowledge_dir:         ~/kc/kicad-knowledge
    edgecut_lib:           ~/kc/kicad-edgecuts/lib
    fp_index_path:         ~/kc/kicad-footprints/index.yaml
    download_dir:          ~/Documents/footprints

CLI::

    python3 -m kstack_config init     # interactive wizard
    python3 -m kstack_config show     # print resolved paths
    python3 -m kstack_config path kicad_projects_dir   # print one value

From Python::

    from kstack_config import cfg
    root = cfg("kicad_projects_dir")                # Path
    root = cfg("kicad_projects_dir", override=args.root)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

# ---------------------------------------------------------------------------
# Defaults — reasonable for a stock KiCad 9 install on Linux
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(os.environ.get("KSTACK_CONFIG",
                                  Path.home() / ".config/kstack/config.yaml"))

DEFAULTS: dict[str, str] = {
    # Where your existing KiCad projects live (used to mine knowledge).
    "kicad_projects_dir":   "~/Documents/kicad",
    # Vendor / organisation-specific symbol+footprint dir.  May not exist.
    "prasad_dir":           "~/Documents/PRASAD/05326/Footprint",
    # KiCad 9 stock libraries.  Override if you installed to a non-standard prefix.
    "stock_symbols_dir":    "/usr/share/kicad/symbols",
    "stock_footprints_dir": "/usr/share/kicad/footprints",
    # Outputs Kstack writes to.
    "knowledge_dir":        "~/kc/kicad-knowledge",
    "edgecut_lib":          "~/kc/kicad-edgecuts/lib",
    "fp_index_path":        "~/kc/kicad-footprints/index.yaml",
    # Where freshly downloaded symbols/footprints should be dropped.
    "download_dir":         "~/Documents/footprints",
}

# Human-readable prompt text for `kstack init`.
PROMPTS: dict[str, str] = {
    "kicad_projects_dir":
        "Root folder of your existing KiCad projects "
        "(used to build the knowledge graph and footprint-usage index)",
    "prasad_dir":
        "Vendor / personal symbol+footprint folder (can be empty if none)",
    "stock_symbols_dir":
        "KiCad 9 stock symbol library directory",
    "stock_footprints_dir":
        "KiCad 9 stock footprint directory (contains *.pretty subdirs)",
    "knowledge_dir":
        "Where to write the knowledge graph (JSON + GraphML + reports)",
    "edgecut_lib":
        "Where to store extracted Edge.Cuts outlines",
    "fp_index_path":
        "Path to the footprint-usage index (YAML built from past projects)",
    "download_dir":
        "Where freshly downloaded symbols/footprints are dropped before "
        "registering them with KiCad",
}

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CACHE: dict[str, Any] | None = None


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve() \
        if p else Path()


def _load_file() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if not CONFIG_PATH.exists() or yaml is None:
        _CACHE = {}
        return _CACHE
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    except Exception as e:
        print(f"warning: could not read {CONFIG_PATH}: {e}", file=sys.stderr)
        data = {}
    _CACHE = data
    return _CACHE


def cfg(key: str, *, override: str | Path | None = None) -> Path:
    """
    Resolve a config key to a Path.

    Precedence: override  ➜  env var  ➜  config file  ➜  DEFAULTS.
    """
    if override:
        return _expand(str(override))

    env_key = "KSTACK_" + key.upper()
    if env_key in os.environ and os.environ[env_key]:
        return _expand(os.environ[env_key])

    file_data = _load_file()
    if key in file_data and file_data[key]:
        return _expand(str(file_data[key]))

    if key in DEFAULTS:
        return _expand(DEFAULTS[key])

    raise KeyError(f"unknown config key: {key}")


def cfg_str(key: str, *, override: str | None = None) -> str:
    return str(cfg(key, override=override))


def resolved() -> dict[str, Path]:
    """Return every configured key → resolved Path."""
    return {k: cfg(k) for k in DEFAULTS}


def is_initialised() -> bool:
    return CONFIG_PATH.exists()


# ---------------------------------------------------------------------------
# Interactive init
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str) -> str:
    try:
        val = input(f"  {prompt}\n    [{default}]: ").strip()
    except EOFError:
        val = ""
    return val or default


def init_interactive(force: bool = False) -> int:
    if CONFIG_PATH.exists() and not force:
        print(f"{CONFIG_PATH} already exists. Re-run with --force to overwrite.")
        return 1
    if yaml is None:
        print("ERROR: PyYAML is required. `pip install pyyaml`.", file=sys.stderr)
        return 2

    print("Kstack setup — press <enter> to accept each default.\n")
    current = _load_file() if CONFIG_PATH.exists() else {}
    answers: dict[str, str] = {}
    for key, default in DEFAULTS.items():
        seed = current.get(key) or default
        answers[key] = _ask(PROMPTS[key], seed)

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        "# Kstack configuration — edit freely.\n"
        "# Paths may use ~ and ${ENV_VAR}.\n\n"
        + yaml.safe_dump(answers, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    global _CACHE
    _CACHE = None  # invalidate
    print(f"\n✓ Wrote {CONFIG_PATH}\n")
    for k, v in resolved().items():
        tag = "" if v.exists() else "  (does not exist yet — will be created on first use)"
        print(f"    {k:<22} {v}{tag}")
    return 0


def show() -> int:
    if not CONFIG_PATH.exists():
        print(f"No config at {CONFIG_PATH} — using built-in defaults.")
        print("Run `python3 -m kstack_config init` to customise.\n")
    for k, v in resolved().items():
        exists = "✓" if v.exists() else "✗"
        print(f"  {exists} {k:<22} {v}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kstack-config",
            description="Manage Kstack path configuration.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_init = sub.add_parser("init", help="interactive setup wizard")
    sp_init.add_argument("--force", action="store_true",
                         help="overwrite existing config")

    sub.add_parser("show", help="print resolved paths")

    sp_path = sub.add_parser("path", help="print one resolved path")
    sp_path.add_argument("key", choices=sorted(DEFAULTS))

    args = p.parse_args(argv)
    if args.cmd == "init":
        return init_interactive(force=args.force)
    if args.cmd == "show":
        return show()
    if args.cmd == "path":
        print(cfg(args.key))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
