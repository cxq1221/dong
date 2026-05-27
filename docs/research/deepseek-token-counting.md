# DeepSeek token 计算选型调研

## 目标

为 dong 替换旧的 `JSON bytes / 4` 近似 token 计算，选择一个能使用 DeepSeek 官方 tokenizer、适合本项目小型 CLI agent 架构的实现方案。

## 当前状态

- 当前入口在 `dong/context_compaction.py`：
  - `estimate_request_tokens()` 把 `messages`、`instructions`、`tools` 都计入输入预算。
  - `_estimated_tokens()` 再加 `max_output_tokens` 作为输出预留。
- 旧的 `JSON bytes / 4` 逻辑已经移除；通用近似路径统一使用 `tiktoken` 并记录 approximate 日志。

## 官方证据

- DeepSeek API 文档提供 `deepseek_tokenizer.zip`，用于离线计算 token 用量。
- 官方 zip 内包含：
  - `tokenizer.json`
  - `tokenizer_config.json`
  - `deepseek_tokenizer.py`
- 官方示例使用 `transformers.AutoTokenizer.from_pretrained("./", trust_remote_code=True)` 加载本地 tokenizer 目录。
- DeepSeek-V3 HuggingFace 示例同样使用 `AutoTokenizer.from_pretrained("deepseek-ai/DeepSeek-V3", trust_remote_code=True)`，并通过 `apply_chat_template(..., tokenize=True)` 计算 chat prompt。
- DeepSeek-V3.1 发布说明明确写到 tokenizer 和 chat template 已更新，所以 tokenizer 资产必须按模型族/版本隔离，不能用一个泛化 `deepseek` 目录长期复用。

## 候选方案

| 方案 | 准确性 | 引入成本 | 适配 dong | 结论 |
|------|--------|----------|-----------|------|
| DeepSeek 官方 tokenizer + `transformers.AutoTokenizer` | 高，贴近官方离线计算路径 | 中，需要 `transformers` 和 tokenizer 资产 | 高，只替换 token 计数层 | 选择 |
| HuggingFace `tokenizers` 直接加载 `tokenizer.json` | 中高，但 chat template 处理弱 | 低到中 | 中，需要自己维护 template 渲染 | 暂缓 |
| LiteLLM `token_counter` + custom tokenizer | 中高，可接 HF tokenizer | 高，会引入 gateway 抽象 | 低到中，超出 dong 当前 LLM 层边界 | 不选 |
| LlamaIndex tokenizer callable | 中高，可接 HF tokenizer | 高，会引入索引/agent 框架心智 | 低，只为计数太重 | 不选 |
| LangChain token counter | 多为近似或 provider 特定 | 高 | 低，框架侵入大 | 不选 |
| `tiktoken` | OpenAI 准确，其他模型近似 | 低 | 高 | 作为通用 fallback |
| 当前 `JSON bytes / 4` | 低到中 | 无 | 已删除 | 不保留 |

## 多模型 token 计数库

### LiteLLM

LiteLLM 是最成熟的多模型 token/cost 工具之一。它提供：

- `token_counter`
- `encode` / `decode`
- `completion_cost`
- `get_max_tokens`
- `model_cost`
- `register_model`
- `create_tokenizer` / `create_pretrained_tokenizer`

LiteLLM 文档说明：它会按模型选择 tokenizer；如果没有特定 tokenizer，会回退到 `tiktoken`。它也允许用户传入自定义 tokenizer。这个能力可以接 DeepSeek 官方 tokenizer，但对 dong 来说缺点是引入 LLM gateway 层心智，容易和现有 `dong/llm.py` provider adapter 重叠。

结论：**能力够，但作为 dong 的底层依赖偏重；适合做参考，不建议直接接入。**

### Tokencost

Tokencost 支持 token counting 和成本估算。它对 OpenAI 使用 `tiktoken`，对 Anthropic 3.5/3 系列使用 Anthropic token counting API，对旧 Claude 使用 `tiktoken` 近似。

结论：**适合成本估算，不适合 DeepSeek 官方 tokenizer 主路径。**

### Tokemon

Tokemon 提供统一接口，支持 OpenAI、Anthropic、Google AI、xAI。OpenAI 使用 `tiktoken` 离线计算；Anthropic、Google 这类 provider 需要 API key 或 provider API。

结论：**多 provider 接口轻，但当前公开能力没有 DeepSeek 官方 tokenizer 路径。**

### token-count / tokenpal

这类库通常覆盖 OpenAI、Anthropic、Google：

- OpenAI：本地 `tiktoken`
- Anthropic：`messages.count_tokens`
- Gemini：`models.countTokens`

结论：**适合三大闭源 provider 的统一接口，不覆盖 DeepSeek 官方 tokenizer。**

### 结论

如果目标是“多模型统一 token 计数”，LiteLLM/Tokemon/Tokencost 这类库存在；但如果目标是“DeepSeek 使用官方 tokenizer 且 dong 保持轻量”，最佳方案仍是自建薄路由：

```text
DeepSeek -> transformers.AutoTokenizer + 官方 tokenizer 文件
OpenAI   -> tiktoken
Claude   -> Anthropic count_tokens API 或 response usage
Gemini   -> Google countTokens API 或 response usage
Fallback -> tiktoken，并记录 token_counter_approximate
```

这样只引入各 provider 的最小计数能力，不引入完整 agent/gateway 框架。

## Agent 实现调研

### opencode

调研对象：`opencode-ai/opencode` 官方源码。

opencode 当前不是本地 tokenizer 预估为主，而是使用 provider 返回的 usage：

- OpenAI provider 从 `completion.Usage.PromptTokens`、`CompletionTokens`、`PromptTokensDetails.CachedTokens` 归一化为内部 `TokenUsage`。
- Anthropic provider 从 `anthropic.Message.Usage` 读取 `InputTokens`、`OutputTokens`、cache creation/read token。
- Gemini provider 从 `GenerateContentResponse.UsageMetadata` 读取 prompt/candidate/cache token。
- Agent 层 `TrackUsage()` 把 provider usage 写回 session 的 `PromptTokens`、`CompletionTokens` 和 cost。
- TUI 用累计 token 与模型 `ContextWindow` 比较，达到 95% 且 `AutoCompact` 开启时触发总结压缩。

结论：**opencode 依赖 provider-reported usage 做累计和自动压缩判断，不提供通用本地 tokenizer。**

这说明 dong 也应该把 provider usage 作为事后权威；本地 tokenizer 只解决请求前 preflight。

### claw-code

调研对象：本机 `../Projects/claw-code`。

claw-code 分两层：

1. runtime compaction 层：
   - `estimate_message_tokens()` 对 text/tool/thinking block 使用 `len / 4 + 1` 估算。
   - `CompactionConfig.max_estimated_tokens` 默认是 `10_000`。
2. provider preflight 层：
   - 通用 `preflight_message_request()` 用 `serde_json::to_vec(value).len() / 4 + 1` 估算 `messages/system/tools/tool_choice`。
   - Anthropic provider 会先跑本地 byte estimate guard，再 best-effort 调用 `/v1/messages/count_tokens` 精修。
   - 如果 count_tokens 失败，保留本地估算结果；如果 count_tokens 成功且超过模型窗口，则抛 `ContextWindowExceeded`。
   - 模型窗口表由 `model_token_limit()` 维护，覆盖 Claude、Grok、GPT、Kimi、Qwen 等。

结论：**claw-code 没有多模型本地 tokenizer；它采用“本地 byte/4 保底 + Anthropic 官方 count_tokens 精修 + provider usage 累计”的工程化折中。**

对 dong 的启发：

- 保留 cheap fallback，但不能让它静默伪装成准确计数。
- Claude 计数应优先走 Anthropic count_tokens，而不是找第三方 tokenizer。
- DeepSeek 没有像 Anthropic count_tokens 这样的通用计数 endpoint 时，使用官方 tokenizer 文件是更好的 preflight 精修路径。

## 推荐方案

采用 **DeepSeek 官方 tokenizer 文件 + HuggingFace `transformers.AutoTokenizer`**。

建议新增模块：

```text
dong/token_counter.py
```

建议接口：

```python
def count_text_tokens(text: str, *, model: str | None = None) -> int:
    """使用模型 tokenizer 计算纯文本 token。"""

def count_request_tokens(
    messages: list,
    *,
    instructions: str = "",
    tools: list | None = None,
    model: str | None = None,
) -> int:
    """使用模型 chat template 计算一次请求的输入 token。"""
```

DeepSeek 模型路径建议：

```text
dong/tokenizers/deepseek_v3/
  tokenizer.json
  tokenizer_config.json
```

模型映射建议：

```python
TOKENIZER_DIRS = {
    "deepseek-v4-pro": "deepseek_v3",
    "deepseek-v4-flash": "deepseek_v3",
}
```

如果后续接入 DeepSeek-V3.1 或 V4 官方 tokenizer，应新增目录，例如：

```text
dong/tokenizers/deepseek_v3_1/
dong/tokenizers/deepseek_v4/
```

不要覆盖旧目录，避免历史 session 的 token 计算语义漂移。

## 请求计数边界

DeepSeek chat 请求不能只 `encode(content)`，否则会漏算 role、system、assistant 起始标记、tool call、tool result 和 chat template 特殊 token。

实现应优先使用：

```python
tokenizer.apply_chat_template(
    messages,
    tokenize=True,
    add_generation_prompt=True,
)
```

`instructions` 需要先合并为 `system` message；`tools` 没有 DeepSeek 官方统一模板时，建议作为近似附加计数：

```python
tool_tokens = len(tokenizer.encode(json.dumps(tools, ensure_ascii=False)))
```

这是比旧的 `JSON bytes / 4` 更接近真实请求的方案，但 provider 端实际 usage 仍是最终权威。

## 依赖建议

在 `pyproject.toml` 增加：

```toml
"tiktoken>=0.13.0",
"transformers>=4.52.0",
```

不建议同时引入 LiteLLM/LangChain/LlamaIndex。dong 现在已有自己的 `llm.py` provider adapter 和 `context_compaction.py`，引入 agent framework 只为 token 计算会制造重复控制面。

## Fallback 策略

不保留 `JSON bytes / 4`。DeepSeek 官方 tokenizer 缺失、Claude `count_tokens` 不可用、未知模型等非官方路径统一退到 `tiktoken`，并写日志：

```text
event=token_counter_approximate
fields={"model": "...", "encoding": "o200k_base", "reason": "..."}
```

这样线上如果 tokenizer 资产缺失，不会静默伪装成精确估算。

## 结论

dong 应选择 **官方 tokenizer 资产 + `transformers.AutoTokenizer`**。这是最小、直接、可验证的替代方案：既使用 DeepSeek 官方 tokenizer，又不把 dong 迁移到 LiteLLM/LangChain/LlamaIndex 这类更重框架。
