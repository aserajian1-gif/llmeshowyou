# llmeshowyou

**Language-aware file-to-markdown mapper** that generates compact structural maps of source files for LLM consumption. Maps reduce token costs by ~80% compared to feeding raw source code.

Instead of dumping entire source files into your LLM context, llmeshowyou extracts classes, functions, signatures, imports, constants, section markers, and TODOs into a token-efficient `.map.md` file. The LLM reads the map to navigate the codebase, then reads only the specific lines it needs.

## Real-World Savings

| Metric | Without Map | With Map | Savings |
|---|---|---|---|
| Input tokens per edit session | ~51,400 | ~11,500 | **~78%** |
| Cost per session (Sonnet 4.6) | ~$1.03 | ~$0.23 | **~$0.80** |
| Context window saturation | Full file (1,714 lines) | Targeted sections only | **~10x less** |

These are actual measured numbers from a real edit session adding a Claude Code button to the GUI (spanning 6 methods across 3 UI locations in an 1,714-line file). The map let the LLM pinpoint the exact 9 insertion points from 114 lines of structured metadata rather than reading the entire file.

## Features

- **Multi-language parsing** — Python (AST), 80+ languages (regex fallback), 12 languages (tree-sitter)
- **Token-efficient maps** — class/function signatures, imports, constants, section markers, TODOs
- **Change detection** — SHA-256 hashing to detect stale maps; major-change flag for structural edits
- **Project graph** — import dependency graph with god-node ranking
- **Graph exports** — HTML, GraphML, Neo4j Cypher, Wiki format
- **Cross-file queries** — shortest path, node explanation, BFS/DFS traversal
- **MCP server** — LLM-friendly API over stdio or HTTP
- **File watcher** — auto-regenerate maps on source changes
- **Git hooks** — post-commit auto-update integration
- **Cost tracker** — tracks tokens saved per session
- **Windows GUI** — tkinter-based with tree view, opencode/Claude Code launch, and context menu

## Quick Start

```bash
# Map a single file
python llmeshowyou.py map myfile.py

# Map a folder recursively
python llmeshowyou.py map src/ --recursive

# Status check
python llmeshowyou.py status src/

# Update changed files only
python llmeshowyou.py update src/

# Launch GUI (Windows)
python llmeshowyou_gui.py
```

## LLM Integration

The generated `.map.md` files are designed to be the first thing an LLM reads when asked about a codebase. The map format includes a read protocol instructing the LLM to:

1. Read the map first (114 lines vs 1,714 lines of source)
2. Navigate to specific sections using `#[Section]` markers
3. Read only the relevant source lines
4. Use cross-file maps for imported dependencies

Integration with [opencode](https://opencode.ai) and Claude Code is built into the GUI — select files and launch directly with the map-aware prompt.

## Dependencies

Zero required. Core functionality uses only Python 3.10+ standard library.

Optional extras:
- **tree-sitter** — enhanced multi-language parsing
- **flask** — MCP server over HTTP

## License

MIT
