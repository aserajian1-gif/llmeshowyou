# llmeshowyou

**Language-aware file-to-markdown mapper** with AC/DoD discipline scratchpad. Generates compact structural maps of source files for LLM consumption, and provides a process-state harness for LLM task tracking — ticket, acceptance criteria, evidence ledger, gate state. Maps reduce token costs by ~80% compared to feeding raw source code.

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
- **Windows GUI** — tkinter-based with tree view, opencode/Claude Code launch, context menu, AC/DoD discipline panel, and independent review agent
- **Discipline scratchpad** — ticket-based AC/DoD tracking with evidence ledger, gate state, attempt ceiling, and separate-reviewer verification; auto-injected into opencode prompts

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

## LLM Integration — Two Modes

### Mode A: Persistent Maps (original)
Write `.map.md` files to disk. The LLM reads them to navigate the codebase. Fast but can drift if source changes without regeneration.

### Mode B: Ephemeral Maps via MCP Tool (recommended for active development)
No `.map.md` files on disk. Instead, run the MCP server and the LLM calls `map_file` as a tool whenever it needs structural info:

```bash
# Start the MCP server (stdio mode, for opencode)
python llmeshowyou_mcp_server.py
```

Configure in `opencode.json`:
```json
{
  "mcpServers": {
    "llmeshowyou": {
      "command": "python",
      "args": ["path/to/llmeshowyou_mcp_server.py"]
    }
  }
}
```

**Why ephemeral?** Every map is generated fresh from current source on disk. Zero staleness. Zero drift. No maintenance. The LLM gets an accurate structural view every time, even while you're actively editing code.

**Tools provided:**
- `map_file(path)` — fresh structural map for any source file
- `read_source(path, start_line, end_line)` — read specific lines by range
- `map_folder(path)` — index all files in a project folder

## GUI

The Windows GUI (`llmeshowyou_gui.py`) provides a tree view of mapped files with one-click launch to opencode, Claude Code, or your editor — with the map-aware prompt pre-loaded.

Includes an **AC/DoD discipline scratchpad** panel (click **Discipline** in the toolbar):
- Init a ticket with a checklist of acceptance criteria
- The criteria and the AC/DoD protocol are auto-injected into opencode/Claude Code prompts when you launch
- The LLM updates the discipline file directly as it completes work: marking `[x]` on done items, appending evidence, and advancing gates
- Failed attempts auto-escalate to HITL after a configurable ceiling
- **Review 🔍** launches a separate opencode session as an independent reviewer with zero prior context — `producer ≠ grader`

## AC/DoD Discipline Scratchpad

`discipline.py` is a lightweight process-state harness that externalizes LLM task tracking to a markdown file:

```
---
ticket: NQ-123
phase: implement
gate: pending
attempts: 0
---

## AC/DoD
- [ ] Add feature X
- [ ] Write tests for X

## Evidence Ledger
- 2026-06-28T02:05:36Z [agent] Feature X implemented

## Open Issues
_none_

## Gate Log
- [init] spec → implement (pending QAS)
```

The discipline file lives wherever you point the GUI (config-pinned absolute path). The LLM reads and writes it during the session — no shell commands needed, just file read/write tools.

**API functions:**
- `init_discipline(ticket, criteria)` — create a new ticket
- `toggle_ac(substring, done=True)` — check/uncheck an item
- `log_evidence(role, message)` — append to ledger
- `set_gate(phase, gate, note)` — advance workflow state
- `read_compact()` — token-efficient view for LLM context (~150–250 tokens)
- `status()` — machine-readable state for the GUI status bar

### Why This Works

**Externalized state** — LLM attention decays over long conversations. Writing ticket, phase, and gate to a file instead of holding it in context prevents the model from forgetting what phase it's in. This is a genuine improvement for multi-turn tasks.

**Checklist effect** — Explicit `- [ ]` items give the LLM clear stopping criteria. Without them, the model tends to over-deliver or under-deliver. With them, it has a concrete target. This is well-documented in LLM behavior research.

**Attempt ceiling** — The circuit breaker after N failed gates prevents infinite rework loops. Real value when the LLM keeps trying the same broken approach.

### Independent Review Agent

Self-reported progress is weak — the LLM can mark `[x]` without actually satisfying the criterion. The Discipline panel includes a **Review 🔍** button that launches a separate opencode session as an independent reviewer:

- The reviewer has **zero prior context** — no knowledge of the implementation session
- It reads the discipline file, reads the source code, and verifies each criterion independently
- It updates the discipline file with findings: marking each criterion pass/fail, adding evidence, and setting the gate
- Because it's a completely separate session, it doesn't share any attention decay or confirmation bias with the implementer

`producer ≠ grader` — the same architecture that makes the discipline harness useful is what limits it. The gate model delivers real quality improvements only when reviewer and implementer are genuinely independent.

## Dependencies

Zero required. Core functionality uses only Python 3.10+ standard library.

Optional extras:
- **tree-sitter** — enhanced multi-language parsing
- **flask** — MCP server over HTTP

## License

MIT
