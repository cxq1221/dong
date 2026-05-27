---
name: product-release-announcement
description: Use when the user wants to turn recent git commits, changelog entries, or engineering work into a polished Chinese product release announcement for WeChat, Feishu, or internal broadcast. Especially useful for requests like "根据提交记录总结最近两周工作内容", "整理为产品特性介绍", "做成产品发布公告", or "微信群发布文案".
---

# Product Release Announcement

Convert recent engineering work into a concise, copy-ready Chinese product announcement.

## Workflow

1. Determine the source range.
   - Use the user's requested range if provided.
   - If no range is provided, default to the most recent two weeks.
   - Prefer local git history as the source of truth.

2. Gather evidence.
   - Start with `git status --short` to notice unrelated local changes.
   - Use `git log --since=... --until=... --no-merges --date=short --pretty=...`.
   - Inspect representative commits with `git show --name-only` or `git show --numstat` when commit messages are too vague.
   - Do not rely only on commit titles when summarizing user-visible product behavior.

3. Group changes into product themes.
   - Prefer 4-7 themes.
   - Merge small or closely related changes.
   - Filter out low-signal internal-only changes unless they explain user-visible reliability, performance, configuration clarity, or delivery readiness.
   - Translate technical details into outcomes: "大结果文件化，避免上下文过长导致失败" is better than naming implementation classes.

4. Write the announcement.
   - Use Chinese.
   - Keep it copy-ready for the requested channel.
   - For WeChat groups, keep paragraphs short and direct.
   - Use a moderate amount of emoji by default to make the announcement easier to scan.
   - Use product feature language, not commit-report language.
   - Do not include commit hashes, file paths, test details, or developer names unless the user asks.
   - Avoid overstating availability. If the evidence only shows implementation, say "支持" or "优化"; do not promise rollout scope unless confirmed.

5. Apply emoji styling.
   - Put one relevant emoji at the start of the opening line, each major theme headline, and the closing summary.
   - For product weekly reports, prefer emoji on section titles rather than every body sentence.
   - If the user explicitly asks for more emoji, add one relevant emoji at the start of each paragraph line.
   - Keep emoji practical and sparse: one per line is enough, and never more than one emoji per line unless the user asks.
   - Avoid decorative overload.

## Recommended Structure

```text
【产品名 产品更新公告】

🚀 近期 [产品名] 完成了一轮能力升级，重点提升 [核心价值 1]、[核心价值 2] 和 [核心价值 3]。

📊 一、[特性主题]
[一句到两句说明用户能做什么、解决什么问题。]

📈 二、[特性主题]
[一句到两句说明用户价值。]

🛠️ 三、[特性主题]
[一句到两句说明稳定性、效率或体验提升。]

✅ 本次更新后，[产品名] 在 [核心业务场景] 下会更 [稳定/高效/易用]。
```

## Filtering Guidance

Keep:
- Data/query capabilities that users can directly use.
- UI changes that change how users complete a workflow.
- Reliability fixes that prevent visible failures or confusing stops.
- Personalization, sharing, export, or collaboration features.
- Configuration/deployment work only when it enables product rollout or safer operations.

Usually drop:
- Pure refactors with no visible product result.
- Test-only, CI-only, cache-only, or build-only commits.
- Internal file moves, naming cleanup, or script tweaks.
- Very small bug fixes unless they affected a visible user workflow.

## Output Modes

If the user asks for a draft, provide only the copy-ready announcement.

If the user asks for rationale or review, provide the announcement first, then a short "取舍说明" that lists which themes were kept or dropped.

If the user asks for multiple tones, produce variants such as:
- 正式公告版
- 微信群轻量版
- 产品周报版

## Quality Checklist

Before finalizing:
- The announcement is grounded in the requested commit range.
- Important user-facing work is not omitted.
- Low-importance implementation details are removed.
- The wording sounds like a product update, not a git log.
- The final text can be copied directly into the target channel.
