# dong project rules

## 编码原则
1. ⚠️不要猜测，不懂就问，一次只问一个问题
2. ⚠️不加没人要求的功能，遵循 KISS 原则（Keep It Simple, Stupid）
3. ⚠️改代码时只动必须改的地方，严禁顺手重构未损坏的代码。
4. ⚠️将任务描述为可验证的目标（如“执行真实 LLM/API 对话端到端验证”，而非“添加验证”）
5. ⚠️任务完成后，需要使用skill：redundancy-gates 检查代码
6. ⚠️所有代码都要增加中文注释，主要介绍流程和定义，每个类/文件开头要说明定义
## 注意事项
- 每次增加新的特性，先调研 ../Projects/claw-code 的相关功能的实现方式
- Code style: Python 3.11+, f-strings over %, type hints everywhere
- Tools use `@registry.register()` decorator pattern
- Tests: pytest, in `tests/`, one file per module
- All file I/O must use `_validate_path()` to prevent traversal
- Import order: stdlib → third-party → local
- No external shell=True calls without list-form subprocess.run
- 每次修改后端代码后，先执行 `uv run ruff check --fix cg_analyzer tests`
- 如果后端做了修改，自动创建自动化测试用例，并根据修改的影响面，选择是否执行真实 LLM/API 对话端到端验证。这里的“端到端”特指：用真实 API 配置启动 dong，实际向 LLM 发起一次对话，让模型按用户入口完成一轮工具/skill/流式对话路径，并检查最终结果；pytest 中命名为 e2e 的测试只能算自动化回归测试，不能替代这个真实用户体验一致的验证。
