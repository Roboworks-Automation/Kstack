# kstack common

Shared helpers imported by every Kstack skill.

## kstack_config

Resolves paths that used to be hardcoded. Order of precedence:

1. Explicit CLI flag
2. Environment variable (`KSTACK_<KEY>`)
3. `~/.config/kstack/config.yaml`
4. Built-in default

### One-time setup

```bash
python3 ~/.claude/skills/common/kstack_config.py init
```

Walks you through every path; hit ↵ to accept each default.

### Inspect

```bash
python3 ~/.claude/skills/common/kstack_config.py show
python3 ~/.claude/skills/common/kstack_config.py path kicad_projects_dir
```

### Override per-invocation

```bash
KSTACK_KICAD_PROJECTS_DIR=/opt/boards python3 …
```

### Keys

| Key | Purpose |
|---|---|
| `kicad_projects_dir` | Tree scanned for existing projects |
| `prasad_dir` | Personal symbol/footprint folder (optional) |
| `stock_symbols_dir` | KiCad 9 stock symbols (`/usr/share/kicad/symbols`) |
| `stock_footprints_dir` | KiCad 9 stock footprints (`/usr/share/kicad/footprints`) |
| `knowledge_dir` | Output of `kicad-block-extract` |
| `edgecut_lib` | Output of `kicad-edgecut extract` |
| `fp_index_path` | YAML built from past projects' footprints |
| `download_dir` | Where new symbols/footprints land before being registered |
