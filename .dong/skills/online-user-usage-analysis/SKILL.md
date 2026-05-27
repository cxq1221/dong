---
name: online-user-usage-analysis
description: "Analyze recent online cg-analyzer user conversations from the project database and produce an evidence-backed Markdown usage report."
triggers:
  - 线上用户使用情况
  - 用户意图分析
  - 最近一周使用情况
  - 解决了多少问题
  - representative questions
argument-hint: "[days=7] [output-md-path]"
---

# Online User Usage Analysis

## Purpose

Use this skill to analyze how real users are using cg-analyzer in production conversations, including:

- recent active users, sessions, and user-message volume
- dominant user intents and representative raw questions
- tool and skill usage patterns
- high-confidence solved case estimates
- a Markdown report suitable for sharing or follow-up planning

This skill is read-mostly. It may write only the requested report file.

## Data Source

Use the project database the user identifies as authoritative for cg-analyzer. Do not hardcode credentials in this skill.

When constructing SQLAlchemy URLs for MySQL credentials, prefer `sqlalchemy.engine.URL.create(...)` rather than interpolating a URL string, because passwords may contain reserved URL characters such as `@`.

Primary tables:

- `conversation_sessions`
- `conversation_messages`
- `conversation_tool_calls`

Optional supporting tables may be used only if needed for names or ownership, but the baseline report should work with the three tables above.

## Message Filter

Use this filter for real user messages:

```sql
m.role = 'user'
AND m.message_type = 'user'
AND m.created_at >= NOW() - INTERVAL :days DAY
AND (
  JSON_EXTRACT(m.raw_payload, '$.contextual_type') IS NULL
  OR JSON_UNQUOTE(JSON_EXTRACT(m.raw_payload, '$.contextual_type')) = ''
)
AND m.content NOT LIKE '<skill>%'
```

Rationale:

- `role='user'` and `message_type='user'` select actual user turns.
- `raw_payload.contextual_type` filters hidden contextual messages such as selected skill injections.
- `<skill>%` filters legacy or plain-text injected skill context.
- Group by conversation when analyzing intent, because one long pasted log can otherwise dominate raw message counts.

## Workflow

1. Confirm the analysis window.
   - Default to the last 7 days if the user does not specify.
   - Use database time via `NOW()` and record both `db_now` and `window_start`.

2. Collect baseline volume.
   - Distinct users.
   - Distinct conversations with real user messages.
   - Real user message count.
   - Assistant message count in the same conversation set.

3. Build daily trend.
   - Active users per day.
   - Conversation count per day.
   - User-message count per day.
   - Note weekends or partial current-day windows.

4. Classify conversations by intent.
   - Prefer conversation-level classification over message-level classification.
   - Use lightweight keyword rules first; manually inspect representative conversations for the top categories.
   - Keep the classification as an evidence-backed heuristic, not a model-truth label.

5. Extract representative questions.
   - Pull raw user questions from each major category.
   - Preserve business meaning.
   - Redact credentials, Authorization headers, tokens, AK/SK values, long private logs, and host/password details unless the user explicitly asks for a secure internal appendix.

6. Analyze tool and skill usage.
   - From `conversation_tool_calls`, compute call count, success count, failure count, failure rate, average latency, and max latency by tool.
   - From hidden skill context or skill-related payloads, extract selected skill counts when available.
   - Highlight high-frequency tools with high failure rates because they directly affect user experience.

7. Estimate high-confidence solved cases.
   - This is not true satisfaction scoring unless explicit user feedback exists.
   - Count a conversation as high-confidence solved only when:
     - the final assistant reply contains a clear conclusion, root cause, query result, report/file delivery, SQL/link/evidence, or concrete answer; and
     - the final assistant reply does not end in an unresolved state such as unable to determine, query failed, missing information, no access, or needs more input; and
     - if tools were used, at least one tool succeeded and failures do not dominate the conversation.
   - Also report medium/partial cases and unresolved/unclear cases.
   - Phrase this as a conservative estimate, not an exact solved-count truth.

8. Write the Markdown report.
   - Default path: `exports/online_user_usage_report_<YYYY-MM-DD>.md`.
   - Include the data window, filter口径, summary, tables, representative questions, solved-case estimate, limitations, and optimization recommendations.

9. Verify.
   - Check the report exists and has expected sections.
   - Search the report for leaked secrets: database passwords, Authorization, Bearer, token values, AK/SK concrete values.
   - Do not run backend tests unless code changed.

## Baseline SQL Snippets

### Volume

```sql
SELECT
  NOW() AS db_now,
  NOW() - INTERVAL :days DAY AS window_start,
  COUNT(*) AS user_messages,
  COUNT(DISTINCT m.conversation_id) AS conversations,
  COUNT(DISTINCT s.user_id) AS users
FROM conversation_messages m
JOIN conversation_sessions s ON s.id = m.conversation_id
WHERE
  m.role = 'user'
  AND m.message_type = 'user'
  AND m.created_at >= NOW() - INTERVAL :days DAY
  AND (
    JSON_EXTRACT(m.raw_payload, '$.contextual_type') IS NULL
    OR JSON_UNQUOTE(JSON_EXTRACT(m.raw_payload, '$.contextual_type')) = ''
  )
  AND m.content NOT LIKE '<skill>%';
```

### Daily Trend

```sql
SELECT
  DATE(m.created_at) AS day,
  COUNT(DISTINCT s.user_id) AS active_users,
  COUNT(DISTINCT m.conversation_id) AS conversations,
  COUNT(*) AS user_messages
FROM conversation_messages m
JOIN conversation_sessions s ON s.id = m.conversation_id
WHERE
  m.role = 'user'
  AND m.message_type = 'user'
  AND m.created_at >= NOW() - INTERVAL :days DAY
  AND (
    JSON_EXTRACT(m.raw_payload, '$.contextual_type') IS NULL
    OR JSON_UNQUOTE(JSON_EXTRACT(m.raw_payload, '$.contextual_type')) = ''
  )
  AND m.content NOT LIKE '<skill>%'
GROUP BY DATE(m.created_at)
ORDER BY day;
```

### Tool Reliability

```sql
SELECT
  tool_name,
  COUNT(*) AS calls,
  SUM(status = 'success') AS success_calls,
  SUM(status <> 'success') AS failed_calls,
  ROUND(SUM(status <> 'success') / COUNT(*) * 100, 2) AS failure_rate_pct,
  ROUND(AVG(duration_ms), 1) AS avg_duration_ms,
  MAX(duration_ms) AS max_duration_ms
FROM conversation_tool_calls
WHERE occurred_at >= NOW() - INTERVAL :days DAY
GROUP BY tool_name
ORDER BY calls DESC;
```

## Default Intent Buckets

Use these as a starting point, then adjust based on actual conversations:

- 流水/启动/串流排障: `flow`, `flowid`, `流水`, `启动`, `串流`, `WebRTC`, `RTT`, `异常 VM`, `加载游戏`, `ResourceID`
- 数据库/Metabase/SQL: `Metabase`, `SQL`, `MySQL`, `DB`, `数据库`, `库表`, `表结构`, `原始 SQL`, `URL`, `链接`
- 游戏/BIZ/GID/标识: `gid`, `biz`, `uuid`, `thirdid`, `app_user_id`, `游戏信息`, `ProjectID`, `AppID`, `计费`
- IaaS/实例/镜像/任务: `IaaS`, `实例`, `镜像`, `task`, `任务`, `OAID`, `RBD`, `归档`, `GORM`, `vmid`
- 质量/趋势/监控: `质量`, `大盘`, `趋势`, `告警`, `监控`, `周度`, `恶化`, `成功率`, `失败率`
- 带宽/报表交付: `带宽`, `上报点数`, `每个 IP`, `报表`, `xlsx`, `文件`
- Agent 能力探索: `agent`, `工具`, `能力`, `当前目录`, `机器 IP`, `你能查什么`, `你可以调用哪些`

## Report Template

The final Markdown report should include:

1. 标题、生成时间、数据窗口、数据源。
2. 统计口径。
3. 总览结论。
4. 高置信度解决数补充。
5. 每日使用趋势。
6. 用户意图分布。
7. 活跃用户画像。
8. 代表性问题，按意图类别分组。
9. 工具使用与可靠性。
10. Skill 使用情况。
11. 用户追问信号。
12. 优化建议。
13. 限制与风险。
14. 一句话总结。

## Safety Notes

- Never write production passwords, tokens, Authorization headers, concrete AK/SK values, or private long-log payloads into the report.
- If the user asks for representative questions, quote enough to preserve intent but redact sensitive parts.
- Make clear when a metric is heuristic. Solved-case estimates are confidence-based approximations unless explicit satisfaction feedback exists.
- Do not modify application code as part of this skill.
