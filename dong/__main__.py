"""支持通过 `python -m dong` 启动 CLI。"""
from dong.cli import main

# 模块执行入口保持极薄，只负责转发到真正的 CLI 主函数。
main()
