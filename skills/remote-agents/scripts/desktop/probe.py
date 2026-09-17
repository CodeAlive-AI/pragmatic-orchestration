"""Run in the interactive Windows session, never in the SSH desktop."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time


def capture():
    """Refuse locked, noninteractive, and unusable captures."""
    result = {'ready': False, 'at': time.time()}
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                     wintypes.LPVOID, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD)]
        handle = user32.OpenInputDesktop(0, False, 1)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not user32.GetUserObjectInformationW(handle, 2, name,
                                                   ctypes.sizeof(name), ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            result['input_desktop'] = name.value
            if name.value.lower() != 'default':
                raise RuntimeError('The input desktop is not the unlocked Default desktop')
        finally:
            user32.CloseDesktop(handle)
        session = wintypes.DWORD()
        if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            raise RuntimeError('Cannot determine session ID')
        if not session.value:
            raise RuntimeError('Probe ran in noninteractive session zero')
        from PIL import ImageGrab
        image = ImageGrab.grab(all_screens=True)
        if image.width < 640 or image.height < 480 or all(lo == hi for lo, hi in image.getextrema()):
            raise RuntimeError('Capture is empty, uniform, or too small')
        result.update(ready=True, session_id=session.value, width=image.width, height=image.height)
    except Exception as exc:
        result['error'] = str(exc)
    return result, image if result['ready'] else None


def main():
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    result, image = capture()
    if image is not None:
        image.save(output / 'desktop.png')
    (output / 'result.json').write_text(json.dumps(result), encoding='utf-8')


if __name__ == '__main__':
    main()
