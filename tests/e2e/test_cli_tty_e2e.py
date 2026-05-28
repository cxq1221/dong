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


def _wait_file_contains(path, needle: str, *, timeout: float = 5.0) -> str:
    """等待文件内容包含指定文本。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if needle in text:
                return text
        time.sleep(0.05)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    raise AssertionError(f"timed out waiting for {needle!r}; file was:\n{text}")


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
        output = _read_until(master_fd, "dong >")
        assert "dong" in output

        os.write(master_fd, b"/")
        menu_output = _read_until(master_fd, "/sessions")
        assert "/skill" in menu_output
        assert "/sessions" in menu_output

        _read_available(master_fd, duration=0.2)
        os.write(master_fd, b"re")
        filtered_output = _read_until(master_fd, "review")
        assert "review" in filtered_output

        # Clear the partial slash command, then verify exact /skills opens the skill menu.
        os.write(master_fd, b"\x15/skills")
        skills_menu_output = _read_until(master_fd, "python-test")
        assert "review" in skills_menu_output

    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)


def test_interactive_tty_tui_accepts_input_while_agent_is_working(tmp_path) -> None:
    """inline TUI 下第一条仍在执行时，第二条输入应被接收并排队执行。"""
    runner = tmp_path / "run_fake_dong.py"
    calls_log = tmp_path / "calls.log"
    _write(
        runner,
        f"""
import sys
import time
from types import SimpleNamespace

from dong import cli


def fake_chat(messages, _tools, instructions="", **kwargs):
    prompt = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"][-1]["content"]
    with open({str(calls_log)!r}, "a", encoding="utf-8") as f:
        f.write(f"start {{prompt}}\\n")
        f.flush()
    if prompt == "first":
        time.sleep(1.0)
    with open({str(calls_log)!r}, "a", encoding="utf-8") as f:
        f.write(f"end {{prompt}}\\n")
        f.flush()
    return SimpleNamespace(role="assistant", content=f"done {{prompt}}", tool_calls=[])


cli.chat = fake_chat
sys.argv = ["dong", "-d", {str(tmp_path)!r}]
cli.main()
""",
    )

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, str(runner)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=os.getcwd(),
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        _read_until(master_fd, "dong >")

        os.write(master_fd, b"first\r")
        _read_until(master_fd, "working", timeout=3.0)
        os.write(master_fd, b"second\r")

        output = _read_until(master_fd, "done first", timeout=8.0)
        assert "done first" in output
        calls = _wait_file_contains(
            calls_log,
            "start first\nend first\nstart second\nend second",
            timeout=8.0,
        )
        assert calls.splitlines()[:4] == [
            "start first",
            "end first",
            "start second",
            "end second",
        ]

    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)


def test_interactive_tty_tui_writes_transcript_to_native_scrollback(tmp_path) -> None:
    """真实 TTY 中稳定 transcript 应进入普通 scrollback，且不启用全屏/鼠标捕获。"""
    runner = tmp_path / "run_scrollback_tui.py"
    _write(
        runner,
        """
import threading

from dong.tui import TuiApp


app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
app.append_item("assistant", "assistant", "\\n".join(f"line {index}" for index in range(80)))
threading.Timer(0.6, app.application.exit).start()
app.run()
""",
    )

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, str(runner)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=os.getcwd(),
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        output = _read_until(master_fd, "line 79", timeout=5.0)
        output += _read_available(master_fd, duration=0.5)
        assert "line 00" in output or "line 0" in output
        assert "\x1b[?1049h" not in output
        assert "\x1b[?1000h" not in output
        assert "\x1b[?1002h" not in output
        assert "\x1b[?1003h" not in output
        assert "\x1b[?1006h" not in output
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)


def test_interactive_tty_tui_ctrl_d_exits(tmp_path) -> None:
    """Ctrl-D 应直接退出 inline TUI。"""
    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    env = {
        **os.environ,
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
        _read_until(master_fd, "dong >")
        os.write(master_fd, b"\x04")
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            _read_available(master_fd, duration=0.1)
        if proc.poll() is None:
            raise subprocess.TimeoutExpired(proc.args, 5)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)


def test_interactive_tty_tui_does_not_capture_mouse_for_copy(tmp_path) -> None:
    """真实 TTY 不应接管鼠标复制；选区复制由终端原生处理。"""
    runner = tmp_path / "run_tui_native_copy.py"
    _write(
        runner,
        """
import threading

from dong.tui import TuiApp

app = TuiApp(process_input=lambda _text, _ui: False, completion_provider=lambda: [])
app.append_item("assistant", "assistant", "alpha\\nbeta", raw="alpha\\nbeta")
threading.Timer(0.6, app.application.exit).start()
app.run()
""",
    )

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, str(runner)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=os.getcwd(),
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    try:
        output = _read_until(master_fd, "alpha", timeout=5.0)
        output += _read_available(master_fd, duration=0.5)
        assert "alpha" in output
        assert "beta" in output
        assert "\x1b[?1000h" not in output
        assert "\x1b[?1002h" not in output
        assert "\x1b[?1003h" not in output
        assert "\x1b[?1006h" not in output
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)


def test_interactive_tty_sessions_enter_restores_visible_session_content(tmp_path) -> None:
    """真实 TTY 中 /sessions 按 Enter 后，应在 TUI 里显示恢复 session 的内容。"""
    from dong.session import SessionStore

    store = SessionStore(str(tmp_path))
    target = store.create(model="deepseek-v4-pro")
    restored_tail = "RESTORED_TAIL_VISIBLE_AFTER_ENTER"
    long_question = (
        "恢复测试旧问题 "
        + "x" * 140
        + f" {restored_tail}"
    )
    target.append_message({"role": "user", "content": long_question})
    target.append_message({"role": "assistant", "content": "恢复测试旧回答"})

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    env = {
        **os.environ,
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
        _read_until(master_fd, "dong >")

        os.write(master_fd, b"/sessions\r")
        picker_output = _read_until(master_fd, target.session_id, timeout=5.0)
        assert restored_tail not in picker_output

        os.write(master_fd, b"\x1b[B\r")
        restored_output = _read_until(master_fd, restored_tail, timeout=5.0)
        restored_output += _read_until(master_fd, "恢复测试旧回答", timeout=5.0)
        assert "恢复测试旧回答" in restored_output
        assert "Recent session content" not in restored_output
        assert "Selected session:" not in restored_output
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)


def test_interactive_tty_tui_ctrl_c_exits_with_resume_command(tmp_path) -> None:
    """空输入状态下 Ctrl-C 应直接退出，并打印可复制恢复命令。"""
    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    env = {
        **os.environ,
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
        output = _read_until(master_fd, "dong >")
        os.write(master_fd, b"\x03")
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            output += _read_available(master_fd, duration=0.1)
        if proc.poll() is None:
            raise subprocess.TimeoutExpired(proc.args, 5)
        output += _read_available(master_fd, duration=0.5)
        assert proc.returncode == 0
        assert "Resume this session" in output
        assert "dong -d" in output
        assert "--resume session-" in output
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        _read_available(master_fd, duration=0.1)
        os.close(master_fd)
