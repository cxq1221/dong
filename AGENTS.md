# dong project rules

## 编码原则
1. ⚠️不要猜测，不懂就问，一次只问一个问题
2. ⚠️不加没人要求的功能，遵循 KISS 原则（Keep It Simple, Stupid）
3. ⚠️改代码时只动必须改的地方，严禁顺手重构未损坏的代码。
4. ⚠️将任务描述为可验证的目标（如“写端到端测试并执行”，而非“添加验证”）
5. ⚠️任务完成后，需要使用skill：redundancy-gates 检查代码
6. ⚠️所有代码都要增加中文注释，主要介绍流程和定义，每个类/文件开头要说明定义
## 注意事项
- Code style: Python 3.11+, f-strings over %, type hints everywhere
- Tools use `@registry.register()` decorator pattern
- Tests: pytest, in `tests/`, one file per module
- All file I/O must use `_validate_path()` to prevent traversal
- Import order: stdlib → third-party → local
- No external shell=True calls without list-form subprocess.run
- 每次修改后端代码后，先执行 `uv run ruff check --fix cg_analyzer tests`
- 如果后端做了修改，除了上述测试以外，还要询问是否要进行创建端到端测试用例，或者是否需要执行单纯的端到端测试，即实际发起调用接口进行流式对话，检查执行完成之后的结果，即执行一次和用户体验一致的测试
