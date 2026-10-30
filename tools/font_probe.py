#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сравнение шрифтов в реальном рендере Tk (для выбора гарнитуры)."""
import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

SAMPLES = ["Голосовой запуск", "Слушаю микрофон", "Кодовая фраза — «олимпиец на месте»"]
FONTS = ["Inter", "Segoe UI", "Calibri", "Verdana", "Tahoma", "Trebuchet MS",
         "Bahnschrift", "Arial", "Georgia"]


def main() -> None:
    from tkinter import font as tkfont

    app.enable_dpi_awareness()
    app.load_bundled_fonts()

    root = tk.Tk()
    root.title("font test")
    root.configure(bg="#121317")
    root.geometry("900x620")

    families = set(tkfont.families(root))
    for name in FONTS:
        if name not in families:
            continue
        row = tk.Frame(root, bg="#121317")
        row.pack(fill="x", padx=14, pady=5)
        tk.Label(row, text=name, bg="#121317", fg="#8a8f9a", font=(name, 9),
                 width=14, anchor="w").pack(side="left")
        tk.Label(row, text=SAMPLES[0], bg="#121317", fg="#edeff3",
                 font=(name, 19)).pack(side="left")
        tk.Label(row, text=SAMPLES[1], bg="#121317", fg="#4ade80",
                 font=(name, 12)).pack(side="left", padx=(18, 0))

    root.after(15000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
