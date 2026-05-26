# PRD: dong 全屏交互式 CLI UI 体验

Label: needs-triage

## Problem Statement

dong 当前的交互式 CLI 输出会把 assistant、thinking、工具调用、工具结果和用户输入混在同一个滚动流里。用户在 AI 工作时很难判断它是否仍在执行、卡在模型请求、卡在工具调用，还是只是 UI 没有刷新。长 Markdown、工具详情和计划内容也容易被压缩、截断或被边框占用空间，窄终端下还会出现文字显示不全的问题。

用户希望 dong 的默认交互体验接近 Codex / Claude Code：AI 工作时界面仍然稳定、可读、可输入；底部输入框始终存在；模型输出、thinking、工具进度和排队输入都有明确状态；Rich Markdown 保持完整渲染但整体视觉更接近 light 风格的 VS Code，而不是高反差、重边框的终端面板。

## Solution

把裸 `dong` 的默认交互模式升级为基于 `prompt_toolkit.Application` 的全屏 TUI。界面由可滚动 transcript、单行/多行状态区和固定底部 composer 组成。assistant、thinking、工具开始、工具结果、计划更新、错误和系统消息都进入 transcript；composer 在 AI 执行期间仍可输入，提交内容先进入队列，等当前轮完成后按顺序执行。

Rich Markdown 继续作为主要内容渲染能力，但只离屏渲染为 ANSI 文本，再交给 TUI transcript 展示。这样既保留 Markdown、代码块、颜色和列表排版，又避免 Rich 直接写真实终端导致覆盖输入框。工具进度和模型思考状态实时更新到状态栏，长文本按当前终端宽度重新排版，状态栏宽度不足时主动截断并显示省略号。

第一期只做排队输入，不做中断追加；只保留 ANSI 颜色，不做鼠标选择、语义级 Markdown block 操作或折叠编辑；单次命令模式和非 TTY 管道模式继续使用现有行式 UI。

## User Stories

1. As a dong CLI user, I want the input box to always stay visible, so that I can type the next instruction while the AI is still working.
2. As a dong CLI user, I want input submitted during an active run to be queued, so that I can prepare follow-up work without interrupting the current turn.
3. As a dong CLI user, I want queued input to execute in order, so that conversation behavior is predictable.
4. As a dong CLI user, I want the UI to show when the AI is thinking, so that I know the program is still active.
5. As a dong CLI user, I want thinking content to be expanded by default, so that I can inspect the model's current reasoning summary stream when the provider exposes it.
6. As a dong CLI user, I want assistant output to stream into one updating transcript item, so that the screen does not fill with token-level fragments.
7. As a dong CLI user, I want thinking output to stream into one updating transcript item, so that reasoning remains readable.
8. As a dong CLI user, I want tool start events to appear immediately, so that I can see which tool is currently running.
9. As a dong CLI user, I want long-running tool status to show elapsed time and timeout budget, so that I can distinguish waiting from a frozen UI.
10. As a dong CLI user, I want bash timeout progress to be visible before final timeout, so that a 30 second wait is understandable.
11. As a dong CLI user, I want fetch/network waits to have a clear status, so that DNS or slow network calls do not look like a generic spinner.
12. As a dong CLI user, I want the final tool result to include full details when available, so that plans and diagnostics are not silently hidden.
13. As a dong CLI user, I want update-plan output to preserve the full plan detail, so that I can audit what changed.
14. As a dong CLI user, I want Markdown lists, headings and code blocks to render cleanly, so that long explanations remain readable.
15. As a dong CLI user, I want the UI to avoid surrounding content with heavy boxes, so that terminal space is used for content.
16. As a dong CLI user, I want a light VS Code-like color style, so that the transcript has lower contrast and less visual noise.
17. As a dong CLI user, I want narrow terminal widths to wrap content correctly, so that text is not cut off.
18. As a dong CLI user, I want status text to truncate gracefully when width is insufficient, so that layout does not break.
19. As a dong CLI user, I want slash skill suggestions when I type `/`, so that available skills are discoverable.
20. As a dong CLI user, I want slash suggestions to filter as I continue typing, so that I can quickly find a skill by prefix.
21. As a dong CLI user, I want dangerous command confirmation to happen inside the TUI, so that the current input surface remains coherent.
22. As a dong CLI user, I want normal composer input disabled only during confirmation, so that command confirmation does not conflict with queued prompts.
23. As a dong CLI user, I want `Ctrl+C` to clear typed text, request cancellation, or exit depending on context, so that interrupt behavior matches terminal expectations.
24. As a dong CLI user, I want `Ctrl+D` to exit the TUI, so that common terminal exit behavior works.
25. As a dong CLI user, I want PageUp/PageDown scrolling, so that I can inspect earlier output.
26. As a dong CLI user, I want new output indication when I have scrolled away from the bottom, so that I know background work produced more content.
27. As a dong CLI user, I want `End` or new submit to return to follow-bottom mode, so that I can quickly resume live output tracking.
28. As a dong CLI user, I want one-shot `dong "prompt"` behavior unchanged, so that scripts and quick commands do not enter full-screen mode.
29. As a dong CLI user, I want non-TTY input/output behavior unchanged, so that piping and automation remain compatible.
30. As a dong developer, I want the agent loop to stay mostly unchanged, so that UI work does not destabilize tool calling and provider adapters.
31. As a dong developer, I want a UI adapter with the same surface as the existing terminal UI, so that the agent loop can target either renderer.
32. As a dong developer, I want Rich rendering isolated behind an offscreen renderer, so that it can be tested without running a real terminal.
33. As a dong developer, I want queueing behavior tested independently, so that background worker ordering is reliable.
34. As a dong developer, I want PTY-level tests for interactive behavior, so that regressions in the composer/transcript split are caught.
35. As a dong developer, I want logs to remain useful for diagnosing stuck sessions, so that I can distinguish active backend work from UI repaint issues.

## Implementation Decisions

- Build a dedicated fullscreen TUI application module that owns layout, transcript state, status state, input queue, key bindings, confirmation state, and worker lifecycle.
- Build a TUI UI adapter that implements the same high-level methods as the existing terminal UI, so the agent loop can remain renderer-agnostic.
- Keep the existing line-mode terminal UI for one-shot and non-TTY execution paths.
- Route bare interactive TTY startup into the fullscreen TUI by default.
- Represent transcript entries as stable typed items: assistant, thinking, tool start, tool result, plan, warning, error and system.
- Stream assistant and thinking content by updating existing transcript entries instead of appending per token.
- Render Rich Markdown offscreen using the current TUI width and a light terminal theme, then display the resulting ANSI text inside prompt_toolkit.
- Avoid Rich Live/Status/direct console writes in fullscreen mode because they compete with the composer for terminal ownership.
- Keep a fixed bottom composer and allow text entry while the worker is busy.
- Use a FIFO queue for user submissions. Phase 1 executes queued prompts after the active turn finishes.
- Do not merge queued input into an active LLM/tool turn in phase 1.
- Make dangerous command confirmation a synchronous TUI confirmation state owned by the active tool turn.
- Keep tool cancellation cooperative in phase 1. The UI may request cancellation and show the request, but should not destructively kill threads or subprocesses beyond existing tool behavior.
- Status bar should show current activity, elapsed time where available, queued count, cancellation request and new-output indicator.
- Width-sensitive rendering should use actual terminal columns with a lower bound for extremely narrow terminals.
- Status text should be display-width aware and truncate with an ellipsis rather than overflowing.
- Slash completion should use a dynamic provider so skill list changes are reflected without restarting the app.
- The implementation should preserve the current tool registry, skill manager, LLM provider adapters, context trimming and logging contracts.

## Testing Decisions

- Tests should focus on external behavior visible to the user: queued input order, transcript updates, completion filtering, confirmation behavior, width wrapping, status truncation and one-shot compatibility.
- Unit tests should cover offscreen Markdown rendering, transcript item updates, queue processing, dynamic slash completion and dangerous command confirmation.
- Unit tests should assert that full tool result details are preserved for plan-style outputs rather than only checking summaries.
- Narrow-width tests should verify wrapping and truncation behavior without relying on exact ANSI snapshot equality.
- PTY tests should verify that interactive mode starts, keeps a composer available, accepts input while work is active, queues follow-up input and avoids overwriting the composer.
- Existing terminal UI tests should remain in place to protect one-shot and non-TTY behavior.
- Existing CLI REPL command tests should remain in place to protect `exit`, `clear`, directory switching and skill commands.
- Full regression should run lint plus the Python test suite before claiming completion.
- Real LLM/API end-to-end verification should be used when validating the complete user experience, especially for streaming assistant/thinking and tool status behavior.

## Out of Scope

- Introducing Textual or any new heavyweight TUI framework.
- Rewriting the agent loop, LLM provider layer, tool registry or skill system.
- Interrupt-and-append semantics where new user input modifies an active model turn.
- Semantic Markdown block operations such as folding, selecting, copying blocks or block-level actions.
- Mouse selection support beyond basic terminal scrolling behavior.
- Command palette, tabs, multi-pane file viewers or IDE-like navigation.
- Destructive cancellation of running subprocesses or worker threads beyond current safe behavior.
- Changing one-shot command behavior.
- Changing non-TTY pipeline behavior.
- Redesigning provider-specific reasoning formats.

## Further Notes

- This PRD captures the current direction after the earlier implementation plan and follow-up UI feedback.
- The existing phase-1 implementation plan remains the execution checklist; this PRD is the product requirement artifact for triage and future iteration.
- The observed “running dong looks stuck” diagnosis should be treated as a UX requirement: the UI must make idle/waiting/working/repaint states distinguishable, and logs should remain sufficient to prove whether backend work is active.
- Future phases may add richer cancellation, semantic transcript operations, persisted transcript sessions, better copy support and configurable themes.
