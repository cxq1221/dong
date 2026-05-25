# Dong CLI

Dong is a minimal CLI coding agent whose first UI improvement phase preserves existing command behavior while improving terminal input and rendering.

## Language

**Behavior-Compatible UI Phase**:
A UI improvement phase that keeps existing command names, arguments, REPL commands, tool execution behavior, and agent loop semantics unchanged.
_Avoid_: rewrite, full-screen mode

**Inline REPL**:
The terminal interaction mode where the conversation scrolls in the main terminal and prompts are read one turn at a time.
_Avoid_: fullscreen TUI, dashboard

**Terminal UI Module**:
The module that owns terminal input, confirmation prompts, startup notices, assistant rendering, tool-result rendering, and user-visible errors.
_Avoid_: agent loop, tool registry

**First-Phase Prompt Input**:
The initial prompt_toolkit input experience: prompt history, optional history search, multiline entry, built-in command and skill completion, paste-safe input, and compatible interrupt behavior.
_Avoid_: command palette, fuzzy finder, Vim mode

**First-Phase Rich Rendering**:
The initial Rich output experience: structured startup notices, Markdown assistant messages, concise tool-result lines, clear errors, warning styling, and dangerous-command confirmation panels without live dashboards.
_Avoid_: live token HUD, fullscreen layout, interactive folding, streaming Markdown

**Behavior Compatibility Tests**:
Tests that prove existing CLI commands, tool execution semantics, and entry modes still work while asserting only stable UI text or return behavior instead of exact ANSI snapshots.
_Avoid_: brittle ANSI snapshot

**First-Phase UI Dependencies**:
The first UI phase may add Rich and prompt_toolkit while keeping argparse and deferring Typer or fullscreen frameworks.
_Avoid_: Typer migration, Textual dependency

## Relationships

- A **Behavior-Compatible UI Phase** preserves the **Inline REPL**.
- The **Inline REPL** can improve input and rendering without changing tool execution semantics.
- A **Terminal UI Module** adapts Rich and prompt_toolkit behind a small interface.
- The agent loop calls the **Terminal UI Module** but still owns turn ordering and tool execution.
- **First-Phase Prompt Input** belongs inside the **Terminal UI Module**.
- **First-Phase Rich Rendering** belongs inside the **Terminal UI Module**.
- **Behavior Compatibility Tests** verify a **Behavior-Compatible UI Phase**.
- **First-Phase UI Dependencies** support the **Terminal UI Module**.

## Example dialogue

> **Dev:** "Can we use Rich and prompt_toolkit in the first UI pass?"
> **Domain expert:** "Yes, as long as it remains a **Behavior-Compatible UI Phase** and preserves the **Inline REPL**."

> **Dev:** "Should the new UI module execute tools?"
> **Domain expert:** "No — the **Terminal UI Module** renders tool results, but the agent loop still owns execution."

> **Dev:** "Should phase one add a command palette or Vim mode?"
> **Domain expert:** "No — phase one uses **First-Phase Prompt Input** and keeps richer navigation for later."

> **Dev:** "Should phase one show a live token dashboard?"
> **Domain expert:** "No — phase one uses **First-Phase Rich Rendering** because LLM calls are non-streaming today."

> **Dev:** "Should tests snapshot every ANSI escape emitted by Rich?"
> **Domain expert:** "No — **Behavior Compatibility Tests** assert stable behavior and key text, not brittle ANSI output."

> **Dev:** "Should phase one migrate argparse to Typer?"
> **Domain expert:** "No — **First-Phase UI Dependencies** are limited to Rich and prompt_toolkit."

## Flagged ambiguities

- "CLI UI" could mean a fullscreen terminal app or the existing scrolling REPL; resolved for phase one as **Inline REPL**.
