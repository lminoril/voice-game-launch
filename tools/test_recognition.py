#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка конвейера на записанных wav: распознать и посчитать похожесть.

    python tools/test_recognition.py <папка с wav> [фраза]

Печатает таблицу: что услышано, какой балл, сработало бы или нет.
"""
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def main() -> int:
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    folder = Path(sys.argv[1])
    phrase = sys.argv[2] if len(sys.argv) > 2 else app.load_config()["phrase"]
    threshold = 88.0
    lang = app.phrase_language(phrase)

    if not app.model_ready(lang):
        print(f"нет модели для языка {lang!r}: {app.model_dir(lang)}")
        return 1

    model = Model(str(app.model_dir(lang)))
    fails = 0
    print(f"фраза: {phrase!r}, язык {lang}, порог {threshold}\n")

    for wav_path in sorted(folder.glob("*.wav")):
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())

        rec = KaldiRecognizer(model, rate)
        texts = []
        for start in range(0, len(frames), 8000):
            if rec.AcceptWaveform(frames[start:start + 8000]):
                text = json.loads(rec.Result()).get("text", "")
                if app.normalize(text):
                    texts.append(text)
        tail = json.loads(rec.FinalResult()).get("text", "")
        if app.normalize(tail):
            texts.append(tail)

        best_score, heard = 0.0, ""
        for text in texts:
            score = app.best_window_score(app.normalize(text).split(), phrase)
            if score > best_score:
                best_score, heard = score, text

        fires = best_score >= threshold
        print(f"{wav_path.stem:<12} услышано={heard!r:<28} балл={best_score:5.1f}  "
              f"{'СРАБОТАЕТ' if fires else 'молчит'}")
        fails += 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
