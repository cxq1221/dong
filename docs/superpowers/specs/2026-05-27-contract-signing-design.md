# dong 契约签订设计

## 背景

dong 需要在复杂开发任务中引入一种类似人类项目交付的隐形压力。这个机制不是强制流程引擎，也不是让主 Agent 填表自我感动，而是让主 Agent 明确知道：复杂交付结束后会被第三方审计，低质量交付会降低本地声誉，并影响后续 session 的提示词压力和验收门槛。

首版目标是建立“契约规范包 + 签名仪式 + 第三方审计评分 + 本地声誉账本”。主 Agent 可以参考契约最佳实践，也可以忽略；但忽略带来的证据不足、风险披露不足、验证不足，会在第三方评分中体现。

## 目标

- 在复杂任务中自动注入契约压力，不阻断正常开发流程。
- 提供可读的契约最佳实践材料，让 Agent 自主参考。
- 在交付后生成本次证据包，并要求执行有计算成本的签名仪式。
- 使用第三方评分 Agent 评分，主 Agent 不能自评。
- 用程序规则底座约束评分，避免评分 Agent 无依据地给高分。
- 把评分结果写入 session 和 workspace，让同一 session 吸取教训，让新 session 感知长期声誉。

## 非目标

- 首版不做完整项目管理状态机。
- 首版不把契约流程做成硬性阻塞，除非用户明确启用更严格模式。
- 首版不做夸张 UI 羞辱展示。
- 首版不引入新依赖。
- 首版不要求 scorer 使用独立模型，但设计保留后续独立配置入口。

## 触发与控制

契约机制是复杂任务压力层。dong 根据本地可观测信号自动触发，而不是依赖模型自觉判断。

自动触发信号包括：

- 本轮发生文件写入或编辑。
- 本轮执行测试、构建、lint、格式化或其他验证命令。
- 工具调用超过阈值，例如 5 次。
- 发生上下文压缩，说明任务已经有较长过程。
- 用户显式开启契约模式。

用户控制命令：

- `/contract on`：本 session 或本轮强制启用契约压力。
- `/contract off`：本轮关闭契约压力，关闭行为会被记录，scorer 可参考。
- `/contract status`：展示当前触发状态、触发原因、平均分、压力等级和最近教训。

触发后，dong 在系统提示词中注入短摘要：

- 当前 session 或 workspace 的声誉压力。
- 最近低分原因和 scorer 给出的具体教训。
- 契约最佳实践摘要。
- 明确提醒：Agent 可以不参考这些规范，但交付后会被第三方审计，低分会影响后续声誉和验收门槛。

## 契约最佳实践材料

稳定材料放在 `.dong/contracts/best-practices.md`。它是参考材料，不是流程状态机。内容应包含：

- 明确交付目标和用户约束。
- 修改前先理解现有上下文。
- 保留真实验证证据。
- 如实披露失败、未验证项和风险。
- 避免把漂亮总结当成验收材料。
- 复杂任务完成前自查交付是否可复现、可回滚、可审阅。

触发契约模式时，dong 注入该文档的短摘要。完整文档可被主 Agent 通过读取工具查看，但不会每轮完整塞进提示词。

## 本次交付证据包

任务结束后，dong 自动生成证据包。主 Agent 可以在最终答复中陈述事实和风险，但不能给自己打分。

证据包字段包括：

- `contract_version`：契约格式版本。
- `session_id`：当前 session。
- `trigger_reasons`：触发契约的本地信号。
- `user_objective`：用户原始需求摘要。
- `tool_summary`：工具调用摘要，包括失败工具。
- `file_changes`：文件修改摘要。
- `verification_evidence`：测试、lint、构建、真实 LLM/API 验证等证据。
- `final_answer`：主 Agent 最终答复。
- `known_risks`：主 Agent 披露或 dong 观察到的风险。
- `unverified_items`：未验证项。
- `signature`：签名仪式结果。
- `scorer_result`：第三方评分结果。

证据包写入 `.dong/contracts/<session-id>-<timestamp>.json`，同时在 session JSONL 中记录关键事件。

## 签名仪式

签名是首版契约感的核心。它不是普通确认字段，而是一个有成本的本地 proof-of-work 任务。

签名输入：

- `session_id`
- `evidence_hash`
- `contract_version`
- `nonce`
- `difficulty`

签名算法：

- 对规范化后的输入做哈希。
- 不断递增或随机尝试 `nonce`。
- 直到哈希满足难度条件，例如前 N 个十六进制字符为 `0`。

签名输出：

- `evidence_hash`
- `nonce`
- `difficulty`
- `elapsed_ms`
- `signature_hash`

默认签名耗时应在几秒到十几秒之间。低声誉时可以提高难度，或者要求更完整的证据包后才允许低风险评分。Agent 可以选择不签，但未签名、签名证据不匹配、签名前证据不足，都会成为严重扣分项。

签名含义是：主 Agent 愿意让这份交付材料接受审计并承担后果。签名不代表质量合格，也不代表主 Agent 自评。

## 第三方评分 Agent

scorer 是第三方评分者。它在任务结束后由 dong 通过独立 prompt 二次调用 LLM。首版复用当前 provider 配置，并预留以下独立 scorer 配置入口：

- `DONG_SCORER_MODEL`
- `DONG_SCORER_API_KEY`
- `DONG_SCORER_BASE_URL`

scorer 输入：

- 契约最佳实践材料。
- 本次证据包。
- 程序规则底座生成的评分约束。
- 历史声誉摘要。

scorer 输出必须是结构化 JSON：

- `score`：0 到 100。
- `deductions`：扣分项列表。
- `risk_flags`：风险标记。
- `lesson_for_session`：给当前 session 的短教训。
- `workspace_summary`：写入长期评分表的低分原因摘要。

主 Agent 不能生成、修改或覆盖 scorer 结果。scorer 结果写入证据包和 session。

## 规则底座

程序规则先根据可观测证据生成评分约束，scorer 在约束内裁量。规则底座不是最终评分者，但负责防止无证据高分。

首版规则：

- 修改了代码但没有任何验证证据，最高分受限。
- 有失败命令但最终答复未披露，必须扣分。
- 没有签名或签名校验失败，必须大幅扣分。
- 证据包缺少文件修改摘要，必须扣分。
- 用户要求真实 LLM/API 端到端验证但没有执行，必须扣分。
- 契约被手动关闭时不直接判失败，但 scorer 应结合交付风险扣分。

规则底座输出必须包含：

- `base_score_ceiling`
- `required_deductions`
- `evidence_gaps`
- `signature_valid`

## 评分表与压力等级

workspace 级评分表写入 `.dong/scoreboard.json`。它记录长期声誉，不保存长篇 scorer 建议。

评分表结构必须包含：

- `version`
- `average_score`
- `recent_scores`
- `pressure_level`
- `common_deductions`
- `sessions`

pressure level 示例：

- `normal`：平均分较高，只注入轻量提醒。
- `watch`：最近有明显扣分，注入具体警示并要求更清楚的验证证据。
- `probation`：低分持续存在，提高签名难度，降低无验证交付的评分上限。

同一 session 还会保存 `lesson_for_session`。后续轮次注入短教训，例如：

> 上次交付因为未运行相关测试被扣分；本 session 后续代码修改必须优先补充验证证据。

新 session 只看到总体声誉压力和常见低分原因，不继承过长的历史细节。

## 模块边界

新增 `dong/contract.py` 作为契约模块，避免继续扩大 `cli.py`。

`dong/contract.py` 职责：

- 收集复杂任务触发信号。
- 读取契约最佳实践并生成摘要。
- 生成证据包。
- 计算和校验 proof-of-work 签名。
- 生成规则底座评分约束。
- 组装 scorer prompt。
- 校验 scorer JSON。
- 读写 `.dong/scoreboard.json`。
- 生成提示词压力摘要和 session lesson 摘要。

`dong/cli.py` 职责：

- 接入 `/contract on|off|status`。
- 在工具执行、上下文压缩、最终答复阶段上报信号。
- 在最终答复后触发契约证据包、签名、scorer 和落盘。
- 在后续 LLM 请求前注入短压力摘要。

`dong/session.py` 职责：

- 保存契约相关 session 事件。
- 支持恢复后读取同 session 的 lesson。

session 事件类型必须包含：

- `contract_triggered`
- `contract_signed`
- `contract_scored`
- `contract_lesson`

## 数据流

1. 用户发起任务。
2. dong 根据工具调用、文件修改、命令执行、上下文压缩或用户命令判断是否进入契约压力模式。
3. LLM 请求前注入短压力摘要和契约最佳实践摘要。
4. 主 Agent 正常执行任务。
5. 最终答复后，dong 生成本次交付证据包。
6. dong 执行 proof-of-work 签名并写入证据包。
7. dong 生成规则底座评分约束。
8. dong 调用第三方 scorer Agent。
9. dong 校验 scorer JSON，把结果写入证据包、session 和 scoreboard。
10. 后续同 session 轮次注入 scorer 的具体教训；新 session 注入长期声誉摘要。

## 错误处理

- 证据包写入失败：向用户显示明确错误，记录日志，不伪造评分。
- 签名超时：记录 `signature_timeout`，scorer 必须看到该事实。
- scorer 调用失败：记录 `scorer_failed`，scoreboard 不更新最终分，只记录待评分事件。
- scorer JSON 无效：重试一次；仍无效则记录失败，不让主 Agent 自行补分。
- `/contract off`：记录关闭事件，不生成硬错误。

## 日志

新增 grep 友好的结构化事件：

- `contract_triggered`
- `contract_pressure_injected`
- `contract_evidence_created`
- `contract_signature_started`
- `contract_signature_finished`
- `contract_signature_failed`
- `contract_rule_floor_created`
- `contract_scorer_started`
- `contract_scorer_finished`
- `contract_scorer_failed`
- `contract_scoreboard_updated`

默认日志只记录摘要、长度、状态和路径，不记录敏感正文。需要正文预览时沿用 `DONG_LOG_PAYLOADS=1` 的既有约束。

## 测试策略

单元测试覆盖 `dong/contract.py`：

- 复杂任务触发规则。
- 证据包 hash 稳定性。
- proof-of-work 签名和校验。
- 规则底座评分上限。
- scoreboard 均分和压力等级。
- scorer JSON 校验。
- `lesson_for_session` 注入摘要。

CLI 集成测试使用 fake LLM：

- 自动触发复杂任务后生成契约文件和 scoreboard。
- `/contract on`、`/contract off`、`/contract status` 生效。
- 主 Agent 没有自评分字段。
- scorer 的 `lesson_for_session` 会进入同 session 后续提示词。
- 低分会提高证据要求或签名难度。

真实 LLM/API 端到端验证：

- 用真实 API 配置启动 dong。
- 让模型完成一个小代码修改。
- 验证模型看到契约压力。
- 验证最终生成证据包、签名和 scorer 评分。
- 验证下一轮同 session 能看到 scorer 教训。

## 验收标准

- 复杂开发任务能自动进入契约压力模式。
- 用户可以手动开启、关闭、查看契约状态。
- 契约最佳实践材料存在且会被摘要注入。
- 主 Agent 不能自评。
- 证据包包含签名结果，签名可校验且有真实耗时。
- 第三方 scorer 输出结构化评分。
- 程序规则底座能限制无证据高分。
- `.dong/scoreboard.json` 能维护平均分、压力等级和常见扣分原因。
- 同一 session 后续轮次能吸收 scorer 的具体教训。
- fake LLM 回归测试和真实 LLM/API 端到端验证均有证据。
