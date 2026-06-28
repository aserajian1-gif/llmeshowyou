"""discipline.py — process-state scratchpad for disciplined LLM workflows.

Companion to llmeshowyou's code maps: maps compress *code* tokens, this compresses
*workflow* tokens. The LLM externalizes gate state to a frontmatter-first markdown
file instead of holding it in context, so independent review subagents read ~200
tokens of ledger rather than re-deriving state from the whole conversation.
"""
# #[File: discipline.py]
from __future__ import annotations
import os
from datetime import datetime, timezone

# #[Constants]
DISCIPLINE_DEFAULT = ".discipline.md"
PHASES = ("spec", "implement", "qas", "review", "done")
GATES = ("pending", "pass", "fail")
ATTEMPT_CEILING = 3  # circuit breaker: beyond this, escalate to TDM (HITL)


# #[Internal Helpers]
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split(text: str):
    """Return (frontmatter_dict, body_str) from a markdown file with --- fences."""
    fm, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip("\n")
            body = text[end + 4:].lstrip("\n")
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    return fm, body


def _join(fm: dict, body: str) -> str:
    order = ["ticket", "phase", "gate", "attempts", "updated"]
    keys = order + [k for k in fm if k not in order]
    head = "\n".join(f"{k}: {fm[k]}" for k in keys if k in fm)
    return f"---\n{head}\n---\n\n{body.strip()}\n"


def _load(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"No discipline file at {path}. Run init_discipline first.")
    with open(path, "r", encoding="utf-8") as f:
        return _split(f.read())


def _save(path: str, fm: dict, body: str) -> None:
    fm["updated"] = _now()
    with open(path, "w", encoding="utf-8") as f:
        f.write(_join(fm, body))


# #[Public API]
# ##[Primary Entry Point]
def init_discipline(ticket: str, ac: list[str], path: str = DISCIPLINE_DEFAULT,
                    overwrite: bool = False) -> str:
    """Create the discipline scratchpad for a ticket with its AC/DoD checklist.

    This is the Stop-the-Line anchor: if this file/section is absent, agents must
    refuse to implement. Returns the file path.
    """
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(f"{path} exists; pass overwrite=True to reset.")
    fm = {"ticket": ticket, "phase": "spec", "gate": "pending", "attempts": "0"}
    checklist = "\n".join(f"- [ ] {c}" for c in ac) or "- [ ] (define criteria)"
    body = (
        f"## AC/DoD\n{checklist}\n\n"
        f"## Evidence Ledger\n- {_now()} [bsa] ticket {ticket} initialized\n\n"
        f"## Open Issues\n_none_\n\n"
        f"## Gate Log\n- [init] spec → implement (pending QAS)\n"
    )
    _save(path, fm, body)
    return path


# ##[Ledger API]
def log_evidence(role: str, message: str, path: str = DISCIPLINE_DEFAULT) -> None:
    """Append one timestamped line to the Evidence Ledger."""
    fm, body = _load(path)
    line = f"- {_now()} [{role}] {message}"
    if "## Evidence Ledger" in body:
        head, _, tail = body.partition("## Evidence Ledger\n")
        # insert right after the section header
        rest = tail.split("\n\n", 1)
        block = rest[0] + "\n" + line
        body = head + "## Evidence Ledger\n" + block + ("\n\n" + rest[1] if len(rest) > 1 else "\n")
    else:
        body += f"\n## Evidence Ledger\n{line}\n"
    _save(path, fm, body)


def set_gate(phase: str | None = None, gate: str | None = None,
             note: str | None = None, bump_attempt: bool = False,
             path: str = DISCIPLINE_DEFAULT) -> dict:
    """Update workflow state. Validates against PHASES/GATES.

    Circuit breaker: when bumping attempts past ATTEMPT_CEILING, the returned dict
    carries 'ceiling_tripped'=True so callers (QAS/TDM/GUI) can escalate to HITL
    instead of looping. A note is auto-appended to the body on trip.
    """
    fm, body = _load(path)
    if phase:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}")
        fm["phase"] = phase
    if gate:
        if gate not in GATES:
            raise ValueError(f"gate must be one of {GATES}")
        fm["gate"] = gate
    if bump_attempt:
        fm["attempts"] = str(int(fm.get("attempts", "0")) + 1)
    if note:
        body = body.rstrip() + f"\n- [{_now()}] {note}\n"
    tripped = int(fm.get("attempts", "0")) > ATTEMPT_CEILING
    if tripped:
        body = body.rstrip() + (
            f"\n- [{_now()}] CIRCUIT BREAKER: attempts={fm['attempts']} > "
            f"{ATTEMPT_CEILING}; escalate to @tdm (HITL decision required)\n"
        )
    _save(path, fm, body)
    fm["ceiling_tripped"] = tripped
    return fm


def toggle_ac(criterion_substr: str, done: bool = True,
              path: str = DISCIPLINE_DEFAULT) -> bool:
    """Check/uncheck an AC line by matching a substring. Returns True if matched."""
    fm, body = _load(path)
    mark = "[x]" if done else "[ ]"
    out, hit = [], False
    for ln in body.splitlines():
        if (ln.startswith("- [ ]") or ln.startswith("- [x]")) and criterion_substr in ln:
            out.append(f"- {mark}" + ln[5:]); hit = True
        else:
            out.append(ln)
    if hit:
        _save(path, fm, "\n".join(out))
    return hit


# ##[Read Views]
def read_compact(path: str = DISCIPLINE_DEFAULT, full: bool = False) -> str:
    """Token-efficient view for the LLM. Default returns frontmatter + AC + open
    issues only (~150–250 tokens). full=True returns the entire ledger."""
    fm, body = _load(path)
    head = " | ".join(f"{k}={fm.get(k,'?')}" for k in ("ticket", "phase", "gate", "attempts"))
    if int(fm.get("attempts", "0")) > ATTEMPT_CEILING:
        head += " | CEILING_TRIPPED→@tdm"
    if full:
        return f"[{head}]\n\n{body}"
    sections = {}
    cur = None
    for ln in body.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip(); sections[cur] = []
        elif cur:
            sections[cur].append(ln)
    ac = "\n".join(sections.get("AC/DoD", []))
    issues = "\n".join(sections.get("Open Issues", []))
    return f"[{head}]\n\n## AC/DoD\n{ac}\n\n## Open Issues\n{issues}"


def status(path: str = DISCIPLINE_DEFAULT) -> dict:
    """Machine-readable state for the GUI status bar."""
    try:
        fm, body = _load(path)
    except FileNotFoundError:
        return {"exists": False}
    total = body.count("- [ ]") + body.count("- [x]")
    done = body.count("- [x]")
    attempts = int(fm.get("attempts", "0"))
    return {"exists": True, "ticket": fm.get("ticket"), "phase": fm.get("phase"),
            "gate": fm.get("gate"), "attempts": attempts,
            "ceiling_tripped": attempts > ATTEMPT_CEILING,
            "ac_done": done, "ac_total": total}
