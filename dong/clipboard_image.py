"""系统剪贴板图片适配层，负责把粘贴的图片保存成本地 PNG 文件。"""

from __future__ import annotations

import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


class ClipboardImageError(RuntimeError):
    """剪贴板中没有可用图片或系统读取失败时抛出的用户可读异常。"""


def save_clipboard_image(workdir: str) -> Path:
    """读取系统剪贴板图片并保存到当前工作区的 .dong/clipboard-images。"""
    output_path = _new_clipboard_image_path(workdir)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    saved = _save_with_pillow(output_path)
    if not saved:
        system = platform.system()
        if system == "Darwin":
            saved = _save_with_macos_osascript(output_path)
        elif system == "Windows":
            saved = _save_with_windows_powershell(output_path)
        else:
            saved = _save_with_linux_clipboard_tool(output_path)

    if not saved or not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise ClipboardImageError("剪贴板中没有可粘贴的图片")
    return output_path


def _new_clipboard_image_path(workdir: str) -> Path:
    """生成不会覆盖旧图片的剪贴板图片路径。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path(workdir).expanduser().resolve() / ".dong" / "clipboard-images" / f"clipboard-{stamp}.png"


def _save_with_pillow(output_path: Path) -> bool:
    """优先使用可选 Pillow；未安装时静默交给系统专用实现。"""
    try:
        from PIL import Image, ImageGrab  # type: ignore[import-not-found]
    except ImportError:
        return False

    image = ImageGrab.grabclipboard()
    if image is None:
        return False
    if isinstance(image, Image.Image):
        image.save(output_path, "PNG")
        return True
    if isinstance(image, list):
        for item in image:
            candidate = Path(item)
            if candidate.is_file():
                with Image.open(candidate) as file_image:
                    file_image.save(output_path, "PNG")
                return True
    return False


def _save_with_macos_osascript(output_path: Path) -> bool:
    """在 macOS 上用 AppleScript 读取剪贴板 PNG 数据。"""
    script = [
        'set outPath to POSIX file "' + str(output_path).replace('"', '\\"') + '"',
        "try",
        "set imageData to the clipboard as «class PNGf»",
        "set fileRef to open for access outPath with write permission",
        "set eof fileRef to 0",
        "write imageData to fileRef starting at 0",
        "close access fileRef",
        "return \"ok\"",
        "on error",
        "try",
        "close access outPath",
        "end try",
        "return \"no-image\"",
        "end try",
    ]
    result = subprocess.run(
        ["osascript", *sum((["-e", line] for line in script), [])],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "ok"


def _save_with_windows_powershell(output_path: Path) -> bool:
    """在 Windows 上用 STA PowerShell 读取剪贴板图片。"""
    executable = (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
        or shutil.which("pwsh")
    )
    if executable is None:
        return False

    script = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$image = [System.Windows.Forms.Clipboard]::GetImage()
if ($null -eq $image) { exit 3 }
$image.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
$image.Dispose()
"""
    result = subprocess.run(
        [executable, "-NoProfile", "-STA", "-Command", script, str(output_path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _save_with_linux_clipboard_tool(output_path: Path) -> bool:
    """Linux 只做常见 Wayland/X11 剪贴板工具兜底。"""
    commands = []
    if shutil.which("wl-paste"):
        commands.append(["wl-paste", "--type", "image/png"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])

    for command in commands:
        with output_path.open("wb") as file:
            result = subprocess.run(
                command,
                stdout=file,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        if result.returncode == 0 and output_path.stat().st_size > 0:
            return True
        output_path.unlink(missing_ok=True)
    return False
