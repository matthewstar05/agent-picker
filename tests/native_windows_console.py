"""Exercise a real Windows console, including the no-provider Connections screen."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def run():
    import msvcrt
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleWindow.restype = wintypes.HWND
    if not kernel.GetConsoleWindow() and not kernel.AllocConsole():
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    # Console buffers need read and write access and shared opening; Python's
    # open() requests write-only CONOUT$, which a fresh console rejects.
    for name, std, descriptor in (("CONIN$", -10, 0), ("CONOUT$", -11, 1), ("CONOUT$", -12, 2)):
        handle = kernel.CreateFileW(name, 0xC0000000, 3, None, 3, 0, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        kernel.SetStdHandle(std & 0xFFFFFFFF, handle)
        os.dup2(msvcrt.open_osfhandle(handle, os.O_RDONLY if descriptor == 0 else os.O_WRONLY), descriptor)
    sys.stdin = open(0, "r", encoding="utf-8", closefd=False)
    sys.stdout = open(1, "w", encoding="utf-8", closefd=False)
    sys.stderr = open(2, "w", encoding="utf-8", closefd=False)

    class Key(ctypes.Structure):
        _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD), ("virtual", wintypes.WORD),
                    ("scan", wintypes.WORD), ("character", wintypes.WCHAR), ("control", wintypes.DWORD)]

    class Event(ctypes.Union):
        _fields_ = [("key", Key), ("padding", ctypes.c_byte * 16)]

    class Record(ctypes.Structure):
        _fields_ = [("kind", wintypes.WORD), ("event", Event)]

    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Record), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.GetStdHandle(-10)

    def inject(text):
        # Console records carry UTF-16 units, including surrogate pairs.
        raw = text.encode("utf-16-le")
        text = "".join(chr(int.from_bytes(raw[i:i + 2], "little")) for i in range(0, len(raw), 2))
        records = (Record * len(text))()
        for record, character in zip(records, text):
            record.kind = 1
            record.event.key = Key(True, 1, 0, 0, character, 0)
        written = wintypes.DWORD()
        if not kernel.WriteConsoleInputW(handle, records, len(records), ctypes.byref(written)):
            raise ctypes.WinError(ctypes.get_last_error())

    from platform_runtime import Terminal
    with Terminal() as terminal:
        inject("xαà😀\x1b[A\x1b")
        assert terminal.wait(1)
        assert [terminal.read_key() for _ in range(6)] == ["x", "α", "à", "😀", "up", "esc"]
        terminal.write("\x1b[38;2;100;180;220mUnicode console check α ✓\x1b[0m\n")
    import agent_picker
    from unittest.mock import patch
    inject("q")
    with patch.object(agent_picker, "installed", return_value=False), patch.object(agent_picker, "resolve_argv", return_value=None):
        assert agent_picker.choose("connections") is None
    return {"console_input": "passed", "connections": "passed"}


if __name__ == "__main__":
    marker = Path(sys.argv[1])
    try:
        marker.write_text(json.dumps(run()), encoding="utf-8")
    except BaseException:
        marker.write_text(traceback.format_exc(), encoding="utf-8")
        raise
