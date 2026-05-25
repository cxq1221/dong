"""真实 TTY 交互端到端测试，覆盖 prompt_toolkit 路径而非 stdin fallback。"""

from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time


def _write(path, content: str) -> None:
    """写入测试文件，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_until(fd: int, needle: str, *, timeout: float = 5.0) -> str:
    """从伪终端读取，直到看到目标文本或超时。"""
    deadline = time.monotonic() + timeout
    chunks: list[str] = []
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not data:
            break
        text = data.decode("utf-8", errors="replace")
        chunks.append(text)
        joined = "".join(chunks)
        if needle in joined:
            return joined
    raise AssertionError(f"timed out waiting for {needle!r}; output was:\n{''.join(chunks)}")


def _read_available(fd: int, *, duration: float = 0.5) -> str:
    """短暂收集当前伪终端输出。"""
    deadline = time.monotonic() + duration
    chunks: list[str] = []
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError:
            break
        if not data:
            break
        chunks.append(data.decode("utf-8", errors="replace"))
    return "".join(chunks)


def test_interactive_tty_repl_shows_slash_skill_menu_and_runs_commands(tmp_path) -> None:
    """真实 TTY 下输入 / 应显示 skill 候选，并可继续执行 REPL 命令。"""
    _write(tmp_path / ".dong" / "skills" / "review.md", "# Review")
    _write(tmp_path / ".dong" / "skills" / "python-test.md", "# Python test")

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    env = {
        **os.environ,
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "dong", "-d", str(tmp_path)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=os.getcwd(),
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        output = _read_until(master_fd, "dong")
        assert "dong" in output

        os.write(master_fd, b"/")
        menu_output = _read_until(master_fd, "/python-test")
        assert "/skill" in menu_output
        assert "/python-test" in menu_output
        assert "/review" in menu_output

        _read_available(master_fd, duration=0.2)
        os.write(master_fd, b"re")
        filtered_output = _read_until(master_fd, "/review")
        assert "/review" in filtered_output

        # Clear the partially typed slash command, then exercise real REPL commands.
        os.write(master_fd, b"\x15/skill review\r")
        load_output = _read_until(master_fd, "Loaded skill")
        assert "review" in load_output
        _read_until(master_fd, "dong", timeout=2.0)

        os.write(master_fd, b"/unskill review\r")
        remove_output = _read_until(master_fd, "Removed skill: review")
        assert "Removed skill: review" in remove_output
        _read_until(master_fd, "dong", timeout=2.0)

        os.write(master_fd, b"exit\r")
        proc.wait(timeout=5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)
