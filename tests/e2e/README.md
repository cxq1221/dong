# 端到端自动化测试

这个目录只放从用户入口贯穿多层边界的自动化回归测试，例如：

- `cli.main()` 单次 prompt 到 `run_loop()` 的完整路径
- `dong logs` / `dong mcp` 等本地 CLI 子命令入口
- TTY/REPL 用户入口行为
- session resume、工具执行、上下文压缩和持久化组合路径
- 多工具链行为，例如 edit 后 grep、skill_search 后 skill_load、fetch 后总结

模块级测试仍放在 `tests/test_*.py`。真实 LLM/API 端到端验证不放进 pytest，按 README 的手动命令执行，避免常规回归依赖外部服务。
