#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Снимок конкретного окна (только его, не всего экрана) — для проверки вёрстки.

Окно временно поднимается поверх остальных, чтобы снимок не зависел от того,
что лежит сверху.

    python tools/shot.py <заголовок окна> <куда сохранить.png>
"""
import sys
import time
from ctypes import byref, windll, wintypes

from PIL import ImageGrab

user32 = windll.user32

HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


def main() -> int:
    title = sys.argv[1]
    out = sys.argv[2]

    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        print("окно не найдено")
        return 1

    user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    )
    time.sleep(0.5)

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, byref(rect))
    box = (rect.left, rect.top, rect.right, rect.bottom)
    print("окно:", box)

    ImageGrab.grab(bbox=box, all_screens=True).save(out)
    user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    print("сохранено:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
