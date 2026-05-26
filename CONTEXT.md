# Dong CLI

Dong is a minimal CLI coding agent whose interactive experience is moving from an inline scrolling REPL to a Codex-like fullscreen terminal UI.

## Language

**Codex-like Fullscreen TUI Phase**:
The replacement interactive UI phase where bare `dong` owns the terminal layout, keeps a fixed composer visible, and renders all agent output through a transcript.
_Avoid_: inline REPL phase, scrolling prompt patch, Rich direct terminal writes

**Inline REPL**:
The former terminal interaction mode where conversation output and the next prompt shared the same scrolling terminal region.
_Avoid_: default interactive mode, persistent composer

**Terminal UI Module**:
The module that owns terminal input, confirmation prompts, startup notices, assistant rendering, tool-result rendering, and user-visible errors.
_Avoid_: agent loop, tool registry

**Persistent Composer**:
The fixed bottom input area that remains available while the agent is thinking or executing tools.
_Avoid_: next prompt, queued line prompt

**Transcript**:
The scrollable output area that displays assistant, thinking, tool, warning, and error events.
_Avoid_: stdout log, raw terminal scrollback

**TUI UI Adapter**:
The adapter that implements the existing agent-loop UI methods by enqueueing transcript/status events instead of printing directly to the terminal.
_Avoid_: direct Console.print, agent-loop rewrite

**Offscreen Rich Rendering**:
Rendering Rich Markdown and styled content into ANSI/text artifacts before handing them to the fullscreen TUI layout.
_Avoid_: Rich Live, direct Rich status, semantic Markdown widget

**First-Phase Prompt Input**:
The prompt_toolkit input experience inside the **Persistent Composer**: prompt history, optional history search, multiline entry, built-in command and skill completion, paste-safe input, and compatible interrupt behavior.
_Avoid_: command palette, fuzzy finder, Vim mode

**First-Phase Rich Rendering**:
The initial Rich output experience rendered through **Offscreen Rich Rendering** into the **Transcript**.
_Avoid_: direct Rich terminal writes, interactive folding, mouse selection

**Behavior Compatibility Tests**:
Tests that prove existing CLI commands, tool execution semantics, and entry modes still work while asserting only stable UI text or return behavior instead of exact ANSI snapshots.
_Avoid_: brittle ANSI snapshot

**Operational File Logging**:
The runtime diagnostic stream written by dong into `logs/dong.log`, with `dong logs` support for level, event, logger, and text filtering over stable event names and JSON field payloads.
_Avoid_: ad hoc print debugging, prompt dumps

**First-Phase UI Dependencies**:
The first fullscreen TUI phase uses Rich and prompt_toolkit.Application while keeping argparse and avoiding Textual.
_Avoid_: Typer migration, Textual dependency

## Relationships

- The **Codex-like Fullscreen TUI Phase** replaces the **Inline REPL** as the default bare `dong` interactive mode.
- The **Codex-like Fullscreen TUI Phase** uses a **Persistent Composer** and a **Transcript**.
- A **Terminal UI Module** adapts Rich and prompt_toolkit behind a small interface.
- The agent loop calls the **Terminal UI Module** but still owns turn ordering and tool execution.
- A **TUI UI Adapter** preserves the agent-loop UI method surface while routing output into the **Transcript**.
- **Offscreen Rich Rendering** lets **First-Phase Rich Rendering** coexist with a **Persistent Composer**.
- **Operational File Logging** records the agent loop, LLM adapter, skill loading, and tool execution paths without changing user-visible terminal output, then exposes filtered local inspection through `dong logs`.
- **First-Phase Prompt Input** belongs inside the **Terminal UI Module**.
- **First-Phase Rich Rendering** belongs inside the **Terminal UI Module**.
- **Behavior Compatibility Tests** verify a **Behavior-Compatible UI Phase**.
- **First-Phase UI Dependencies** support the **Terminal UI Module**.

## Example dialogue

> **Dev:** "Should bare `dong` keep using the old scrolling prompt?"
> **Domain expert:** "No — the **Codex-like Fullscreen TUI Phase** replaces the **Inline REPL** for the default interactive mode."

> **Dev:** "Should the new UI module execute tools?"
> **Domain expert:** "No — the **Terminal UI Module** renders tool results, but the agent loop still owns execution."

> **Dev:** "Can Rich Markdown write directly to the terminal while the composer is visible?"
> **Domain expert:** "No — use **Offscreen Rich Rendering** so the **Transcript** and **Persistent Composer** stay under one TUI layout."

> **Dev:** "Should input typed while the agent works modify the current LLM/tool turn?"
> **Domain expert:** "No — the **Persistent Composer** queues the next message; the active turn remains unchanged."

> **Dev:** "Should tests snapshot every ANSI escape emitted by Rich?"
> **Domain expert:** "No — **Behavior Compatibility Tests** assert stable behavior and key text, not brittle ANSI output."

> **Dev:** "Should phase one migrate argparse to Typer?"
> **Domain expert:** "No — **First-Phase UI Dependencies** are limited to Rich and prompt_toolkit."

## Flagged ambiguities

- "CLI UI" previously meant **Inline REPL** improvements; resolved now as the **Codex-like Fullscreen TUI Phase** for bare `dong`.
