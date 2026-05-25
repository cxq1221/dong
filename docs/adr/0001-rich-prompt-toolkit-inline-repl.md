# Use Rich and prompt_toolkit for the first inline REPL UI phase

Dong will use Rich for first-phase terminal rendering and prompt_toolkit for first-phase prompt input while preserving the existing inline REPL, commands, entry modes, tool execution semantics, and agent loop ordering. This keeps the UI phase behavior-compatible and avoids a Textual/fullscreen rewrite before the current CLI interaction model is stabilized.

## Considered Options

- Keep `input()` and `print()` only — rejected because multiline input, history, completion, structured errors, and Markdown rendering would remain shallow or duplicated.
- Use Textual immediately — rejected because a fullscreen event-driven app would change too much of the interaction model for phase one.
- Build a Rust or Go UI sidecar — rejected because it is unnecessary for the current non-streaming Python CLI and would add integration cost before the UI interface is stable.

## Consequences

Textual can still be introduced later as a second adapter for a fullscreen `dong tui` mode, but the first phase keeps Rich and prompt_toolkit behind the Terminal UI Module interface.
