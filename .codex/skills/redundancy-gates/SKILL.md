---
name: redundancy-gates
description: Applies high-level redundancy prevention gates before adding, refactoring, or preserving code. Use when reviewing or changing code that may duplicate interfaces, configs, tools, schema fields, tests, prompts, compatibility layers, or architecture policy.
---

# Redundancy Gates

Use this skill to prevent redundant code before it enters the repo, and to review cleanup work without drifting into broad redesign. Unless the user explicitly asks for another language, write the final report in Chinese.

## Core Rule

Treat redundancy as a boundary failure, not just "too many lines":
- An Interface has no real consumer.
- An Implementation detail leaks into contracts, tests, or prompts.
- One rule has multiple authorities.
- A compatibility path has no expiry condition.
- A migration removes one layer but leaves adjacent surfaces alive.

Prefer deletion, consolidation, and clearer ownership before adding a new layer.

## Evidence Pass

Before judging or editing, check `git status --short`, use `rg` to find callers/contracts/tests/docs/prompts/registries/frontend references, read nearby ADRs when boundaries are involved, and name the smallest externally visible Interface affected by the task.

Do not call code redundant only because it looks unused locally; prove whether a caller, runtime hook, prompt contract, migration, or external API still depends on it.

## Gates

1. Interface-First: the caller-facing Interface should be smaller than the Implementation. Fail if one internal field forces model, contract, frontend, and tests to change without new user behavior.
2. Consumer-Proof: every new field, route, tool, config key, table, prompt rule, or compatibility branch needs a named consumer. Fail on "maybe useful later", "for completeness", or "future extension".
3. Single-Authority: one rule should have one owning Module. Avoid repeating policy across runtime code, prompts, tests, and docs. In this repo, likely owners are tool registry/toolset assembly, sandbox runner layers, or route -> service -> query boundaries.
4. Compatibility-Expiry: compatibility code must name the old caller, detection method, deletion condition, and focused test. Fail if old and new interfaces coexist indefinitely.
5. Test-Surface: tests should protect stable behavior, not temporary Implementation structure. Fail if tests lock progress internals, preview payload internals, or status bookkeeping callers should not know.
6. Config-Control-Plane: stable defaults belong in code; real variability belongs in config. Fail if duplicate knobs do not change operational control or make precedence unclear.
7. Migration-Closure: when deleting or replacing an Interface, scan routes, contracts, services, queries, tool registry, prompts, frontend, ORM/migrations, tests, E2E scripts, docs, ADRs, and profile/skill text.
8. One-Change-Many-Modules: pause if one small requirement creates more than three Modules, copied fields across layers, parallel old/new tools, prompt rules mirroring runtime code, or a generic framework for one concrete behavior.

## Workflow

1. State the task as a verifiable target.
2. List intended Interface changes.
3. Run the evidence pass.
4. Classify suspect items as `delete`, `consolidate`, `deepen`, `defer`, or `keep`.
5. If editing, make the smallest reversible change and prefer deletion over addition.
6. Verify with focused tests or static checks that match the changed surface.

## Output Shape

Use Chinese section titles and Chinese severity labels:

```
门禁发现
- 阻塞/警告/通过：<发现>（<文件引用>）

建议动作
- 删除/合并/深化/暂缓/保留：<要做什么以及原因>

证据
- <执行的命令或检查的文件>

剩余风险
- <未验证的内容>
```

Keep the report grounded in file references and current repo evidence. Avoid generic advice when a concrete owner, caller, or deletion path can be named.
