"""Windows console and clipboard helpers for contest mode.

Contest mode watches the clipboard: copy a question on the exam page and
answering starts by itself. Nothing has to be pasted into the console,
which on Windows is fragile (multi-line pastes, and a stray click puts the
window in "Select" mode, which freezes all program output).
"""

from __future__ import annotations

import sys

from .question import parse_question

CF_UNICODETEXT = 13
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080


def _win_clipboard() -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    if not user32.OpenClipboard(None):
        return ""  # another program holds it; the next poll will retry
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _tk_clipboard() -> str:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()
    except Exception:
        return ""


def clipboard_sequence() -> int | None:
    """Windows bumps this on every copy, even when the text is identical."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except Exception:
        return None


def read_clipboard() -> str:
    try:
        text = _win_clipboard() if sys.platform == "win32" else _tk_clipboard()
    except Exception:
        return ""
    return text.replace("\r\n", "\n").strip()


def looks_like_question(text: str) -> bool:
    """Copied text worth answering: options A-D, or a true/false statement."""
    if not (8 <= len(text) <= 4000):
        return False
    q = parse_question(text)
    if len(q.options) >= 2:
        return True
    return any(mark in text for mark in ("（）", "()", "（ ）", "对错", "判断"))


def has_console_input() -> bool:
    """True when stdin is a real Windows console (not a pipe or file)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mode = ctypes.c_uint32()
        return bool(kernel32.GetConsoleMode(kernel32.GetStdHandle(-10), ctypes.byref(mode)))
    except Exception:
        return False


def disable_quick_edit() -> None:
    """Stop a stray click from freezing output ("Select" mode) in cmd.exe."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE)
    except Exception:
        pass
