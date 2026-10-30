#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Тест логики «услышал → решил → запустил» без микрофона.

    python tools/test_engine.py

Подсовывает движку тексты (как будто их распознал Vosk) и проверяет,
какие из них приводят к срабатыванию.
"""
import queue
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

CASES = [
    ("олимпиец на месте", True),
    ("Олимпиец на месте!", True),
    ("олимпиец на мсте", True),
    ("олимпийцы на месте", True),
    ("на месте олимпиец", True),
    ("олимпиада на месте", False),
    ("олимпиец", False),
    ("привет как дела", False),
    ("где мой кофе", False),
]


def main() -> int:
    cfg = dict(app.DEFAULT_CONFIG)
    cfg["cooldown_seconds"] = 0

    events: "queue.Queue[tuple]" = queue.Queue()
    engine = app.Engine(cfg, events)

    fails = 0
    for text, expected in CASES:
        engine.buffer = []
        engine.last_hit_at = 0.0
        engine._handle(text)

        fired = False
        while True:
            try:
                kind = events.get_nowait()[0]
            except queue.Empty:
                break
            if kind == "hit":
                fired = True

        ok = fired == expected
        fails += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text:<26} сработало={fired!s:<6} ждали={expected}")

    # повтор того же текста в пределах кулдауна не должен срабатывать дважды
    cfg2 = dict(app.DEFAULT_CONFIG)
    cfg2["cooldown_seconds"] = 15
    events2: "queue.Queue[tuple]" = queue.Queue()
    engine2 = app.Engine(cfg2, events2)
    engine2._handle("олимпиец на месте")
    engine2._handle("олимпиец на месте")
    hits = 0
    while True:
        try:
            if events2.get_nowait()[0] == "hit":
                hits += 1
        except queue.Empty:
            break
    ok = hits <= 1
    fails += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} {'двойное срабатывание':<26} hits={hits}")

    print(f"\nПровалов: {fails}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
