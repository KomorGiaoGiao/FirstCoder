"""OS clipboard helpers for multimodal paste."""

from __future__ import annotations

import platform
import subprocess
import tempfile
from pathlib import Path


def read_clipboard_image_bytes() -> bytes | None:
    """Return image bytes from the system clipboard when available.

    Supports macOS, Linux clipboard tools, and Windows PowerShell.
    Returns None when the clipboard has no image or the platform is unsupported.
    """

    system = platform.system()
    if system == "Darwin":
        return _read_macos_clipboard_image()
    if system == "Linux":
        return _read_linux_clipboard_image()
    if system == "Windows":
        return _read_windows_clipboard_image()
    return None


def _read_linux_clipboard_image() -> bytes | None:
    commands = (
        ["wl-paste", "--no-newline", "--type", "image/png"],
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    )
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0 and completed.stdout:
            return completed.stdout
    return None


def _read_windows_clipboard_image() -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="firstcoder-clipboard-") as tmp:
        out_path = Path(tmp) / "clipboard.png"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$image = [Windows.Forms.Clipboard]::GetImage(); "
            f'if ($null -ne $image) {{ $image.Save("{out_path}", '
            "[System.Drawing.Imaging.ImageFormat]::Png) }}"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return out_path.read_bytes()
    return None


def _read_macos_clipboard_image() -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="firstcoder-clipboard-") as tmp:
        out_path = Path(tmp) / "clipboard.png"
        # Prefer PNG; fall back to TIFF and convert via sips when needed.
        script = f"""
        set outPath to "{out_path.as_posix()}"
        try
          set pngData to the clipboard as «class PNGf»
          set fileRef to open for access (POSIX file outPath) with write permission
          set eof fileRef to 0
          write pngData to fileRef
          close access fileRef
          return "png"
        on error
          try
            close access (POSIX file outPath)
          end try
        end try
        try
          set tiffPath to outPath & ".tiff"
          set tiffData to the clipboard as «class TIFF»
          set fileRef to open for access (POSIX file tiffPath) with write permission
          set eof fileRef to 0
          write tiffData to fileRef
          close access fileRef
          return "tiff:" & tiffPath
        on error
          try
            close access (POSIX file (outPath & ".tiff"))
          end try
          return "none"
        end try
        """
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        result = (completed.stdout or "").strip()
        if result == "png" and out_path.exists() and out_path.stat().st_size > 0:
            return out_path.read_bytes()
        if result.startswith("tiff:"):
            tiff_path = Path(result.split(":", 1)[1])
            if not tiff_path.exists() or tiff_path.stat().st_size == 0:
                return None
            converted = Path(tmp) / "clipboard-converted.png"
            try:
                convert = subprocess.run(
                    ["sips", "-s", "format", "png", str(tiff_path), "--out", str(converted)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if convert.returncode == 0 and converted.exists() and converted.stat().st_size > 0:
                return converted.read_bytes()
            # Last resort: return raw TIFF bytes (providers usually reject TIFF).
            return None
        return None
