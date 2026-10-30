#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Голосовой запуск программы по кодовой фразе.

Слушает микрофон, офлайн распознаёт речь (Vosk) и запускает указанный .exe,
когда слышит заданную фразу. Крестик сворачивает окно в трей.

Запуск:
    python app.py            # или через start.cmd / ярлык
"""

from __future__ import annotations

import array
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "app.log"
ICON_PATH = APP_DIR / "icon.ico"
MODEL_DIR = APP_DIR / "model" / "vosk-model-small-ru-0.22"

MODEL_URLS = [
    "https://raw.githubusercontent.com/kercre123/vosk-models/main/vosk-model-small-ru-0.22.zip",
    "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
]

CREATE_NO_WINDOW = 0x08000000
BUFFER_WORDS = 20

DEFAULT_CONFIG = {
    "app_path": r"C:\Program Files (x86)\Steam\steamapps\common\Arc Raiders\PioneerGame.exe",
    "process_name": "PioneerGame.exe",
    "phrase": "олимпиец на месте",
    "threshold": 88.0,
    "cooldown_seconds": 15,
    "sample_rate": 16000,
    "device": None,
    "beep": True,
}

THRESHOLD_PRESETS = {"Строго": 92.0, "Обычно": 88.0, "Мягко": 82.0}

# ---------------------------------------------------------------------------
# Палитра
# ---------------------------------------------------------------------------
BG = "#121317"
CARD = "#1b1d23"
CARD_HI = "#262931"
FIELD = "#20232a"
TEXT = "#edeff3"
MUTED = "#8a8f9a"
BORDER = "#30343d"
ACCENT = "#4ade80"
ACCENT_INK = "#0b120c"
BLUE = "#60a5fa"
RED = "#f87171"
YELLOW = "#fbbf24"

log = logging.getLogger("voice")


# ---------------------------------------------------------------------------
# Чёткость на HiDPI
# ---------------------------------------------------------------------------

def enable_dpi_awareness() -> None:
    """Должно вызываться ДО создания окна, иначе Windows растянет картинку."""
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v1
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class Fonts:
    """Один шрифт на весь интерфейс, разные начертания.

    Segoe UI — системный шрифт Windows: в нём есть полноценная кириллица и
    латиница, и он отлично хинтован под GDI (которым рисует Tk). Модные
    веб-шрифты вроде Inter в GDI дают неровные штрихи, поэтому не они.
    """

    PREFERRED = ["Segoe UI Variable Text", "Segoe UI", "Tahoma", "Arial"]

    def __init__(self, root):
        from tkinter import font as tkfont

        families = {name.lower(): name for name in tkfont.families(root)}
        base = next((families[name.lower()] for name in self.PREFERRED
                     if name.lower() in families), "TkDefaultFont")

        self.base = base
        self.regular = base
        self.medium = self._variant(families, "medium") or base
        self.semi = (self._variant(families, "semibold")
                     or self._variant(families, "semi bold")
                     or base)

    def _variant(self, families: dict, suffix: str) -> str | None:
        return families.get(f"{self.base.lower()} {suffix}")

    def __call__(self, size: int, weight: str = "normal"):
        family = {
            "medium": self.medium,
            "semi": self.semi,
            "light": self.regular,
        }.get(weight, self.regular)
        return (family, size)


# ---------------------------------------------------------------------------
# Конфиг и служебные функции
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S")
    )
    root.addHandler(handler)

    log.setLevel(logging.DEBUG)
    for noisy in ("PIL", "urllib3", "pystray", "asyncio", "sounddevice"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    data = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(text: str) -> str:
    text = (text or "").lower().translate(str.maketrans({"ё": "е"}))
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())


def phrase_score(text: str, phrase: str) -> float:
    from rapidfuzz import fuzz

    a, b = normalize(text), normalize(phrase)
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    return float(max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)))


def best_window_score(tokens: list[str], phrase: str) -> float:
    words = normalize(phrase).split()
    n = len(words)
    if not tokens or not n:
        return 0.0
    best = 0.0
    for size in {max(1, n - 1), n, n + 1}:
        if size > len(tokens):
            continue
        for start in range(len(tokens) - size + 1):
            score = phrase_score(" ".join(tokens[start:start + size]), phrase)
            if score > best:
                best = score
                if best >= 99.9:
                    return best
    return best


def process_running(process_name: str) -> bool:
    """Запущен ли процесс — через Win32 API, без внешних утилит."""
    if not process_name:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot in (-1, 0xFFFFFFFFFFFFFFFF):
            return False

        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        target = process_name.lower()
        try:
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() == target:
                    return True
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return False
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return False


def launch_app(path: str) -> None:
    exe = Path(path)
    if not exe.exists():
        raise FileNotFoundError(f"Не найден файл: {exe}")
    subprocess.Popen(
        [str(exe)], cwd=str(exe.parent), close_fds=True,
        creationflags=CREATE_NO_WINDOW,
    )


def beep() -> None:
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


def desktop_dir() -> Path:
    home = Path.home()
    for candidate in (home / "Desktop", home / "OneDrive" / "Desktop", home / "Рабочий стол"):
        if candidate.exists():
            return candidate
    return home / "Desktop"


def app_icon_path() -> Path:
    """Иконка для ярлыка живёт в профиле пользователя.

    Имя файла содержит хеш содержимого: если картинка изменилась, путь тоже
    изменится, и Windows не подсунет из кэша старую иконку. А из папки проекта
    проводник её вообще читать отказывается.
    """
    try:
        import hashlib

        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VoiceLauncher"
        base.mkdir(parents=True, exist_ok=True)

        source = ensure_icon(force=True)
        digest = hashlib.md5(source.read_bytes()).hexdigest()[:10]
        target = base / f"icon-{digest}.ico"
        if not target.exists():
            shutil.copyfile(source, target)

        for old in base.glob("icon*.ico"):
            if old.name != target.name:
                try:
                    old.unlink()
                except OSError:
                    pass
        return target
    except Exception as exc:
        log.warning("Не удалось подготовить иконку: %s", exc)
        return ensure_icon()


def create_shortcut() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)
    link = desktop_dir() / "Голосовой запуск.lnk"
    icon = app_icon_path()

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{link}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '\"{APP_DIR / 'app.py'}\"'; "
        f"$s.WorkingDirectory = '{APP_DIR}'; "
        f"$s.IconLocation = '{icon},0'; "
        "$s.Description = 'Голосовой запуск по кодовой фразе'; "
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30,
    )
    return link


# ---------------------------------------------------------------------------
# Модель
# ---------------------------------------------------------------------------

def model_ready() -> bool:
    return MODEL_DIR.exists() and (MODEL_DIR / "am").exists()


def download_model(progress=None) -> None:
    import urllib.request

    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    zip_path = APP_DIR / "model.zip"
    last_error: Exception | None = None

    for url in MODEL_URLS:
        try:
            if progress:
                progress(0, "Соединяюсь…")
            request = urllib.request.Request(url, headers={"User-Agent": "voice-launcher"})
            with urllib.request.urlopen(request, timeout=60) as response, \
                    open(zip_path, "wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        percent = (done * 100 / total) if total else 0
                        progress(percent, f"Скачано {done // 1048576} из {max(1, total // 1048576)} МБ")
            if not zipfile.is_zipfile(zip_path):
                raise RuntimeError("файл повреждён")
            if progress:
                progress(100, "Распаковываю…")
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(MODEL_DIR.parent)
            zip_path.unlink(missing_ok=True)
            if model_ready():
                return
            raise RuntimeError("в архиве нет модели")
        except Exception as exc:
            last_error = exc
            if progress:
                progress(0, "Зеркало не ответило, пробую другое…")
            continue

    raise RuntimeError(f"Не удалось скачать модель: {last_error}")


# ---------------------------------------------------------------------------
# Движок распознавания
# ---------------------------------------------------------------------------

class Engine(threading.Thread):
    """Фоновый поток: микрофон → Vosk → оценка похожести на фразу."""

    def __init__(self, cfg: dict, out: "queue.Queue[tuple]"):
        super().__init__(daemon=True, name="voice-engine")
        self.cfg = cfg
        self.out = out

        self.stopped = threading.Event()
        self.paused = threading.Event()
        self.probe_until = 0.0
        self.last_hit_at = 0.0
        self.buffer: list[str] = []

    def stop(self) -> None:
        self.stopped.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self.paused.set()
        else:
            self.paused.clear()

    def start_probe(self, seconds: float = 6.0) -> None:
        self.buffer = []
        self.probe_until = time.time() + seconds
        self.out.put(("status", "probe", f"Слушаю {int(seconds)} с…"))

    def run(self) -> None:
        attempt = 0
        while not self.stopped.is_set():
            try:
                self._listen_once()
                attempt = 0
            except Exception as exc:
                attempt += 1
                log.exception("Сбой слушателя: %s", exc)
                self.out.put(("status", "error", f"Ошибка: {exc}"))
                if self.stopped.is_set():
                    break
                self.stopped.wait(min(30, 3 * attempt))

    def _listen_once(self) -> None:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel

        SetLogLevel(-1)
        self.out.put(("status", "loading", "Загружаю модель…"))
        model = Model(str(MODEL_DIR))
        log.info("Модель загружена")

        rate = int(self.cfg.get("sample_rate", 16000))
        recognizer = KaldiRecognizer(model, rate)

        audio: "queue.Queue[bytes]" = queue.Queue()
        device = self.cfg.get("device")
        stream, src_rate = self._open_stream(sd, rate, device, audio)

        with stream:
            self.out.put(("status", "listening", self._device_label(sd, device)))
            while not self.stopped.is_set():
                try:
                    data = audio.get(timeout=0.4)
                except queue.Empty:
                    continue
                if self.paused.is_set():
                    continue

                if src_rate != rate:
                    data = resample_linear(data, src_rate, rate)

                if recognizer.AcceptWaveform(data):
                    text = json.loads(recognizer.Result()).get("text", "")
                    self._handle(text)

    @staticmethod
    def _open_stream(sd, rate: int, device, audio: "queue.Queue[bytes]"):
        def callback(indata, frames, time_info, status) -> None:
            audio.put(bytes(indata))

        try:
            stream = sd.RawInputStream(
                samplerate=rate, blocksize=4000, device=device,
                dtype="int16", channels=1, callback=callback,
            )
            return stream, rate
        except Exception:
            native = int(sd.query_devices(device, "input")["default_samplerate"])
            stream = sd.RawInputStream(
                samplerate=native, blocksize=4000, device=device,
                dtype="int16", channels=1, callback=callback,
            )
            return stream, native

    @staticmethod
    def _device_label(sd, device) -> str:
        try:
            return sd.query_devices(device, "input")["name"]
        except Exception:
            return "микрофон"

    def _handle(self, text: str) -> None:
        clean = normalize(text)
        if not clean:
            return

        tokens = (self.buffer + clean.split())[-BUFFER_WORDS:]
        self.buffer = tokens

        score = best_window_score(tokens, self.cfg["phrase"])
        probing = time.time() < self.probe_until

        self.out.put(("heard", text, score))

        if probing or score < float(self.cfg.get("threshold", 88.0)):
            return

        now = time.time()
        if now - self.last_hit_at < float(self.cfg.get("cooldown_seconds", 15)):
            return
        self.last_hit_at = now
        self.buffer = []
        log.info("Сработало: %r (%.1f%%)", text, score)
        self.out.put(("hit", text, score))


def resample_linear(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not data:
        return data
    count = len(data) // 2
    src = array.array("h")
    src.frombytes(data[: count * 2])
    out_len = max(1, int(count * dst_rate / src_rate))
    out = array.array("h", bytes(2 * out_len))
    step = src_rate / dst_rate
    for i in range(out_len):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = src[j] if j < count else 0
        b = src[j + 1] if j + 1 < count else a
        out[i] = max(-32768, min(32767, int(a + (b - a) * frac)))
    return out.tobytes()


# ---------------------------------------------------------------------------
# Графика: иконка и картинки состояний
# ---------------------------------------------------------------------------

def _hex_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _draw_mic(draw, size: float, color: tuple) -> None:
    """Микрофон в тех же пропорциях, что и на иконке."""
    c = size / 2
    cap_w, cap_h = 0.145 * size, 0.245 * size
    top = c - 0.165 * size
    bottom = top + cap_h
    draw.rounded_rectangle((c - cap_w / 2, top, c + cap_w / 2, bottom),
                           radius=cap_w / 2, fill=color)

    cradle_r = 0.145 * size
    center_y = top + cap_h * 0.55
    draw.arc((c - cradle_r, center_y - cradle_r, c + cradle_r, center_y + cradle_r),
             start=5, end=175, fill=color, width=max(1, int(0.02 * size)))

    stem_top = center_y + cradle_r
    stem_bottom = stem_top + 0.05 * size
    draw.line((c, stem_top, c, stem_bottom), fill=color, width=max(1, int(0.02 * size)))
    draw.line((c - 0.1 * size, stem_bottom, c + 0.1 * size, stem_bottom),
              fill=color, width=max(1, int(0.02 * size)))


def draw_icon(size: int = 256, circle: str = ACCENT):
    """Иконка приложения: тёмный квадрат, круг и микрофон поверх."""
    from PIL import Image, ImageDraw

    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(0.227 * s), fill=(24, 26, 32, 255))
    c = s / 2
    r = 0.36 * s
    d.ellipse((c - r, c - r, c + r, c + r), fill=_hex_rgb(circle) + (255,))
    _draw_mic(d, s, (11, 18, 12, 255))

    return img.resize((size, size), Image.LANCZOS)


def _dib_entry(image) -> bytes:
    """32-битная BMP-запись для ICO: так Windows-оболочка точно её прочитает."""
    import struct

    from PIL import Image

    width, height = image.size
    rgba = image.convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM)
    pixels = bytearray(rgba.tobytes())
    # RGBA → BGRA: меняем местами красный и синий каналы (ровно один раз!)
    red = bytes(pixels[0::4])
    blue = bytes(pixels[2::4])
    pixels[0::4] = blue
    pixels[2::4] = red
    mask_stride = ((width + 31) // 32) * 4
    mask = b"\x00" * (mask_stride * height)
    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
        len(pixels) + len(mask), 0, 0, 0, 0,
    )
    return header + bytes(pixels) + mask


def save_ico(path: Path, image, sizes=(16, 24, 32, 48, 64, 128, 256)) -> None:
    """Собирает .ico вручную.

    Важно: маленькие размеры должны быть BMP (DIB), а не PNG — иначе
    проводник Windows не показывает такую иконку у ярлыка. PNG оставляем
    только для 256×256, где он допустим и сильно экономит место.
    """
    import io
    import struct

    from PIL import Image

    payloads: list[bytes] = []
    for size in sizes:
        resized = image.resize((size, size), Image.LANCZOS)
        if size >= 256:
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG")
            payloads.append(buffer.getvalue())
        else:
            payloads.append(_dib_entry(resized))

    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for size, blob in zip(sizes, payloads):
        width = 0 if size >= 256 else size
        height = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)

    path.write_bytes(header + entries + b"".join(payloads))


def ensure_icon(force: bool = False) -> Path:
    if force or not ICON_PATH.exists():
        save_ico(ICON_PATH, draw_icon(256))
    return ICON_PATH


def status_image(px: int, color: str):
    """Плашка состояния — тот же знак, что и у иконки приложения."""
    return draw_icon(px, color)


# ---------------------------------------------------------------------------
# Виджеты
# ---------------------------------------------------------------------------

_photo_cache: dict = {}


def rounded_photo(width: int, height: int, radius: int, fill: str,
                  bg: str | None = None, outline: str | None = None,
                  outline_width: int = 1):
    """Скруглённый прямоугольник, отрисованный с суперсэмплингом (без «лесенки»)."""
    from PIL import Image, ImageDraw, ImageTk

    key = (width, height, radius, fill, bg, outline, outline_width)
    if key in _photo_cache:
        return _photo_cache[key]

    ss = 4
    W, H, R = max(1, width * ss), max(1, height * ss), radius * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        (0, 0, W - 1, H - 1), radius=R, fill=fill,
        outline=outline, width=outline_width * ss,
    )
    img = img.resize((width, height), Image.LANCZOS)

    if bg:
        base = Image.new("RGBA", (width, height), bg)
        base.alpha_composite(img)
        img = base

    photo = ImageTk.PhotoImage(img)
    _photo_cache[key] = photo
    return photo


class RoundButton:
    """Кнопка со скруглением, отрисованным картинкой (ровные края)."""

    def __init__(self, master, text, command=None, width=150, height=38,
                 fill=ACCENT, text_color=ACCENT_INK, bg=CARD, font=None, radius=12):
        import tkinter as tk

        self.tk = tk
        self.canvas = tk.Canvas(master, width=width, height=height, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.fill = fill
        self.hover = self._shade(fill, 1.10)
        self.press = self._shade(fill, 0.92)
        self.text_color = text_color
        self.font = font
        self.radius = radius
        self.enabled = True

        self._normal = rounded_photo(width, height, radius, fill, bg=bg)
        self._hover = rounded_photo(width, height, radius, self.hover, bg=bg)
        self._pressed = rounded_photo(width, height, radius, self.press, bg=bg)
        self._img = self.canvas.create_image(0, 0, anchor="nw", image=self._normal)
        self._label = self.canvas.create_text(width / 2, height / 2, text=text,
                                              fill=text_color, font=font)

        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Enter>", lambda _e: self._show(self._hover))
        self.canvas.bind("<Leave>", lambda _e: self._show(self._normal))

    @staticmethod
    def _shade(color: str, factor: float) -> str:
        r, g, b = _hex_rgb(color)
        clamp = lambda v: max(0, min(255, int(v * factor)))  # noqa: E731
        return f"#{clamp(r):02x}{clamp(g):02x}{clamp(b):02x}"

    def _show(self, photo) -> None:
        if self.enabled:
            self.canvas.itemconfigure(self._img, image=photo)

    def _on_press(self, _event=None) -> None:
        if self.enabled:
            self.canvas.itemconfigure(self._img, image=self._pressed)

    def _on_release(self, _event=None) -> None:
        if self.enabled:
            self.canvas.itemconfigure(self._img, image=self._hover)
            if self.command:
                self.command()

    def configure(self, text: str | None = None, fill: str | None = None,
                  text_color: str | None = None) -> None:
        if text is not None:
            self.canvas.itemconfigure(self._label, text=text)
        if text_color is not None:
            self.canvas.itemconfigure(self._label, fill=text_color)
        if fill is not None:
            self.fill = fill
            self.hover = self._shade(fill, 1.10)
            self.press = self._shade(fill, 0.92)
            self._rebuild()

    def _rebuild(self) -> None:
        width = int(self.canvas.cget("width"))
        height = int(self.canvas.cget("height"))
        bg = self.canvas.cget("bg")
        self._normal = rounded_photo(width, height, self.radius, self.fill, bg=bg)
        self._hover = rounded_photo(width, height, self.radius, self.hover, bg=bg)
        self._pressed = rounded_photo(width, height, self.radius, self.press, bg=bg)
        self.canvas.itemconfigure(self._img, image=self._normal)

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)
        return self

    def grid(self, **kwargs):
        self.canvas.grid(**kwargs)
        return self


class RoundEntry:
    """Поле ввода со скруглёнными краями, как у кнопок."""

    def __init__(self, master, variable, height=38, radius=12, bg=BG,
                 fill=FIELD, text_color=TEXT, font=None, pad=14, border=BORDER):
        import tkinter as tk

        self.canvas = tk.Canvas(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self.height = height
        self.radius = radius
        self.bg = bg
        self.fill = fill
        self.pad = pad
        self.border = border
        self._width = 240

        self._normal = rounded_photo(self._width, height, radius, fill, bg=bg,
                                     outline=border, outline_width=1)
        self._focus = rounded_photo(self._width, height, radius, fill, bg=bg,
                                    outline=ACCENT, outline_width=1)
        self._img = self.canvas.create_image(0, 0, anchor="nw", image=self._normal)

        self.entry = tk.Entry(
            self.canvas, textvariable=variable, bd=0, relief="flat",
            bg=fill, fg=text_color, insertbackground=text_color, font=font,
            highlightthickness=0, selectbackground=ACCENT, selectforeground=ACCENT_INK,
        )
        self._win = self.canvas.create_window(
            pad, height // 2, anchor="w", window=self.entry, height=height - 12
        )

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False))

        # длинный путь показываем с конца — важнее имя файла
        if variable.get():
            self.entry.xview_moveto(1.0)

    def _set_focus(self, focused: bool) -> None:
        self.canvas.itemconfigure(self._img, image=self._focus if focused else self._normal)

    def _on_configure(self, event) -> None:
        width = max(60, event.width)
        if width == self._width:
            return
        self._width = width
        self._normal = rounded_photo(width, self.height, self.radius, self.fill,
                                     bg=self.bg, outline=self.border, outline_width=1)
        self._focus = rounded_photo(width, self.height, self.radius, self.fill,
                                    bg=self.bg, outline=ACCENT, outline_width=1)
        self.canvas.itemconfigure(self._img, image=self._normal)
        self.canvas.itemconfigure(self._win, width=width - 2 * self.pad)

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)
        return self


class MicView:
    """Две статичные картинки: слушаю / на паузе."""

    def __init__(self, master, size: int, bg: str = CARD):
        import tkinter as tk
        from PIL import ImageTk

        self.size = size
        self.active = ImageTk.PhotoImage(status_image(size, ACCENT))
        self.idle = ImageTk.PhotoImage(status_image(size, "#5b6270"))
        self.label = tk.Label(master, image=self.active, bg=bg, bd=0, highlightthickness=0)

    def set_active(self, active: bool) -> None:
        self.label.configure(image=self.active if active else self.idle)

    def pack(self, **kwargs):
        self.label.pack(**kwargs)
        return self


class RoundCard:
    """Панель со скруглёнными углами: картинка-фон + содержимое поверх."""

    def __init__(self, master, bg=BG, fill=CARD, radius=14, padding=16):
        import tkinter as tk

        self.frame = tk.Frame(master, bg=bg)
        self.bg = bg
        self.fill = fill
        self.radius = radius
        self.padding = padding
        self._job = None
        self._photo = None

        self.canvas = tk.Canvas(self.frame, bg=bg, highlightthickness=0, bd=0,
                                height=padding * 2 + 10)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=fill)
        self._win = self.canvas.create_window(padding, padding, anchor="nw", window=self.body)
        self._img = self.canvas.create_image(0, 0, anchor="nw")

        self.canvas.bind("<Configure>", self._schedule)
        self.body.bind("<Configure>", self._schedule)

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)
        return self

    def _schedule(self, _event=None) -> None:
        if self._job:
            try:
                self.frame.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.frame.after(8, self._sync)

    def _sync(self) -> None:
        self._job = None
        width = self.canvas.winfo_width()
        if width <= 1:
            return
        inner = width - 2 * self.padding
        if inner > 1:
            self.canvas.itemconfigure(self._win, width=inner)

        height = self.body.winfo_reqheight() + 2 * self.padding
        if int(self.canvas.cget("height")) != height:
            self.canvas.configure(height=height)

        self.frame.update_idletasks()
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 10 or h < 10:
            return
        self._photo = rounded_photo(w, h, self.radius, self.fill, bg=self.bg)
        self.canvas.itemconfigure(self._img, image=self._photo)
        self.canvas.tag_lower(self._img)

    def finalize(self) -> None:
        self.frame.update_idletasks()
        self._sync()


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------

class App:
    def __init__(self) -> None:
        import tkinter as tk

        self.tk = tk
        self.cfg = load_config()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.commands: "queue.Queue[str]" = queue.Queue()
        self.engine: Engine | None = None
        self.listening = False
        self._save_job = None
        self._was_paused = False
        self.tray = None
        self._hidden_hint_shown = False
        self._placeholder = True
        self._preset = next(
            (name for name, value in THRESHOLD_PRESETS.items()
             if abs(value - float(self.cfg.get("threshold", 88))) < 0.01),
            "Обычно",
        )

        self.root = tk.Tk()
        self.root.title("Голосовой запуск")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        self.dpi = self.root.winfo_fpixels("1i")
        self.scale = max(1.0, self.dpi / 96.0)
        self.root.tk.call("tk", "scaling", self.dpi / 72.0)
        self.fonts = Fonts(self.root)

        try:
            self.root.iconbitmap(default=str(ensure_icon()))
        except Exception:
            pass

        self._build()
        self.status_card.finalize()
        self.log_card.finalize()
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        self.root.geometry(f"{width}x{height}")

        # Скруглённые панели узнают свою высоту только после появления окна,
        # поэтому подгоняем размер ещё раз, когда раскладка устоялась.
        self.root.after(40, self._fit_window)
        self.root.after(260, self._fit_window)

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.after(80, self._poll)
        self.root.after(250, self.start_listening)
        self.root.after(600, self._start_tray)

        # Первый запуск (конфига ещё нет) — сразу делаем ярлык, чтобы
        # у скачавшего приложение было под рукой.
        if not CONFIG_PATH.exists():
            self.root.after(1500, self._create_shortcut_on_first_run)

    def _create_shortcut_on_first_run(self) -> None:
        def worker() -> None:
            try:
                link = create_shortcut()
                log.info("Ярлык создан при первом запуске: %s", link)
                self.events.put(("shortcut", str(link)))
            except Exception as exc:
                log.warning("Ярлык не создан: %s", exc)

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------- размеры и шрифты ----------------------

    def s(self, value: float) -> int:
        return int(round(value * self.scale))

    def _fit_window(self) -> None:
        """Подгоняет окно под содержимое после того, как оно отрисовалось."""
        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 200)
        height = max(self.root.winfo_reqheight(), 200)
        if (width, height) != (self.root.winfo_width(), self.root.winfo_height()):
            self.root.geometry(f"{width}x{height}")

    # ---------------------- сборка интерфейса ----------------------

    def _build(self) -> None:
        tk = self.tk
        s = self.s
        f = self.fonts

        outer = tk.Frame(self.root, bg=BG, padx=s(22), pady=s(18))
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text="Голосовой запуск", bg=BG, fg=TEXT,
                 font=f(19, "semi")).pack(side="left")
        self.badge = tk.Label(header, text="● слушаю", bg=BG, fg=ACCENT, font=f(10, "light"))
        self.badge.pack(side="right")

        card = RoundCard(outer, bg=BG, fill=CARD, radius=s(14), padding=s(16))
        card.pack(fill="x", pady=(s(16), 0))
        self.status_card = card
        body = card.body

        self.mic = MicView(body, size=s(84), bg=CARD)
        self.mic.pack(side="left")

        info = tk.Frame(body, bg=CARD)
        info.pack(side="left", fill="x", expand=True, padx=(s(14), 0))
        self.status_title = tk.Label(info, text="Запускаюсь…", bg=CARD, fg=TEXT,
                                     font=f(16, "semi"), anchor="w")
        self.status_title.pack(fill="x")
        self.status_hint = tk.Label(info, text="Секунду", bg=CARD, fg=MUTED,
                                    font=f(10, "light"), anchor="w", justify="left")
        self.status_hint.pack(fill="x", pady=(s(4), 0))

        fields = tk.Frame(outer, bg=BG)
        fields.pack(fill="x", pady=(s(18), 0))

        tk.Label(fields, text="Программа или игра", bg=BG, fg=MUTED,
                 font=f(10, "light")).pack(anchor="w")
        row = tk.Frame(fields, bg=BG)
        row.pack(fill="x", pady=(s(5), 0))
        self.path_var = tk.StringVar(value=self.cfg["app_path"])
        self.path_entry = RoundEntry(row, self.path_var, height=s(38), radius=s(12),
                                     bg=BG, fill=FIELD, font=f(10))
        self.path_entry.pack(side="left", fill="x", expand=True)
        RoundButton(row, "Обзор", width=s(84), height=s(38), radius=s(12),
                    fill=CARD_HI, text_color=TEXT, bg=BG, font=f(10, "light"),
                    command=self.browse).pack(side="left", padx=(s(8), 0))

        tk.Label(fields, text="Кодовая фраза", bg=BG, fg=MUTED,
                 font=f(10, "light")).pack(anchor="w", pady=(s(12), 0))
        self.phrase_var = tk.StringVar(value=self.cfg["phrase"])
        self.phrase_entry = RoundEntry(fields, self.phrase_var, height=s(38),
                                       radius=s(12), bg=BG, fill=FIELD, font=f(10))
        self.phrase_entry.pack(fill="x", pady=(s(5), 0))

        tk.Label(fields, text="Чувствительность", bg=BG, fg=MUTED,
                 font=f(10, "light")).pack(anchor="w", pady=(s(12), 0))
        pills = tk.Frame(fields, bg=BG)
        pills.pack(anchor="w", pady=(s(6), 0))
        self.pills: dict[str, RoundButton] = {}
        for name in THRESHOLD_PRESETS:
            button = RoundButton(
                pills, name, width=s(100), height=s(34), radius=s(10),
                fill=CARD, text_color=MUTED, bg=BG, font=f(10, "light"),
                command=lambda n=name: self.set_preset(n),
            )
            button.pack(side="left", padx=(0, s(8)))
            self.pills[name] = button

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(s(20), 0))
        self.toggle_btn = RoundButton(actions, "Пауза", width=s(150), height=s(44),
                                      fill=ACCENT, bg=BG, font=f(11, "semi"),
                                      radius=s(13), command=self.toggle_listening)
        self.toggle_btn.pack(side="left")
        RoundButton(actions, "Проверить фразу", width=s(180), height=s(44),
                    fill=CARD_HI, text_color=TEXT, bg=BG, font=f(11, "light"),
                    radius=s(13), command=self.probe).pack(side="left", padx=(s(8), 0))
        RoundButton(actions, "Запустить", width=s(150), height=s(44),
                    fill=CARD_HI, text_color=TEXT, bg=BG, font=f(11, "light"),
                    radius=s(13), command=self.launch_now).pack(side="left", padx=(s(8), 0))

        log_card = RoundCard(outer, bg=BG, fill=CARD, radius=s(14), padding=s(14))
        log_card.pack(fill="x", pady=(s(18), 0))
        self.log_card = log_card
        inner = log_card.body
        tk.Label(inner, text="Что слышно", bg=CARD, fg=MUTED,
                 font=f(9, "light")).pack(anchor="w")
        self.log_text = tk.Text(inner, height=4, bg=CARD, fg=TEXT,
                                font=f(10, "light"), wrap="word", bd=0,
                                highlightthickness=0, insertbackground=CARD,
                                state="disabled", padx=0, pady=s(4))
        self.log_text.pack(fill="both", expand=True, pady=(s(2), 0))
        self.log_text.tag_configure("hit", foreground=ACCENT)
        self.log_text.tag_configure("dim", foreground=MUTED)
        self.log_text.tag_configure("err", foreground=RED)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "Пока тихо. Скажи фразу — здесь появится, "
                                    "что услышал распознаватель.\n", "dim")
        self.log_text.configure(state="disabled")

        tk.Label(outer, text="Крестик сворачивает в трей — микрофон продолжает слушать",
                 bg=BG, fg="#5d626c", font=f(9, "light")).pack(pady=(s(12), 0))

        self.path_var.trace_add("write", lambda *_a: self._schedule_save())
        self.phrase_var.trace_add("write", lambda *_a: self._schedule_save())

        self._sync_pills()
        self.status_hint.configure(text=self._phrase_hint())

    # ---------------------- настройки ----------------------

    def _schedule_save(self) -> None:
        self._apply_settings(save=False)
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(500, lambda: self._apply_settings(save=True))

    def _apply_settings(self, save: bool = True) -> None:
        self.cfg["app_path"] = self.path_var.get().strip()
        self.cfg["phrase"] = self.phrase_var.get().strip()
        self.cfg["threshold"] = THRESHOLD_PRESETS.get(self.current_preset, 88.0)
        self.cfg["process_name"] = Path(self.cfg["app_path"]).name or self.cfg["process_name"]
        if save:
            save_config(self.cfg)
        if self.engine:
            self.engine.cfg = self.cfg
        self._update_hint()

    @property
    def current_preset(self) -> str:
        return getattr(self, "_preset", "Обычно")

    def set_preset(self, name: str) -> None:
        self._preset = name
        self._sync_pills()
        self._apply_settings()

    def _sync_pills(self) -> None:
        for name, pill in self.pills.items():
            selected = name == self.current_preset
            pill.configure(fill=ACCENT if selected else CARD,
                           text_color=ACCENT_INK if selected else MUTED)

    def _update_hint(self) -> None:
        self.status_hint.configure(text=self._phrase_hint(), fg=MUTED)

    def _phrase_hint(self) -> str:
        phrase = self.phrase_var.get().strip() or "фразу"
        name = Path(self.cfg.get("app_path", "")).stem or "программу"
        return f"Скажи «{phrase}» — запущу {name}"

    def browse(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="Выбери exe программы",
            filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")],
        )
        if path:
            self.path_var.set(path)

    # ---------------------- состояние ----------------------

    def set_status(self, title: str, hint: str | None = None,
                   color: str = ACCENT, badge: str | None = None) -> None:
        self.status_title.configure(text=title, fg=TEXT)
        if hint is not None:
            self.status_hint.configure(text=hint, fg=MUTED)
        self.mic.set_active(self.listening)
        self.badge.configure(text=badge or ("● слушаю" if self.listening else "● пауза"),
                             fg=color)

    # ---------------------- слушатель ----------------------

    def start_listening(self) -> None:
        if not model_ready():
            if not self.ask_download_model():
                self.set_status("Модель не загружена", "Нужна модель распознавания",
                                YELLOW, "● нет модели")
                return
            return

        self.engine = Engine(self.cfg, self.events)
        self.engine.start()
        self.listening = True
        self.toggle_btn.configure(text="Пауза", fill=ACCENT)
        self.mic.set_active(True)

    def toggle_listening(self) -> None:
        if not self.engine:
            self.start_listening()
            return
        self.listening = not self.listening
        self.engine.set_paused(not self.listening)
        self.toggle_btn.configure(text="Пауза" if self.listening else "Слушать",
                                  fill=ACCENT if self.listening else CARD_HI)
        if not self.listening:
            self.set_status("Пауза", "Микрофон не слушаю", MUTED, "● пауза")
        else:
            self.set_status("Слушаю микрофон", self._phrase_hint(), ACCENT)

    def probe(self) -> None:
        if not self.engine:
            self.append_log("Модель ещё не готова", "dim")
            return
        self._was_paused = not self.listening
        self.engine.set_paused(False)
        self.engine.start_probe(6.0)
        self.root.after(6800, self._probe_done)

    def _probe_done(self) -> None:
        if self._was_paused and self.engine:
            self.engine.set_paused(True)
            self.set_status("Пауза", "Микрофон не слушаю", MUTED, "● пауза")
        elif self.listening:
            self.set_status("Слушаю микрофон", self._phrase_hint(), ACCENT)

    def launch_now(self) -> None:
        self._apply_settings()
        try:
            if process_running(self.cfg["process_name"]):
                self.append_log("Уже запущено — второй раз не открываю", "dim")
                return
            launch_app(self.cfg["app_path"])
            self.append_log("Запущено вручную", "hit")
        except Exception as exc:
            self.append_log(f"Не вышло: {exc}", "err")

    # ---------------------- события ----------------------

    def _poll(self) -> None:
        try:
            while True:
                self._run_command(self.commands.get_nowait())
        except queue.Empty:
            pass

        try:
            while True:
                self._on_event(self.events.get_nowait())
        except queue.Empty:
            pass

        self.root.after(80, self._poll)

    def _run_command(self, kind: str) -> None:
        if kind == "show":
            self.show_window()
        elif kind == "toggle":
            self.toggle_listening()
        elif kind == "launch":
            self.launch_now()
        elif kind == "shortcut":
            self.make_shortcut()
        elif kind == "quit":
            self.quit()

    def _on_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "status":
            _, state, detail = event
            if state == "listening":
                self.set_status("Слушаю микрофон", self._phrase_hint(), ACCENT)
            elif state == "loading":
                self.set_status("Загружаю модель…", "Один раз при запуске",
                                YELLOW, "● загрузка")
            elif state == "error":
                self.set_status("Ошибка", detail, RED, "● ошибка")
                self.append_log(detail, "err")
            elif state == "probe":
                self.set_status("Слушаю…", f"Скажи фразу ({detail})", BLUE, "● проверка")
        elif kind == "heard":
            _, text, score = event
            threshold = float(self.cfg.get("threshold", 88))
            tag = "hit" if score >= threshold else "dim"
            mark = "✓" if score >= threshold else "·"
            self.append_log(f"{mark} {text}  ({score:.0f}%)", tag)
        elif kind == "hit":
            _, text, score = event
            self.on_hit(text, score)
        elif kind == "tray_failed":
            self.tray = None
            self.append_log("Трей недоступен — закрытие окна завершит работу", "err")
        elif kind == "shortcut":
            self.append_log(f"Ярлык на рабочем столе: {Path(event[1]).name}", "dim")

    def on_hit(self, text: str, score: float) -> None:
        self._apply_settings()
        self.append_log(f"🎯 «{text}» — запускаю", "hit")
        if self.cfg.get("beep", True):
            beep()
        try:
            if process_running(self.cfg["process_name"]):
                self.append_log("Уже запущено — пропускаю", "dim")
                return
            launch_app(self.cfg["app_path"])
            self.set_status("Запущено!", f"Фраза «{self.cfg['phrase']}»", ACCENT)
        except Exception as exc:
            self.append_log(f"Не вышло: {exc}", "err")

    def append_log(self, line: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        if self._placeholder:
            self.log_text.delete("1.0", "end")
            self._placeholder = False
        self.log_text.insert("end", f"{time.strftime('%H:%M:%S')}  {line}\n", tag)
        self.log_text.see("end")
        if int(self.log_text.index("end-1c").split(".")[0]) > 120:
            self.log_text.delete("1.0", "30.0")
        self.log_text.configure(state="disabled")

    # ---------------------- трей ----------------------

    def _start_tray(self) -> None:
        try:
            self.tray = Tray(self)
            self.tray.start()
            log.info("Трей запущен")
        except Exception as exc:
            log.warning("Трей недоступен: %s", exc)
            self.tray = None

    def post(self, command: str) -> None:
        self.commands.put(command)

    def hide_to_tray(self) -> None:
        ready = bool(self.tray) and not self.tray.failed and self.tray.icon.visible
        if not ready:
            from tkinter import messagebox

            if messagebox.askyesno(
                "Свернуть некуда",
                "Иконка в трее недоступна.\nЗакрыть приложение?",
                parent=self.root,
            ):
                self.quit()
            return

        self.root.withdraw()
        if not self._hidden_hint_shown:
            self._hidden_hint_shown = True
            self.tray.notify("Приложение работает в трее", "Голосовой запуск")

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def make_shortcut(self) -> None:
        try:
            link = create_shortcut()
            self.append_log(f"Ярлык создан: {link.name}", "hit")
        except Exception as exc:
            self.append_log(f"Ярлык не создан: {exc}", "err")

    # ---------------------- загрузка модели ----------------------

    def ask_download_model(self) -> bool:
        from tkinter import messagebox

        if not messagebox.askyesno(
            "Нужна модель",
            "Для распознавания речи нужна модель Vosk (~45 МБ).\n"
            "Скачать её сейчас? Нужен интернет.",
            parent=self.root,
        ):
            return False
        self._download_dialog()
        return False

    def _download_dialog(self) -> None:
        from tkinter import messagebox
        import tkinter as tk

        s, f = self.s, self.fonts
        win = tk.Toplevel(self.root)
        win.title("Загрузка модели")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        label = tk.Label(win, text="Соединяюсь…", bg=BG, fg=TEXT, font=f(10, "light"))
        label.pack(padx=s(22), pady=(s(20), s(12)))
        bar = tk.Canvas(win, width=s(340), height=s(8), bg=CARD, highlightthickness=0)
        bar.pack(padx=s(22), pady=(0, s(22)))
        fill = bar.create_rectangle(0, 0, 0, s(8), fill=ACCENT, outline="")

        progress_queue: "queue.Queue[tuple]" = queue.Queue()

        def worker() -> None:
            try:
                download_model(lambda pct, text: progress_queue.put(("progress", pct, text)))
                progress_queue.put(("done", 100, ""))
            except Exception as exc:
                progress_queue.put(("error", 0, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

        def tick() -> None:
            try:
                while True:
                    kind, pct, text = progress_queue.get_nowait()
                    if kind == "progress":
                        bar.coords(fill, 0, 0, s(340) * pct / 100, s(8))
                        label.configure(text=text)
                    elif kind == "done":
                        win.destroy()
                        self.append_log("Модель загружена", "hit")
                        self.start_listening()
                        return
                    elif kind == "error":
                        win.destroy()
                        messagebox.showerror("Не вышло", text, parent=self.root)
                        return
            except queue.Empty:
                pass
            win.after(120, tick)

        tick()

    # ---------------------- выход ----------------------

    def quit(self) -> None:
        self._apply_settings()
        if self.engine:
            self.engine.stop()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Трей
# ---------------------------------------------------------------------------

class Tray:
    def __init__(self, app: App):
        import pystray

        self.app = app
        self.pystray = pystray
        self.failed = False
        self.thread: threading.Thread | None = None
        self.icon = self._create_icon()

    def _create_icon(self):
        return self.pystray.Icon(
            "voice-launcher",
            draw_icon(64),
            "Голосовой запуск",
            self._menu(),
        )

    @staticmethod
    def _patch_message_filter() -> bool:
        """В урезанных сессиях Windows падает регистрация фильтра сообщений."""
        try:
            import pystray._util.win32 as win32_util

            win32_util.ChangeWindowMessageFilterEx = lambda *args, **kwargs: True
            return True
        except Exception:
            return False

    def _menu(self):
        pystray = self.pystray
        return pystray.Menu(
            pystray.MenuItem("Открыть окно", lambda: self.app.post("show"), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Слушать / пауза", lambda: self.app.post("toggle")),
            pystray.MenuItem("Запустить сейчас", lambda: self.app.post("launch")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ярлык на рабочий стол", lambda: self.app.post("shortcut")),
            pystray.MenuItem("Выход", lambda: self.app.post("quit")),
        )

    def start(self) -> None:
        def runner() -> None:
            try:
                self.icon.run()
                return
            except Exception as exc:
                log.warning("Трей не поднялся: %s", exc)

            if not self._patch_message_filter():
                self.failed = True
                self.app.post("tray_failed")
                return

            try:
                self.icon = self._create_icon()
                self.icon.run()
            except Exception as exc:
                log.warning("Трей не поднялся и со второй попытки: %s", exc)
                self.failed = True
                self.app.post("tray_failed")

        self.thread = threading.Thread(target=runner, daemon=True, name="tray")
        self.thread.start()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass

    def notify(self, message: str, title: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            pass


def main() -> int:
    enable_dpi_awareness()
    setup_logging()
    ensure_icon(force=True)
    try:
        App().run()
    except Exception:
        log.exception("Падение")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
