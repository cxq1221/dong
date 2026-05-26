# Fullscreen TUI Phase 1 Implementation Plan

## Goal

Replace the default interactive `dong` experience with a Codex-like fullscreen terminal UI that keeps a bottom composer visible while the agent thinks, calls tools, streams output, and accepts queued follow-up input.

## Non-goals

- Do not introduce Textual.
- Do not rewrite the agent loop or tool registry.
- Do not mix new user input into an active LLM/tool turn.
- Do not use Rich Live/Status/direct terminal writes inside the fullscreen TUI.
- Do not implement mouse selection, semantic Markdown block editing, folding, or a command palette in phase 1.
- Do not remove `dong "prompt"` one-shot mode.

## Confirmed Decisions

- Bare `dong` defaults to fullscreen TUI.
- `dong "prompt"` remains a one-shot command mode and exits after completion.
- Non-TTY/stdin pipeline paths do not enter fullscreen TUI.
- The TUI is built with `prompt_toolkit.Application`.
- Rich Markdown is preserved through offscreen rendering, then displayed in the TUI transcript with ANSI color.
- The bottom composer is always visible in interactive TUI mode.
- Input submitted while the agent is working is queued for the next turn.
- Phase 1 does not support interrupt-and-append semantics.
- Assistant text streams into a single updating transcript item.
- Thinking/reasoning streams into a single updating transcript item and is expanded by default.
- Slash skill completion and filtering remain available in the composer.
- Dangerous command confirmation is a synchronous confirmation prompt in the TUI, temporarily disabling normal composer input.
- Tool progress is shown in the status bar, with tool start/end records in transcript.
- Scrolling defaults to follow-bottom; user scroll disables auto-follow; `End` or submitting input restores auto-follow.
- `Ctrl+C` behavior:
  - Composer has text: clear composer.
  - Composer empty and agent working: request cancellation.
  - Composer empty and idle: first press warns, second press exits.
  - `Ctrl+D`: exit TUI.

## Architecture

### Existing Paths

Keep the current `TerminalUI` path for one-shot and non-TTY execution. It can continue using Rich direct output because no persistent composer is present.

### New TUI Path

Add `dong/tui.py` with two primary surfaces:

- `TuiApp`: owns `prompt_toolkit.Application`, layout, key bindings, transcript state, queue state, status bar, and worker lifecycle.
- `TuiUI`: adapter implementing the current UI method surface used by `run_loop()`, but enqueueing transcript/status events instead of printing directly.

`run_loop()` should stay mostly unchanged. It receives either `TerminalUI` or `TuiUI`.

### Event Flow

```text
agent loop / tool execution
  -> TuiUI method call
  -> TUI event queue
  -> TuiApp main UI thread
  -> transcript/status/composer render
```

All background worker output must cross the event queue. Background threads must not call `Application.invalidate()` while mutating shared UI state without using the TUI app's synchronization helpers.

## UI Layout

```text
transcript viewport
  assistant / thinking / tools / warnings / errors
status bar
  idle | thinking 12s | tool bash 8s/30s | queued 2 | new output
composer
  dong > ...
```

The transcript area is scrollable. The composer is fixed at the bottom and never shares its region with transcript output.

## Rendering

### Rich Offscreen Rendering

Use Rich only to render offscreen:

```python
Console(
    file=StringIO(),
    force_terminal=True,
    color_system="truecolor",
    width=current_transcript_width,
    theme=DONG_LIGHT_THEME,
)
```

Render Markdown/Text/Table into ANSI text and append/update transcript items with that output. Do not call `Console.print()` against the real terminal in TUI mode.

### Transcript Items

Use stable item types:

- `assistant`
- `thinking`
- `tool_start`
- `tool_result`
- `plan`
- `warning`
- `error`
- `system`

Streaming assistant/thinking items should be updated in place and re-rendered on a small throttle, for example every 50-100ms.

## Input And Queueing

The composer accepts input while the worker is active. On Enter:

- If idle: enqueue and start execution immediately.
- If working: enqueue and update status `queued N`.
- If confirmation prompt is active: handle the confirmation instead of queueing normal input.

The worker processes queued inputs sequentially. Each queued input runs through the existing REPL command handling first, then normal `run_loop()` if needed.

## Command Handling

Reuse existing command semantics:

- `exit`, `quit`, `/bye`
- `clear`
- `dir=<path>`
- `/skill`, `/skills`
- `/skill <name>`
- `/unskill <name>`
- `/<skill> <prompt>`
- bare skill-name invocation

Commands submitted while working are queued like prompts, except dangerous-command confirmation, which belongs to the currently running tool and is handled synchronously.

## Scrolling

- Start in follow-bottom mode.
- PageUp, mouse wheel, or explicit scroll up disables follow-bottom.
- End or submitting a new input enables follow-bottom.
- If new output arrives while follow-bottom is disabled, status bar shows `new output`.

## Cancellation

Phase 1 cancellation should be cooperative:

- `Ctrl+C` while working requests cancellation.
- LLM/tool cancellation support should reuse existing `KeyboardInterrupt` handling where possible.
- If a blocking operation cannot be interrupted immediately, status should show cancellation requested while waiting for the operation to unwind.

Do not kill subprocesses or threads destructively in phase 1 unless the existing tool path already supports it.

## File-Level Changes

- `dong/tui.py`: new fullscreen TUI app and adapter.
- `dong/cli.py`: route bare interactive `dong` into `TuiApp`; keep one-shot/non-TTY paths on `TerminalUI`.
- `dong/ui.py`: keep line-mode UI; share theme/rendering helpers where useful.
- `dong/llm.py`: no provider rewrite expected; reasoning streaming already exists.
- `tests/test_tui.py`: new unit tests for TUI state, rendering, queueing, and status.
- `tests/test_cli_tty_e2e.py`: add a lightweight PTY test for persistent composer + queued input.

## Implementation Steps

1. Add transcript data model, status model, and offscreen Rich renderer.
2. Add `TuiUI` adapter methods for assistant, thinking, tools, warnings, errors, working status, streaming, and dangerous command confirmation.
3. Add `TuiApp` layout with transcript viewport, status bar, and composer.
4. Wire composer key bindings, slash completion, history, multiline input, scroll bindings, and Ctrl+C/Ctrl+D behavior.
5. Wire worker queue so inputs are processed sequentially through existing REPL command handling and `run_loop()`.
6. Route `dong` interactive TTY mode to `TuiApp`; keep `dong "prompt"` and non-TTY on `TerminalUI`.
7. Add unit tests for rendering, queueing, status transitions, slash filtering, and confirmation behavior.
8. Add PTY E2E for working-while-input queue behavior.
9. Run `uv run ruff check dong tests` and `uv run pytest -q`.

## Risks

- `prompt_toolkit.Application` redraw performance can degrade if transcript lines are rebuilt too often. Mitigation: throttle streaming updates and cap retained rendered lines.
- ANSI rendering from Rich may not map perfectly into prompt_toolkit formatted text. Mitigation: start with ANSI text display and test key visual content, not full snapshots.
- Cooperative cancellation may not stop all blocking tool calls immediately. Mitigation: status makes cancellation state visible and keeps existing timeout protections.
- Existing `TerminalUI` tests may become brittle if shared helpers move. Mitigation: preserve public behavior for non-TTY/one-shot paths.

## Rollback Points

- Keep one-shot mode untouched until TUI tests pass.
- Add `TuiApp` behind a narrow CLI routing point before deleting the current interactive queue experiment.
- If fullscreen TUI is unstable, fallback can route interactive TTY back to `_run_repl_sync()` while retaining the new module for iteration.

## Verification

Required before claiming phase 1 complete:

- `uv run ruff check dong tests`
- `uv run pytest -q`
- PTY E2E proves:
  - composer is visible while work is active
  - second input is accepted while first task runs
  - second input is queued
  - second input executes after first task finishes
  - output does not overwrite composer

