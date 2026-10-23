#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地离线语音输入 —— 单文件版（Qwen3-ASR + Qwen3.5 润色，llama.cpp 本地推理）。

按住热键说话，松开自动识别 + 润色 + 粘贴到光标处。声音不出本机。

    python voice_input.py                 启动（模型服务由本程序托管）
    python voice_input.py --setup         一次性准备：依赖 + llama.cpp + 模型
    python voice_input.py --test a.wav    不用麦克风，测整条链路
    python voice_input.py --make-icon i.ico

配置见同目录 config.toml；模型放 models/，llama.cpp 放 llama/。
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import io
import json
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import wave
import zipfile
from collections import deque
from ctypes import wintypes as wt
from pathlib import Path

import numpy as np
import pyperclip
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pynput import keyboard as kb

try:
    import skia
except Exception:          # 没装就降级到 Pillow 渲染
    skia = None

try:
    import pystray
except Exception:          # 没装就没有托盘菜单
    pystray = None


def app_dir() -> Path:
    """脚本目录；PyInstaller 打包后是 exe 所在目录（配置/模型/日志都放那里）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
CFG_PATH = ROOT / "config.toml"


# ==============================================================================
#  1. 配置 / 录音 / HTTP / ASR / 润色 / 上屏
# ==============================================================================


import base64
import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np
import pyperclip
import sounddevice as sd
from pynput import keyboard as kb



# pythonw（开机自启）没有控制台，stdout 是 None，print 会炸 → 写日志文件
if sys.stdout is None:
    _log_path = ROOT / "client.log"
    try:                                   # 超过 5MB 轮转，避免无限增长
        if _log_path.exists() and _log_path.stat().st_size > 5 * 1024 * 1024:
            _log_path.replace(ROOT / "client.log.1")
    except Exception:
        pass
    _early_log = open(_log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _early_log
    sys.stderr = _early_log

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 配置


def load_cfg() -> dict:
    path = Path(os.environ.get("VOICE_INPUT_CONFIG") or CFG_PATH)
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve_path(p) -> Path:
    """相对路径按项目根目录解析；绝对路径原样返回。"""
    p = Path(p).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def notify(title: str, msg: str, error: bool = False) -> None:
    """控制台 + 弹窗双通道提示（pythonw / 打包后没有控制台，只能靠弹窗）。"""
    try:
        print(f"[{'fatal' if error else 'info'}] {title}: {msg}")
        sys.stdout.flush()
    except Exception:
        pass
    if os.environ.get("VOICE_INPUT_NO_GUI"):   # 自动化测试时关掉弹窗
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, str(msg), str(title), 0x10 if error else 0x40)
    except Exception:
        pass


VALID_HOTKEYS = ("caps_lock", "scroll_lock", "pause", "f9")


def prepare_cfg(cfg: dict, manage_servers: bool = True) -> list[str]:
    """启动前把配置修成能跑的状态，返回警告列表；致命问题抛 SystemExit。"""
    warns: list[str] = []
    hk = str(cfg.setdefault("hotkey", {}).get("key") or "caps_lock").lower()
    if hk not in VALID_HOTKEYS:
        cfg["hotkey"]["key"] = "caps_lock"
        warns.append(f'热键 "{hk}" 不支持，已回退到 caps_lock')
    if not manage_servers:
        return warns
    L = cfg.get("llama", {})
    asr = resolve_path(L.get("asr_model", ""))
    mm = resolve_path(L.get("asr_mmproj", ""))
    missing = [q for q in (asr, mm) if not q.exists()]
    if missing:
        raise SystemExit(
            "缺少识别模型：\n  " + "\n  ".join(str(q) for q in missing)
            + "\n\n先运行: python voice_input.py --setup")
    pol = resolve_path(L.get("polish_model", ""))
    if cfg.get("polish", {}).get("enabled", True) and not pol.exists():
        cfg["polish"]["enabled"] = False
        warns.append(f"没有润色模型 {pol.name}，已自动关闭润色（只用识别原文）")
    return warns


# ---------------------------------------------------------------- 录音


class Recorder:
    def __init__(self, sr: int, device=None, blocksize: int = 1024, meter=None,
                 max_seconds: float = 150.0):
        self.meter = meter
        self.max_samples = int(max_seconds * sr)
        self.overflow = False
        self._captured = 0
        self.target_sr = sr
        self.capture_sr = sr
        self.device = device or None
        self.blocksize = blocksize
        self._chunks: list[np.ndarray] = []
        self._stream = None

    def start(self):
        self._chunks = []
        self.overflow = False
        self._captured = 0
        try:
            self._open(self.target_sr)
        except Exception:
            # 设备不支持 16k 就用它自己的采样率，录完再重采样
            info = sd.query_devices(self.device, kind="input")
            self._open(int(info["default_samplerate"]))
        self._stream.start()

    def _open(self, sr: int):
        self.capture_sr = sr
        self._stream = sd.InputStream(
            samplerate=sr,
            channels=1,
            dtype="int16",
            device=self.device,
            blocksize=self.blocksize,
            callback=self._cb,
        )

    def _cb(self, indata, frames, time_info, status):
        if self.overflow:          # 到达上限：继续读但不保存，避免卡住音频流
            return
        self._chunks.append(indata.copy())
        self._captured += frames
        if self.meter is not None:
            self.meter.push(float(np.abs(indata).max()) / 32768.0)
        if self.max_samples and self._captured >= self.max_samples:
            self.overflow = True
            print(f"[warn] 录音达到 {self.max_samples / self.target_sr:.0f} 秒上限，后面的不再记录")

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:       # 设备被拔掉 / 驱动异常
                print(f"[warn] 关闭音频流失败: {e}")
            finally:
                self._stream = None
        if not self._chunks:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(self._chunks, axis=0).reshape(-1)
        if self.capture_sr != self.target_sr and audio.size:
            n = int(len(audio) * self.target_sr / self.capture_sr)
            x = np.arange(n) * (self.capture_sr / self.target_sr)
            audio = np.interp(x, np.arange(len(audio)), audio).astype(np.int16)
        return audio


def trim_silence(audio: np.ndarray, sr: int, min_rms: float, pad_ms: int) -> np.ndarray:
    """掐掉首尾静音，中间不动。"""
    if audio.size == 0:
        return audio
    x = audio.astype(np.float32) / 32768.0
    win = max(1, int(sr * 0.02))
    n = len(x) // win
    if n == 0:
        return audio
    rms = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    idx = np.where(rms > min_rms)[0]
    if idx.size == 0:
        return audio[:0]
    pad = int(sr * pad_ms / 1000)
    s = max(0, idx[0] * win - pad)
    e = min(len(x), (idx[-1] + 1) * win + pad)
    return audio[s:e]


def to_wav(audio: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- HTTP


def post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _content_text(resp: dict) -> str:
    msg = resp["choices"][0]["message"]
    c = msg.get("content")
    if isinstance(c, list):  # 少数情况返回分段
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return (c or "").strip()


# ---------------------------------------------------------------- ASR


def transcribe(wav_bytes: bytes, cfg: dict) -> str:
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    user_content = [
        {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}
    ]
    lang = (cfg["asr"].get("language") or "").strip()
    if lang:
        user_content.append({"type": "text", "text": lang})

    payload = {
        "messages": [{"role": "user", "content": user_content}],
        "temperature": 0.0,
        "max_tokens": 2048,
        "stream": False,
    }
    resp = post_json(cfg["asr"]["url"], payload, cfg["asr"]["timeout"])
    return clean_asr(_content_text(resp))


def clean_asr(raw: str) -> str:
    """Qwen3-ASR 会输出 "language Chinese<asr_text>正文</asr_text>"，把包装剥掉。"""
    m = re.search(r"<asr_text>(.*?)(?:</asr_text>|$)", raw, re.S)
    text = m.group(1) if m else raw
    text = text.replace("</asr_text>", "")
    text = re.sub(r"^\s*language\s+\S+\s*", "", text, flags=re.I)
    return text.strip()


# ---------------------------------------------------------------- 润色

POLISH_SYSTEM = """你是一个语音输入的"文字整理器"。用户给你一段语音识别的原始文本。

你的输出要求：
1. 修正并补全标点，断句符合中文习惯，句子该停就停。
2. 删除口水词和重复："嗯、呃、那个、就是说、然后然后、这个这个"等。
3. 修正明显的同音字、别字、漏字错误（结合上下文判断）。
4. 严格保留原意和原语气：不扩写、不总结、不解释、不评论、不改人称。
5. 原话是提问就保留问号；原文是列举就保持列举。
6. 中英混合说话时，英文术语保持英文原样（如 CUDA、API、Git）。
7. 只输出整理后的最终文字本身，不要加引号、不要加"整理后："之类的前缀。

如果原文已经通顺，就原样输出，不要为了显得用心而改写。"""


def system_prompt(cfg: dict) -> str:
    """基础规则 + config 里的额外规则。"""
    extra = cfg.get("polish", {}).get("extra_rules") or []
    if isinstance(extra, str):
        extra = [extra]
    extra = [e.strip() for e in extra if e and e.strip()]
    if not extra:
        return POLISH_SYSTEM
    lines = [f"{7 + i}. {rule}" for i, rule in enumerate(extra, start=1)]
    return POLISH_SYSTEM + "\n附加规则：\n" + "\n".join(lines)


_MATH_GLYPHS = "0-9A-Za-z\u0370-\u03ff\u00b2\u00b3\u00b9\u2070-\u209f\u221a\u221e"
_MATH_OPS = "+-\u00d7\u00f7=\u2248\u2260\u2264\u2265\u00b1<>"
_LATEX_MAP = {
    r"\times": "\u00d7", r"\div": "\u00f7", r"\pm": "\u00b1",
    r"\leq": "\u2264", r"\le": "\u2264", r"\geq": "\u2265", r"\ge": "\u2265",
    r"\neq": "\u2260", r"\ne": "\u2260", r"\approx": "\u2248",
    r"\infty": "\u221e", r"\sqrt": "\u221a",
    r"\alpha": "\u03b1", r"\beta": "\u03b2", r"\gamma": "\u03b3",
    r"\delta": "\u03b4", r"\theta": "\u03b8", r"\lambda": "\u03bb",
    r"\mu": "\u03bc", r"\sigma": "\u03c3", r"\omega": "\u03c9", r"\pi": "\u03c0",
}
_SUP = str.maketrans("0123456789n", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207f")


def tidy_math(text: str) -> str:
    """清掉模型输出里的 $ / LaTeX 残留，并把公式排紧（纯排版，不改字）。"""
    out = text.replace("$", "").replace("\\(", "").replace("\\)", "")
    # 口语里的“派/圆周率”只有紧挨算式（约等于/等于/=）时才换成 π，避免误伤“派对”
    out = re.sub(r"(?:圆周率|派)(?=\s*(?:≈|=|约等于|等于|的值|大约))", "π", out)
    out = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"\1/\2", out)
    for k, v in _LATEX_MAP.items():
        out = re.sub(re.escape(k) + r"(?![A-Za-z])", v, out)
    out = re.sub(r"\\(?:text|mathrm|mathbf)\s*\{([^{}]*)\}", r"\1", out)
    # 运算符两侧不留空格：α + β -> α+β，3 ≤ x -> 3≤x（连续运算符一次处理完）
    ops = re.escape(_MATH_OPS)
    out = re.sub(
        rf"(?<=[{_MATH_GLYPHS})\u221a])\s*([{ops}])\s*(?=[{_MATH_GLYPHS}(\u221a])",
        r"\1", out)
    # x^2 -> x²，x^10 -> x¹⁰
    def _sup(m):
        e = m.group(1)
        if e.startswith("-"):
            return "\u207b" + e[1:].translate(_SUP)
        return e.translate(_SUP)
    out = re.sub(r"(?<=[0-9A-Za-z)])\^(-?(?:\d+|n))", _sup, out)
    # 数字与单位之间不留空格：10 kΩ -> 10kΩ
    out = re.sub(
        r"(?<=\d)\s+(?=(?:k\u03a9|M\u03a9|\u03a9|\u03bcF|nF|pF|mA|\u03bcA|kV|V|mV|Hz|kHz|%))",
        "", out)
    return out


def polish(text: str, cfg: dict) -> str:
    p = cfg["polish"]
    if not p.get("enabled", True):
        return text
    sysmsg = system_prompt(cfg)
    payload = {
        "messages": [
            {"role": "system", "content": sysmsg},
            {"role": "user", "content": text},
        ],
        "temperature": p.get("temperature", 0.2),
        "max_tokens": max(256, len(text) * 2 + 128),
        "stream": False,
        # Qwen3.5 是思考模型，整理文字不需要思考链，关掉省时间
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = post_json(p["url"], payload, p["timeout"])
    out = _content_text(resp)
    out = out.strip().strip("「」\"'“”")
    if not out:
        return text
    out = tidy_math(out)
    # 防跑飞：长度离谱就直接用原文
    if len(out) > max(10, len(text) * p.get("max_ratio", 2.2)):
        print(f"[warn] 润色结果异常长，回退原文 ({len(out)} vs {len(text)})")
        return text
    return out


# ---------------------------------------------------------------- 上屏


def paste_text(text: str, cfg: dict) -> bool:
    """写剪贴板 + 模拟 Ctrl+V；返回 False 表示连剪贴板都写不进去。"""
    out = cfg["output"]
    old = None
    if out.get("restore_clipboard", True):
        try:
            old = pyperclip.paste()
        except Exception:
            old = None

    try:
        pyperclip.copy(text)
    except Exception as e:
        print(f"[error] 写剪贴板失败: {e}")
        return False
    if not out.get("paste", True):
        return True
    time.sleep(0.06)
    ctl = kb.Controller()
    with ctl.pressed(kb.Key.ctrl):
        ctl.press("v")
        ctl.release("v")

    if old is not None and out.get("restore_clipboard", True):
        def _restore():
            time.sleep(0.6)
            try:
                pyperclip.copy(old)
            except Exception:
                pass

        threading.Thread(target=_restore, daemon=True).start()
    return True


# ---------------------------------------------------------------- 主逻辑


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.sr = cfg["audio"]["samplerate"]
        self.meter = LevelMeter()
        self.rec = Recorder(
            self.sr, cfg["audio"].get("device") or None, meter=self.meter,
            max_seconds=float(cfg["audio"].get("max_seconds", 150) or 0),
        )
        self.overlay, self.native_overlay = create_overlay(self.meter, self.cfg)
        self.tray = Tray(self)
        self.lock = threading.Lock()
        self.recording = False
        self.busy = False
        self.paused = False
        self.servers = None
        self._quit = threading.Event()
        self._restart_lock = threading.Lock()
        self._overlay_tries = 0

    # ---- 状态
    def _show(self, state: str, text: str = ""):
        self.overlay.set(state, text)
        self.tray.set_state(state if not self.paused else "paused")

    def set_paused(self, value: bool):
        self.paused = value
        if value and self.recording:
            self.recording = False
            self.rec.stop()
        self._show("idle")
        print("⏸ 已暂停" if value else "▶ 已恢复")

    def _watchdog(self):
        """每 20 秒巡检：模型服务是否还活着、悬浮条线程有没有挂。"""
        while not self._quit.wait(20):
            if not self._overlay_alive():
                self._restart_overlay()
            if self.busy or self.recording or self.servers is None:
                continue
            if not self._restart_lock.acquire(blocking=False):
                continue
            try:
                self.servers.ensure_running()
            except BaseException as e:   # SystemExit 也要吃掉，否则巡检线程直接死掉
                print(f"[看门狗] 重启失败: {type(e).__name__}: {e}")
            finally:
                self._restart_lock.release()

    def _overlay_alive(self) -> bool:
        t = getattr(self.overlay, "_thread", None)
        return t is None or t.is_alive()

    def _restart_overlay(self):
        if self._overlay_tries >= 3:
            return
        self._overlay_tries += 1
        print("[看门狗] 悬浮条线程已退出，正在重建…")
        try:
            self.overlay.stop()
        except Exception:
            pass
        try:
            self.overlay, self.native_overlay = create_overlay(self.meter, self.cfg)
            self._show("idle")
            print("[看门狗] 悬浮条已恢复")
        except Exception as e:
            print(f"[看门狗] 悬浮条重建失败: {type(e).__name__}: {e}")

    def quit(self):
        self._quit.set()
        if not self.native_overlay:
            self.overlay.stop()

    # ---- 热键回调
    def on_down(self):
        if self.paused or self.busy or self.recording:
            return
        try:
            self.rec.start()
        except Exception as e:
            print(f"[error] 打开麦克风失败: {e}")
            self._show("error", f"麦克风打不开: {e}")
            return
        self.recording = True
        self._show("recording")
        print("● 录音中…")

    def on_up(self):
        if not self.recording:
            return
        self.recording = False
        audio = self.rec.stop()
        threading.Thread(target=self._pipeline, args=(audio,), daemon=True).start()

    def _pipeline(self, audio: np.ndarray):
        self.busy = True
        try:
            a = self.cfg["audio"]
            audio = trim_silence(
                audio, self.sr, a.get("min_rms", 0.012), a.get("pad_ms", 200)
            )
            dur = len(audio) / self.sr
            if dur < 0.25:
                print("(太短，忽略)")
                self._show("error", "说话太短，再说一次")
                return

            self._show("busy")
            t0 = time.perf_counter()
            try:
                raw = transcribe(to_wav(audio, self.sr), self.cfg)
            except urllib.error.HTTPError as e:
                msg = f"识别服务报错 HTTP {e.code}（看 asr.log）"
                print(f"[error] {msg}")
                self._show("error", msg)
                return
            except (TimeoutError, socket.timeout):
                msg = "识别超时（句子太长或服务卡住）"
                print(f"[error] {msg}")
                self._show("error", msg)
                return
            except urllib.error.URLError as e:
                msg = "识别服务没连上（先运行 VoiceInput.bat）"
                print(f"[error] {msg}: {e.reason}")
                self._show("error", msg)
                return
            t1 = time.perf_counter()
            print(f"[asr {dur:.2f}s audio / {t1 - t0:.2f}s] {raw}")

            if not raw.strip():
                self.overlay.set("error", "没识别出内容")
                return

            final = raw
            try:
                final = polish(raw, self.cfg)
                print(f"[polish {time.perf_counter() - t1:.2f}s] {final}")
            except Exception as e:
                print(f"[warn] 润色失败，用原文: {e}")

            if paste_text(final, self.cfg):
                self._show("done")
            else:
                self._show("error", "写剪贴板失败")
            if self.rec.overflow:
                print("[warn] 本次录音超过上限，末尾部分未包含")
        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}")
            self._show("error", f"{type(e).__name__}: {e}")
        finally:
            self.busy = False

    # ---- 热键监听（Windows 下抑制 CapsLock 原始行为）
    def start_hotkey(self):
        keyname = (self.cfg["hotkey"].get("key") or "caps_lock").lower()
        key = getattr(kb.Key, keyname, None) or keyname

        if sys.platform == "win32":
            vkmap = {"caps_lock": 0x14, "scroll_lock": 0x91, "pause": 0x13, "f9": 0x78}
            vk = vkmap.get(keyname)
            if vk is not None:
                app = self

                def win32_filter(msg, data):
                    if app.paused:      # 暂停时别吞按键，CapsLock 恢复原功能
                        return True
                    if data.vkCode == vk and msg in (0x100, 0x104):
                        app.on_down()
                        return False  # 吞掉，CapsLock 不会切换大小写
                    if data.vkCode == vk and msg in (0x101, 0x105):
                        app.on_up()
                        return False
                    return True

                listener = kb.Listener(on_press=lambda k: None, win32_event_filter=win32_filter)
                listener.daemon = True
                listener.start()
                return listener

        # 非 Windows / 其他键：普通监听
        listener = kb.Listener(
            on_press=lambda k: self.on_down() if k == key else None,
            on_release=lambda k: self.on_up() if k == key else None,
        )
        listener.daemon = True
        listener.start()
        return listener

    def run(self):
        print(f"就绪。按住 [{self.cfg['hotkey']['key']}] 说话，松开上屏。")
        print("托盘图标右键可暂停 / 开机自启 / 退出。")
        self.start_hotkey()
        if not self.tray.start():
            hint = ("托盘图标不可用（没装 pystray）。\n\n"
                    "退出方式：关闭这个窗口 / Ctrl+C，或运行 VoiceInput.bat stop")
            print("[warn] " + hint)
            if sys.stdout is None:     # pythonw / 打包后没有控制台，只能弹窗告诉用户
                notify("Voice Input", hint)
        self._show("idle")

        if self.servers is not None:
            threading.Thread(target=self._watchdog, daemon=True).start()

        if os.environ.get("VOICE_INPUT_AUTOQUIT"):  # 自测钩子
            delay = float(os.environ["VOICE_INPUT_AUTOQUIT"])

            def _auto():
                time.sleep(delay)
                print("[debug] 自动退出")
                self.quit()

            threading.Thread(target=_auto, daemon=True).start()
        if self.native_overlay:
            self._quit.wait()
        else:
            self.overlay.run()
        self.tray.stop()
        self.overlay.stop()


# ---------------------------------------------------------------- 服务进程管理

CREATE_NO_WINDOW = 0x08000000


class JobGuard:
    """把子进程绑进一个 Windows Job 对象。

    效果：本进程无论怎么死（Ctrl+C、关窗口、任务管理器强杀），
    子进程都会被系统一起带走，不会留后台。
    """

    def __init__(self):
        self.ok = False
        self._k32 = None
        self._job = None
        self._ct = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes as wt

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class BASIC(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD),
                    ("SchedulingClass", wt.DWORD),
                ]

            class EXTENDED(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            job = k32.CreateJobObjectW(None, None)
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise OSError(ctypes.get_last_error())
            self._k32, self._job, self._ct = k32, job, ctypes
            self.ok = True
        except Exception as e:  # 只是少一层保险，不影响主功能
            print(f"[warn] Job 对象不可用，改用主动结束进程: {e}")

    def assign(self, pid: int):
        if not self.ok:
            return
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        h = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if h:
            self._k32.AssignProcessToJobObject(self._job, h)
            self._k32.CloseHandle(h)


class ServerManager:
    """启动 / 等待 / 结束 llama-server 子进程（不弹窗口，日志写文件）。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.llama = resolve_path(cfg["llama"]["dir"])
        self.procs: list = []
        self._logs = []
        self.job = JobGuard()
        self._lock = threading.RLock()

    @staticmethod
    def healthy(port: int, timeout: float = 1.5) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=timeout
            ) as r:
                return r.status == 200
        except Exception:
            return False

    def _spawn(self, args, logname):
        path = ROOT / logname
        try:                       # 日志超过 5MB 就轮转，避免无限增长
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                path.replace(ROOT / (logname + ".1"))
        except Exception:
            pass
        f = open(path, "ab")
        self._logs.append(f)
        p = subprocess.Popen(
            args,
            cwd=str(self.llama),
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        self.procs.append(p)
        self.job.assign(p.pid)
        return p

    def plan(self):
        L = self.cfg["llama"]
        exe = str(self.llama / "llama-server.exe")
        if not Path(exe).exists():
            raise SystemExit(
                f"找不到 llama-server.exe：{exe}\n"
                "请到 https://github.com/ggml-org/llama.cpp/releases 下载 Windows 版，\n"
                "解压到项目下的 llama.cpp\\ 目录，或在 config.toml 的 [llama] dir 里指向它。"
            )
        ngl, threads = str(L.get("ngl", 99)), str(L.get("threads", 8))

        asr_model = resolve_path(L["asr_model"])
        asr_mmproj = resolve_path(L["asr_mmproj"])
        for f in (asr_model, asr_mmproj):
            if not f.exists():
                raise SystemExit(f"缺少模型文件：{f}")
        polish_model = resolve_path(L["polish_model"])

        common = [
            "--host", "127.0.0.1", "-ngl", ngl, "-c", "4096",
            "-np", "1", "-t", threads, "--no-warmup",
        ]
        jobs = [
            (
                8081,
                [exe, "-m", str(asr_model), "--mmproj", str(asr_mmproj),
                 "--port", "8081"] + common,
                "asr.log",
            )
        ]
        if self.cfg.get("polish", {}).get("enabled", True):
            if polish_model.exists():
                jobs.append(
                    (
                        8082,
                        [exe, "-m", str(polish_model), "--port", "8082",
                         "--temp", "0.2"] + common,
                        "polish.log",
                    )
                )
            else:
                print(f"[warn] 润色模型不存在，只启动识别服务：{polish_model}")
        return jobs

    @staticmethod
    def _pids_on_port(port: int):
        """找出监听指定端口的进程 PID。"""
        pids = set()
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            return pids
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" \
                    and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                pids.add(parts[4])
        return pids

    def _take_over(self, ports):
        """上次没退干净留下的服务：先杀掉，再起自己的。"""
        killed = 0
        for port in ports:
            for pid in self._pids_on_port(port):
                try:
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=10)
                    killed += 1
                except Exception:
                    pass
        if killed:
            print(f"清理了上次残留的 {killed} 个推理进程")
            time.sleep(1.5)

    def start(self):
        with self._lock:
            return self._start()

    def _start(self):
        jobs = self.plan()
        ports = [p for p, _, _ in jobs]
        if any(self.healthy(p) for p in ports):
            self._take_over(ports)

        for port, args, logname in jobs:
            name = Path(args[args.index("-m") + 1]).name
            print(f"启动 {name} -> :{port} ...")
            self._spawn(args, logname)

        for port, _, _ in jobs:
            t0 = time.time()
            while True:
                if self.healthy(port):
                    print(f"  :{port} 就绪（{time.time() - t0:.0f}s）")
                    break
                if time.time() - t0 > 180:
                    self.stop()
                    raise SystemExit(
                        f":{port} 等了 180 秒没就绪，看 asr.log / polish.log"
                    )
                if any(p.poll() is not None for p in self.procs):
                    self.stop()
                    raise SystemExit("llama-server 启动失败，看 asr.log / polish.log")
                time.sleep(0.5)

    def ensure_running(self) -> bool:
        with self._lock:
            return self._ensure_running()

    def _ensure_running(self) -> bool:
        """健康检查：进程死了或端口无响应 → 重启。返回是否做了重启。"""
        if not self.procs:
            return False
        dead = any(p.poll() is not None for p in self.procs)
        if not dead:
            for port, _, _ in self.plan():
                if not self.healthy(port, timeout=2.0):
                    dead = True
                    break
        if not dead:
            return False
        print("[看门狗] 推理服务无响应，正在重启…")
        self.stop()
        time.sleep(1.0)
        self.start()
        print("[看门狗] 重启完成")
        return True

    def stop(self):
        with self._lock:
            return self._stop()

    def _stop(self):
        if not self.procs:
            return
        for p in self.procs:
            if p.poll() is None:
                try:
                    p.terminate()
                except Exception:
                    pass
        t0 = time.time()
        for p in self.procs:
            try:
                p.wait(timeout=max(0.5, 6 - (time.time() - t0)))
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.procs.clear()
        for f in self._logs:
            try:
                f.close()
            except Exception:
                pass
        self._logs.clear()


_CTRL_REF = None


def install_exit_hooks(on_exit):
    """Ctrl+C / 关窗口 / 注销 时先把子进程收掉。"""
    import atexit
    import signal
    import threading

    atexit.register(on_exit)

    def _safe():
        try:
            on_exit()
        except Exception:
            pass

    if sys.platform == "win32":
        import ctypes

        HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)

        def handler(ctrl_type):
            if ctrl_type in (0, 1, 2, 5, 6):  # Ctrl+C / Ctrl+Break / 关窗 / 注销 / 关机
                threading.Thread(target=_safe, daemon=False).start()
            return False  # 交回系统默认处理

        global _CTRL_REF
        _CTRL_REF = HANDLER(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_REF, True)
    else:
        signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))


# ---------------------------------------------------------------- 启动


def acquire_single_instance(port: int = 49731):
    """用绑定本地端口当锁，防止双击两次开了两个实例抢 CapsLock。"""
    port = int(os.environ.get("VOICE_INPUT_LOCK_PORT") or port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def main() -> int:
    # ---- 1. 读配置（缺文件 / 语法错都要能看见）
    try:
        cfg = load_cfg()
    except FileNotFoundError:
        notify("Voice Input 启动失败",
               f"找不到配置文件：\n{CFG_PATH}\n\n请把 config.toml 放回程序目录。", True)
        return 1
    except Exception as e:
        notify("Voice Input 启动失败", f"config.toml 解析失败：\n{e}", True)
        return 1

    # ---- 2. 配置自检（热键回退 / 润色模型缺失降级 / 识别模型缺失致命）
    manage = bool(cfg.get("llama", {}).get("manage", True))
    try:
        for w in prepare_cfg(cfg, manage):
            print(f"[warn] {w}")
    except SystemExit as e:
        notify("Voice Input 启动失败", str(e), True)
        return 1

    # ---- 3. 单实例
    lock = acquire_single_instance()
    if lock is None:
        print("已经有一个语音输入在运行了，这个窗口会在 2 秒后自动关闭。")
        print("（要彻底退出：托盘右键 → 退出，或运行 VoiceInput.bat stop）")
        time.sleep(2.0)
        return 0

    # ---- 4. 起模型服务
    servers = None
    if manage:
        servers = ServerManager(cfg)
        try:
            servers.start()
        except SystemExit as e:
            notify("模型服务启动失败", str(e), True)
            return 1
        except Exception as e:
            notify("模型服务启动失败", f"{type(e).__name__}: {e}", True)
            return 1
        install_exit_hooks(servers.stop)

    # ---- 5. 起界面
    try:
        app = App(cfg)
    except Exception as e:
        notify("界面初始化失败", f"{type(e).__name__}: {e}", True)
        if servers is not None:
            servers.stop()
        return 1

    app.servers = servers
    try:
        app.run()
    finally:
        if servers is not None:
            print("正在关闭模型服务…")
            servers.stop()
            print("已清理干净。")
        # 托盘/音频线程可能残留，强制退出；Job 句柄随进程关闭，子进程一定被回收
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)


# ==============================================================================
#  2. 悬浮胶囊（Win32 + Pillow / Tk 兜底）
# ==============================================================================

import ctypes
import math
import queue
import sys
import threading
import time
from collections import deque
from ctypes import wintypes as wt

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------------------------------------------------------- 音量表


class LevelMeter:
    """麦克风电平 + 自动增益：不管麦克风灵敏度高低，波形都能填满。"""

    def __init__(self, n: int = 30):
        self._buf = deque([0.0] * n, maxlen=n)
        self._lock = threading.Lock()
        self._agc = 0.006

    def push(self, peak: float):
        v = min(1.0, max(0.0, peak))
        # 慢衰减的峰值包络，约 8 秒半衰期
        self._agc = max(v, self._agc * 0.985)
        with self._lock:
            self._buf.append(v)

    def snapshot(self):
        """相对峰值归一 + 幂曲线：既有动态，又不会把小噪声画成柱子。"""
        with self._lock:
            ref = max(self._agc, 1e-4)
            out = []
            for v in self._buf:
                r = v / ref
                if r < 0.06:          # 噪声门
                    out.append(0.0)
                else:
                    out.append(min(1.0, r ** 0.55))
            return out

    def clear(self):
        with self._lock:
            self._buf = deque([0.0] * self._buf.maxlen, maxlen=self._buf.maxlen)
            self._agc = 0.006


# ---------------------------------------------------------------- 配色

#            主色              亮色(高光)         暗色(底)
STATES = {
    "recording": ((255, 106, 96), (255, 176, 166), (222, 56, 62)),
    "busy":      ((116, 198, 255), (186, 232, 255), (62, 146, 226)),
    "done":      ((92, 222, 152), (168, 248, 202), (40, 186, 108)),
    "error":     ((255, 176, 82), (255, 214, 158), (232, 132, 36)),
}

ACCENTS = {
    "recording": (255, 62, 52),
    "busy": (10, 132, 255),
    "done": (46, 196, 112),
    "error": (255, 159, 28),
}

FONT_FILES = [
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", 0),
]


def _font(size: int):
    for path, idx in FONT_FILES:
        try:
            return ImageFont.truetype(path, size, index=idx)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- Win32

WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WLW_ALPHA = 0x00000002
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
DIB_RGB_COLORS = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


def _setup_api():
    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    user32.RegisterClassW.restype = wt.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, ctypes.c_void_p,
    ]
    user32.DefWindowProcW.restype = ctypes.c_void_p
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    user32.GetDC.restype = wt.HDC
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.UpdateLayeredWindow.restype = wt.BOOL
    user32.UpdateLayeredWindow.argtypes = [
        wt.HWND, wt.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
        wt.HDC, ctypes.POINTER(POINT), wt.DWORD,
        ctypes.POINTER(BLENDFUNCTION), wt.DWORD,
    ]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
        ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD,
    ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]


_setup_api()


def _catmull(points, samples: int = 8):
    if len(points) < 3:
        return list(points)
    pts = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(len(pts) - 3):
        p0, p1, p2, p3 = pts[i], pts[i + 1], pts[i + 2], pts[i + 3]
        for j in range(samples):
            t = j / samples
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t
                              + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    out.append(points[-1])
    return out


def _dpi_scale() -> float:
    try:
        user32.GetDpiForSystem.restype = wt.UINT
        return max(1.0, user32.GetDpiForSystem() / 96.0)
    except Exception:
        return 1.0


class WinOverlay:
    SS = 2          # 超采样倍数
    PAD_LOGICAL = 40  # 给外发光/投影留的边距（逻辑像素）

    def __init__(self, meter: LevelMeter, cfg: dict | None = None):
        ui = (cfg or {}).get("ui", {}) if cfg else {}
        self.meter = meter
        self.s = _dpi_scale()
        self.glass = bool(ui.get("glass", True))
        self.theme = str(ui.get("theme", "light")).lower()
        self.style = str(ui.get("style", "bars")).lower()
        self.white_alpha = int(ui.get("tint", 72))
        self.bottom_margin = int(ui.get("bottom_margin", 92) * self.s)

        self.pad = int(self.PAD_LOGICAL * self.s)
        self.pill_w = int(ui.get("width", 360) * self.s)
        self.pill_h = int(ui.get("height", 80) * self.s)
        self.W = self.pill_w + self.pad * 2
        self.H = self.pill_h + self.pad * 2
        self.pill = (self.pad, self.pad, self.W - self.pad, self.H - self.pad)
        self.radius = self.pill_h // 2

        self._q: queue.Queue = queue.Queue()
        self._state = "idle"
        self._text = ""
        self._state_t = time.time()
        self._visible = False
        self._alpha = 0.0
        self._backdrop = None
        self._light_mode = self.theme != "dark"
        self._bg_lum = 200.0
        self._eff_white = int(self.white_alpha)
        self._smooth_level = 0.0

        self._hwnd = None
        self._ready = threading.Event()
        self._err = None
        self._thread = None
        self._wndproc_ref = None
        self._bitmap = self._memdc = self._bits = self._old_bitmap = None
        self._scr_dc = None
        self._x = self._base_y = 0

        self._font_text = _font(int(17 * self.s))
        self._font_btn = _font(int(17 * self.s))
        self._chk = {}

    # -------------------------------------------------- 对外

    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("悬浮条窗口创建超时")
        if self._err:
            raise self._err
        return True

    def set(self, state: str, text: str = ""):
        self._q.put((state, text))

    def stop(self):
        if self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, 0x0010, 0, 0)
            except Exception:
                pass

    # -------------------------------------------------- 窗口

    def _thread_main(self):
        try:
            self._create_window()
            self._ready.set()
            user32.SetTimer(self._hwnd, 1, 20, None)
            msg = wt.MSG()
            while True:
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._err = e
            self._ready.set()

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = "VoiceInputGlass"
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = cls
        user32.RegisterClassW(ctypes.byref(wc))

        ex = (WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
              | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        self._hwnd = user32.CreateWindowExW(
            ex, cls, "overlay", WS_POPUP, 0, 0, self.W, self.H,
            None, None, hinst, None,
        )
        if not self._hwnd:
            raise OSError(f"CreateWindowExW 失败 {ctypes.get_last_error()}")

        self._scr_dc = user32.GetDC(None)
        self._memdc = gdi32.CreateCompatibleDC(self._scr_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.W
        bmi.bmiHeader.biHeight = -self.H
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        self._bitmap = gdi32.CreateDIBSection(
            self._scr_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        self._bits = bits
        self._old_bitmap = gdi32.SelectObject(self._memdc, self._bitmap)

        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        self._x = (sw - self.W) // 2
        self._base_y = sh - self.H - self.bottom_margin

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # -------------------------------------------------- 动画

    def _tick(self):
        now = time.time()
        try:
            while True:
                state, text = self._q.get_nowait()
                self._state = state
                self._text = text
                self._state_t = now
        except queue.Empty:
            pass

        target = 0.0 if self._state == "idle" else 1.0
        if self._state in ("done", "error") and now - self._state_t > 1.1:
            target = 0.0

        dt = 1 / 50
        step = dt / 0.18 if target > self._alpha else dt / 0.30
        self._alpha += max(-1.0, min(1.0, target - self._alpha)) * min(1.0, step)
        self._alpha = max(0.0, min(1.0, self._alpha))

        if self._alpha <= 0.002:
            if self._visible:
                user32.ShowWindow(self._hwnd, SW_HIDE)
                self._visible = False
                self._chk.pop("base_recording", None)
            return

        if not self._visible:
            self._capture_backdrop()
            self._chk = {k: v for k, v in self._chk.items() if not k.startswith("base")}
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True

        self._blit(self._render(now), self._alpha)

    def _compute_white(self, bg_lum: float) -> int:
        """按背景亮度算白度：背景暗就多加白，背景亮就少加白。
        目标：合成后的玻璃亮度稳定在 200 左右（亮玻璃 + 深色字的观感）。"""
        if self.theme == "dark":
            return int(self.white_alpha * 0.92)
        target = 242.0
        if bg_lum >= target - 8:
            a = float(self.white_alpha)
        else:
            a = 255.0 * (target - bg_lum) / max(1.0, 255.0 - bg_lum)
        return int(max(70, min(215, a)))

    def _capture_backdrop(self):
        if not self.glass:
            self._backdrop = None
            return
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab(
                bbox=(self._x, self._base_y, self._x + self.W, self._base_y + self.H),
                all_screens=True,
            ).convert("RGB")
            tw, th = max(6, self.W // 12), max(6, self.H // 12)
            small = img.resize((tw, th), Image.BILINEAR).filter(
                ImageFilter.GaussianBlur(3.0)
            )
            glass = small.resize((self.W, self.H), Image.BICUBIC)
            lum_img = img.convert("L").resize((16, 8), Image.BILINEAR)
            vals = lum_img.tobytes()
            self._bg_lum = sum(vals) / max(1, len(vals))
            self._eff_white = self._compute_white(self._bg_lum)

            if self.theme == "auto":
                gray = img.convert("L")
                stat = gray.resize((16, 8), Image.BILINEAR)
                vals = stat.tobytes()
                self._light_mode = (sum(vals) / max(1, len(vals))) > 150
            else:
                self._light_mode = self.theme != "dark"
            r, g, b = glass.split()
            # 提亮一点，玻璃才有通透感
            lift = lambda v: min(255, int(v * 1.06 + 8))
            self._backdrop = Image.merge("RGB", (r.point(lift), g.point(lift), b.point(lift)))
        except Exception:
            self._backdrop = None

    # -------------------------------------------------- 静态元素

    def _static(self, key, builder):
        if key not in self._chk:
            self._chk[key] = builder()
        return self._chk[key]

    def _geom(self):
        x0, y0, x1, y1 = self.pill
        ss = self.SS
        return (x0 * ss, y0 * ss, x1 * ss, y1 * ss)

    def _mask(self):
        def build():
            m = Image.new("L", (self.W * self.SS, self.H * self.SS), 0)
            ImageDraw.Draw(m).rounded_rectangle(
                self._geom(), self.radius * self.SS, fill=255
            )
            return m

        return self._static("mask", build)

    def _vgrey(self, top: int, bottom: int, size):
        """竖向 alpha 渐变图。"""
        w, h = size
        g = Image.new("L", (1, h))
        d = ImageDraw.Draw(g)
        for y in range(h):
            t = y / max(1, h - 1)
            d.point((0, y), int(top + (bottom - top) * t))
        return g.resize((w, h), Image.BILINEAR)

    def _stroke(self, inset: float, width_px: int) -> Image.Image:
        m = Image.new("L", (self.W * self.SS, self.H * self.SS), 0)
        g = self._geom()
        ss = self.SS
        ImageDraw.Draw(m).rounded_rectangle(
            [g[0] + inset * ss, g[1] + inset * ss,
             g[2] - inset * ss, g[3] - inset * ss],
            max(1, self.radius * ss - inset * ss),
            outline=255, width=width_px,
        )
        return m

    def _base(self):
        """白色磨砂玻璃：投影 + 透镜背景 + 磨砂白 + 厚玻璃边。"""
        theme_key = "light" if self._light_mode else "dark"

        def build():
            import PIL.ImageChops as CH

            ss, W, H = self.SS, self.W, self.H
            w, h = W * ss, H * ss
            geom = self._geom()
            rad = self.radius * ss
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

            # --- 投影：大而软 + 贴身
            lw, lh = max(8, w // 4), max(8, h // 4)
            for blur, dy, a in ((8.5, 4.6, 58), (3.0, 1.4, 38), (15.0, 7.0, 20)):
                sh = Image.new("L", (lw, lh), 0)
                d = ImageDraw.Draw(sh)
                d.rounded_rectangle(
                    [geom[0] / 4, geom[1] / 4 + dy * ss / 4,
                     geom[2] / 4, geom[3] / 4 + dy * ss / 4],
                    rad / 4, fill=255,
                )
                sh = sh.filter(ImageFilter.GaussianBlur(blur)).resize(
                    (w, h), Image.BICUBIC
                )
                lay = Image.new("RGBA", (w, h), (0, 0, 0, 255))
                lay.putalpha(sh.point(lambda v, a=a: int(v * a / 255)))
                canvas.alpha_composite(lay)

            # --- 背景：0.94 缩放采样，像玻璃透镜
            if self._backdrop is not None:
                bw, bh = int(W * 0.94), int(H * 0.94)
                ox, oy = (W - bw) // 2, (H - bh) // 2
                bg = (self._backdrop
                      .crop((ox, oy, ox + bw, oy + bh))
                      .resize((w, h), Image.BICUBIC).convert("RGBA"))
            else:
                bg = Image.new("RGBA", (w, h), (236, 238, 242, 255))
            canvas.paste(bg, (0, 0), self._mask())

            # --- 磨砂层
            if self._light_mode:
                rgb, base_a = (252, 253, 255), self._eff_white
            else:
                rgb, base_a = (12, 14, 20), int(self.white_alpha * 0.92)
            tint = Image.new("RGBA", (1, h))
            td = ImageDraw.Draw(tint)
            for y in range(h):
                t = y / max(1, h - 1)
                a = int(base_a * (1.10 - 0.26 * t))
                td.point((0, y), rgb + (max(0, min(255, a)),))
            canvas.alpha_composite(Image.composite(
                tint.resize((w, h), Image.BILINEAR),
                Image.new("RGBA", (w, h), (0, 0, 0, 0)), self._mask()))

            # --- 斜向玻璃反光（左上亮 + 右下暗，像凸面玻璃）
            gw2, gh2 = max(16, w // 6), max(16, h // 6)
            gl = Image.new("L", (gw2, gh2), 0)
            ImageDraw.Draw(gl).ellipse(
                [-gw2 * 0.25, -gh2 * 0.75, gw2 * 0.95, gh2 * 0.9], fill=86
            )
            gl = gl.filter(ImageFilter.GaussianBlur(6)).resize((w, h), Image.BICUBIC)
            gll = Image.new("RGBA", (w, h), (255, 255, 255, 0))
            gll.putalpha(Image.composite(gl, Image.new("L", (w, h), 0), self._mask()))
            canvas.alpha_composite(gll)

            sh2 = Image.new("L", (gw2, gh2), 0)
            ImageDraw.Draw(sh2).ellipse(
                [gw2 * 0.35, gh2 * 0.55, gw2 * 1.3, gh2 * 1.5], fill=44
            )
            sh2 = sh2.filter(ImageFilter.GaussianBlur(6)).resize((w, h), Image.BICUBIC)
            shl = Image.new("RGBA", (w, h), (40, 44, 56, 0))
            shl.putalpha(Image.composite(sh2, Image.new("L", (w, h), 0), self._mask()))
            canvas.alpha_composite(shl)

            # --- 中心偏上的柔和高光（透镜亮斑）
            hw, hh = 64, 40
            hl = Image.new("L", (hw, hh), 0)
            ImageDraw.Draw(hl).ellipse(
                [-hw * 0.15, -hh * 1.05, hw * 1.15, hh * 1.0], fill=64
            )
            hl = hl.filter(ImageFilter.GaussianBlur(9)).resize((w, h), Image.BICUBIC)
            hi = Image.new("RGBA", (w, h), (255, 255, 255, 0))
            hi.putalpha(Image.composite(hl, Image.new("L", (w, h), 0), self._mask()))
            canvas.alpha_composite(hi)

            # --- 厚玻璃边：底部淡暗边 + 顶部亮边 + 内侧高光
            crisp = self._stroke(0.0, max(2, int(0.9 * self.s * ss)))
            cl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            cl.putalpha(crisp.point(lambda v: int(v * 0.20)))
            canvas.alpha_composite(cl)

            dark_rim = self._stroke(0.0, max(2, int(1.2 * self.s * ss)))
            dark_a = CH.multiply(dark_rim, self._vgrey(0, 30, (w, h)))
            dl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            dl.putalpha(dark_a)
            canvas.alpha_composite(dl)

            light_rim = self._stroke(0.4, max(2, int(1.1 * self.s * ss)))
            light_a = CH.multiply(light_rim, self._vgrey(165, 55, (w, h)))
            ll = Image.new("RGBA", (w, h), (255, 255, 255, 0))
            ll.putalpha(light_a)
            canvas.alpha_composite(ll)

            bshadow = self._stroke(1.6, max(3, int(3.0 * self.s * ss)))
            bs_a = CH.multiply(bshadow, self._vgrey(0, 34, (w, h))).filter(
                ImageFilter.GaussianBlur(2.2 * ss)
            )
            bs_l = Image.new("RGBA", (w, h), (30, 32, 40, 0))
            bs_l.putalpha(bs_a)
            canvas.alpha_composite(bs_l)

            inner = self._stroke(1.5, max(2, int(1.2 * self.s * ss)))
            inner_a = CH.multiply(
                CH.multiply(inner, self._vgrey(230, 0, (w, h))), self._mask()
            )
            il = Image.new("RGBA", (w, h), (255, 255, 255, 0))
            il.putalpha(inner_a)
            canvas.alpha_composite(il)

            return canvas

        return self._static(f"base_{theme_key}_{self._eff_white // 12}", build)

    # -------------------------------------------------- 渲染

    def _render(self, now: float) -> Image.Image:
        ss, W, H = self.SS, self.W, self.H
        w, h = W * ss, H * ss
        state = self._state if self._state in STATES else "recording"
        accent = ACCENTS[state]

        canvas = self._base().copy()
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        cy = h // 2
        s = self.s * ss
        px0 = self.pill[0] * ss
        px1 = self.pill[2] * ss

        ink = (24, 24, 28, 255) if self._light_mode else (245, 245, 248, 255)
        ink_soft = (48, 48, 56, 255) if self._light_mode else (215, 216, 224, 255)

        # --- 电平
        levels = self.meter.snapshot() if self._state == "recording" else []
        lv_now = levels[-1] if levels else 0.0
        self._smooth_level += (lv_now - self._smooth_level) * 0.3

        # --- 右侧黑色圆按钮
        btn_r = 28 * s
        btn_cx = px1 - 7 * s - btn_r

        # --- 左侧状态点
        dot_x = px0 + 34 * s
        dot_r = (3.6 + 1.4 * self._smooth_level) * s
        if self._state == "recording":
            pulse = 0.5 + 0.5 * math.sin(now * 4.6)
            dot_r = dot_r * (1.0 + 0.07 * pulse)
        draw.ellipse([dot_x - dot_r, cy - dot_r, dot_x + dot_r, cy + dot_r],
                     fill=accent + (255,))
        draw.ellipse([dot_x - dot_r * 0.55, cy - dot_r * 0.72,
                      dot_x - dot_r * 0.02, cy - dot_r * 0.16],
                     fill=(255, 255, 255, 130))

        # --- 文字（识别中/已上屏/出错）
        label = ""
        if self._state == "busy":
            label = "识别中"
        elif self._state == "done":
            label = "已上屏"
        elif self._state == "error":
            label = self._text or "出错了"
        label_w = 0
        if label:
            label_w = draw.textlength(label, font=self._font_text) + 18 * s

        # --- 波形
        x0 = px0 + 60 * s
        x1 = max(x0 + 50 * s, btn_cx - btn_r - label_w - 14 * s)
        heights = self._levels_to_heights(levels, now)
        if heights:
            if self.style == "bars":
                self._draw_wave(layer, draw, heights, x0, x1, cy, s, ink, accent)
            else:
                self._draw_ribbon(layer, draw, heights, x0, x1, cy, s, ink, accent)

        # --- 黑圆按钮
        draw.ellipse([btn_cx - btn_r, cy - btn_r, btn_cx + btn_r, cy + btn_r],
                     fill=(15, 15, 17, 255))
        draw.arc([btn_cx - btn_r + 1.5 * s, cy - btn_r + 1.5 * s,
                  btn_cx + btn_r - 1.5 * s, cy + btn_r - 1.5 * s],
                 205, 325, fill=(255, 255, 255, 30),
                 width=max(1, int(1.3 * s)))
        if self._state == "recording":
            el = now - self._state_t
            txt = f"{int(el // 60)}:{int(el % 60):02d}"
            draw.text((btn_cx, cy), txt, font=self._font_btn,
                      fill=(255, 255, 255, 252), anchor="mm")
        elif self._state == "busy":
            r = btn_r * 0.42
            a0 = (now * 380) % 360
            draw.arc([btn_cx - r, cy - r, btn_cx + r, cy + r], a0, a0 + 260,
                     fill=(255, 255, 255, 240), width=max(2, int(2.2 * s)))
        elif self._state == "done":
            draw.line([(btn_cx - 6.5 * s, cy + 0.5 * s),
                       (btn_cx - 2.0 * s, cy + 4.8 * s),
                       (btn_cx + 6.5 * s, cy - 5.0 * s)],
                      fill=(255, 255, 255, 255), width=max(2, int(2.4 * s)),
                      joint="curve")
        else:
            draw.text((btn_cx, cy), "!", font=self._font_btn,
                      fill=(255, 255, 255, 252), anchor="mm")

        # --- 标签文字（按钮左侧）
        if label:
            draw.text((btn_cx - btn_r - 14 * s, cy), label, font=self._font_text,
                      fill=ink_soft, anchor="rm")

        canvas.alpha_composite(layer)
        return canvas.reduce(self.SS) if self.SS > 1 else canvas

    def _levels_to_heights(self, levels, now):
        n = 26
        if self._state == "recording" and levels:
            seq = ([0.0] * n + levels)[-n:]
            seq = [v * (0.8 + 0.2 * (i / (n - 1))) for i, v in enumerate(seq)]
            # 3 点平滑，波形看起来是"流淌"的而不是锯齿
            sm = []
            for i in range(len(seq)):
                a = seq[max(0, i - 1)]
                c = seq[min(len(seq) - 1, i + 1)]
                sm.append(0.25 * a + 0.5 * seq[i] + 0.25 * c)
            return sm
        if self._state == "busy":
            return [0.16 + 0.30 * (0.5 + 0.5 * math.sin(i * 0.62 - now * 5.2))
                    for i in range(n)]
        return []

    def _draw_wave(self, layer, draw, heights, x0, x1, cy, s, ink, accent):
        n = len(heights)
        if n < 2:
            return
        pitch = (x1 - x0) / n
        bw = max(1.8 * s, pitch * 0.30)
        max_h = 32 * s
        r = bw / 2

        for i, v in enumerate(heights):
            cx = x0 + (i + 0.5) * pitch
            hgt = max(2.2 * s, v * max_h)
            t = i / max(1, n - 1)
            alpha = int(200 + 55 * (t ** 0.5))
            col = tuple(int(ink[k] * 0.94 + accent[k] * 0.06) for k in range(3))
            draw.rounded_rectangle(
                [cx - bw / 2, cy - hgt / 2, cx + bw / 2, cy + hgt / 2],
                r, fill=col + (alpha,),
            )

    def _draw_ribbon(self, layer, draw, heights, x0, x1, cy, s, ink, accent):
        """平滑连续的对称波形（像录音软件那种），比竖条更"液态"。"""
        n = len(heights)
        if n < 3:
            return
        max_h = 27 * s
        smooth = _catmull(heights, 8)
        m = len(smooth)
        pts_x = [x0 + (x1 - x0) * (i / (m - 1)) for i in range(m)]
        half = [max(0.9 * s, v * max_h) for v in smooth]

        top = [(pts_x[i], cy - half[i]) for i in range(m)]
        bottom = [(pts_x[i], cy + half[i]) for i in reversed(range(m))]

        col = tuple(int(ink[k] * 0.90 + accent[k] * 0.10) for k in range(3))
        draw.polygon(top + bottom, fill=col + (238,))

        # 上沿一条细高光，边缘更"亮"
        draw.line(top, fill=(255, 255, 255, 70), width=max(1, int(1.0 * s)),
                  joint="curve")

    def _label(self, now: float) -> str:
        if self._state == "recording":
            el = now - self._state_t
            return f"{int(el // 60)}:{int(el % 60):02d}"
        if self._state == "busy":
            return "识别中"
        if self._state == "done":
            return "已上屏"
        if self._state == "error":
            return self._text or "出错了"
        return ""

    def _blit(self, img: Image.Image, alpha: float):
        if alpha < 0.999:
            r, g, b, a = img.split()
            a = a.point(lambda v: int(v * alpha))
            img = Image.merge("RGBA", (r, g, b, a))
        data = img.tobytes("raw", "BGRA")
        ctypes.memmove(self._bits, data, len(data))
        blend = BLENDFUNCTION(0, 0, 255, 1)
        dst = POINT(self._x, self._base_y)
        src = POINT(0, 0)
        size = SIZE(self.W, self.H)
        user32.UpdateLayeredWindow(
            self._hwnd, self._scr_dc, ctypes.byref(dst), ctypes.byref(size),
            self._memdc, ctypes.byref(src), 0, ctypes.byref(blend), WLW_ALPHA,
        )


# ---------------------------------------------------------------- Tk 备用


class TkOverlay:
    def __init__(self, meter: LevelMeter | None = None, cfg: dict | None = None):
        import tkinter as tk

        self.meter = meter
        self.tk = tk
        self._q: queue.Queue = queue.Queue()
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.93)
        except Exception:
            pass
        self.root.configure(bg="#141414")
        self.label = tk.Label(
            self.root, text="", bg="#141414", fg="#f0f0f0",
            font=("Microsoft YaHei UI", 11), padx=18, pady=9,
        )
        self.label.pack()
        self.root.withdraw()

    def start(self, timeout: float = 5.0) -> bool:
        return True

    def set(self, state: str, text: str = ""):
        self._q.put((state, text))

    def stop(self):
        try:
            self.root.quit()
        except Exception:
            pass

    def _place(self):
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), 200)
        h = self.root.winfo_reqheight()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 90}")

    def _apply(self, state: str, text: str):
        colors = {
            "recording": ("#c0392b", "● 录音中…  松开结束"),
            "busy": ("#1f6f8b", "⏳ 识别中…"),
            "done": ("#1e7a3c", "✓ 已上屏"),
            "error": ("#8e44ad", "⚠ "),
        }
        if state == "idle":
            self.root.withdraw()
            return
        bg, default = colors.get(state, ("#141414", ""))
        self.label.configure(text=(default + text) if state == "error" else default)
        self.root.configure(bg=bg)
        self.label.configure(bg=bg)
        self._place()
        self.root.deiconify()

    def _tick(self):
        try:
            while True:
                state, text = self._q.get_nowait()
                self._apply(state, text)
                if state in ("done", "error"):
                    self.root.after(900, lambda: self.root.withdraw())
        except queue.Empty:
            pass
        self.root.after(40, self._tick)

    def run(self):
        self.root.after(40, self._tick)
        self.root.mainloop()


def create_overlay(meter: LevelMeter, cfg: dict | None = None):
    if sys.platform == "win32":
        # 1) Skia 版（画质最好）
        if skia is None:
            print("[ui] 未安装 skia，使用 Pillow 渲染")
        else:
            try:
                ov = SkiaOverlay(meter, cfg)
                ov.start()
                print("[ui] 使用 Skia 渲染")
                return ov, True
            except Exception as e:
                print(f"[warn] Skia 悬浮条不可用，退回 Pillow 版: {e}")
        # 2) Pillow 版
        try:
            ov = WinOverlay(meter, cfg)
            ov.start()
            print("[ui] 使用 Pillow 渲染")
            return ov, True
        except Exception as e:
            print(f"[warn] 原生悬浮条不可用，退回 Tk 版本: {e}")
    return TkOverlay(meter, cfg), False


# ==============================================================================
#  3. 悬浮胶囊（Skia 主渲染器，可用时自动启用）
# ==============================================================================

import ctypes
import math
import queue
import sys
import threading
import time
from collections import deque
from ctypes import wintypes as wt



# ---------------------------------------------------------------- 配色

SK_ACCENTS = {
    "recording": (255, 64, 54),
    "busy": (10, 132, 255),
    "done": (46, 196, 112),
    "error": (255, 159, 28),
}

# 波形：深蓝 -> 浅青蓝
SK_BAR_LEFT = (28, 90, 255)
SK_BAR_RIGHT = (104, 206, 255)


# ---------------------------------------------------------------- 动画辅助


def _clamp01(x):
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _ease_out(t):
    t = _clamp01(t)
    return 1.0 - (1.0 - t) ** 3


def _ease_out_back(t):
    """先冲过头一点再回弹（spring 感）。"""
    t = _clamp01(t)
    c1 = 1.62
    return 1.0 + (c1 + 1.0) * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


def _mixc(c1, c2, t):
    t = _clamp01(t)
    return (c1[0] + (c2[0] - c1[0]) * t,
            c1[1] + (c2[1] - c1[1]) * t,
            c1[2] + (c2[2] - c1[2]) * t)

FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT_REG = r"C:\Windows\Fonts\msyh.ttc"


def C(r, g, b, a=255):
    return skia.Color(int(r), int(g), int(b), int(a))


def _typeface(path: str):
    try:
        return skia.Typeface.MakeFromFile(path, 0)
    except Exception:
        return skia.Typeface.MakeDefault()


WM_POWERBROADCAST = 0x0218
WM_DISPLAYCHANGE = 0x007E
WM_DPICHANGED = 0x02E0
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_APMRESUMESUSPEND = 0x0007

# 额外用到的 API（释放/重建图形资源、判断窗口是否还活着）
if sys.platform == "win32":
    try:
        user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        user32.IsWindow.argtypes = [wt.HWND]
        user32.IsWindow.restype = wt.BOOL
        gdi32.DeleteDC.argtypes = [wt.HDC]
        gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
        gdi32.DeleteObject.restype = wt.BOOL
    except Exception:
        pass


# ---------------------------------------------------------------- 几何 / 设计令牌

class Design:
    """所有设计参数集中在这里，改这个就能换风格。"""

    pill_w = 380.0          # 胶囊宽（逻辑像素）
    pill_h = 76.0           # 胶囊高
    pad = 34.0              # 窗口留白（给投影/发光）

    # 内容布局（相对胶囊左边界）
    dot_x = 30.0
    dot_r = 3.6
    bars_x0 = 54.0
    bars_right_pad = 96.0   # 右侧给黑圆留出的宽度
    bars_n = 32
    bar_w = 2.6
    bar_max_h = 30.0        # 波形最大高度（胶囊高的 ~39%）

    circle_r = 23.0
    circle_right_pad = 8.0

    # 玻璃材质
    white_top = 0.45
    white_mid = 0.30
    white_bot = 0.36
    spec_alpha = 0.38       # 左上反光强度
    blur_sigma = 8.0       # 背景磨砂半径（逻辑像素）
    lens_zoom = 1.05        # 透镜放大


class SkiaOverlay:
    SS = 1  # Skia 自带解析式抗锯齿，1 倍即可（2 倍太慢）

    def __init__(self, meter: LevelMeter, cfg: dict | None = None):
        ui = (cfg or {}).get("ui", {}) if cfg else {}
        self.meter = meter
        try:
            user32.GetDpiForSystem.restype = wt.UINT
            self.s = max(1.0, user32.GetDpiForSystem() / 96.0)
        except Exception:
            self.s = 1.0

        self.d = Design()
        if "width" in ui:
            self.d.pill_w = float(ui["width"])
        if "height" in ui:
            self.d.pill_h = float(ui["height"])
        if "tint" in ui:
            # tint 越小越透
            self.d.white_top = min(0.9, max(0.0, ui["tint"] / 255.0 + 0.16))
            self.d.white_mid = self.d.white_top * 0.65
            self.d.white_bot = self.d.white_top * 0.82
        self.d.radius = self.d.pill_h / 2.0

        s = self.s
        self.pw = int(self.d.pill_w * s)
        self.ph = int(self.d.pill_h * s)
        self.pad = int(self.d.pad * s)
        self.W = self.pw + self.pad * 2
        self.H = self.ph + self.pad * 2

        self._q: queue.Queue = queue.Queue()
        self._state = "idle"
        self._text = ""
        self._state_t = time.time()
        self._visible = False
        self._alpha = 0.0
        self._glass_img = None
        self._static = None
        self._wl = (self.d.white_top, self.d.white_mid, self.d.white_bot)
        self._smooth = 0.0
        self._gfx_dirty = False
        self._blit_fail = 0
        self._tick_n = 0

        self._hwnd = None
        self._ready = threading.Event()
        self._err = None
        self._thread = None
        self._wndproc_ref = None
        self._bitmap = self._memdc = self._bits = self._old_bitmap = None
        self._scr_dc = None
        self._x = self._base_y = 0

        self._tf_bold = _typeface(FONT_BOLD)
        self._tf_reg = _typeface(FONT_REG)
        self._font_btn = skia.Font(self._tf_bold, 14.5 * s * self.SS)
        self._font_msg = skia.Font(self._tf_reg, 16.5 * s * self.SS)
        self._paint_cache = {}

        # 动画状态
        self._canvas = None
        self._bars_v = [0.0] * int(self.d.bars_n)
        self._last_t = 0.0
        self._shown_t = 0.0
        self._appear = 1.0
        self._accent_now = SK_ACCENTS["recording"]
        self._accent_prev = SK_ACCENTS["recording"]
        self._img_cache = {}
        self._bar_shader = None

    # -------------------------------------------------- 对外
    def start(self, timeout: float = 6.0) -> bool:
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("Skia 悬浮条创建超时")
        if self._err:
            raise self._err
        return True

    def set(self, state: str, text: str = ""):
        self._q.put((state, text))

    def stop(self):
        if self._hwnd:
            try:
                user32.PostMessageW(self._hwnd, 0x0010, 0, 0)
            except Exception:
                pass

    # -------------------------------------------------- Win32 窗口
    def _thread_main(self):
        try:
            self._create_window()
            self._ready.set()
            user32.SetTimer(self._hwnd, 1, 20, None)
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._err = e
            self._ready.set()

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        cls = "VoiceInputSkia"
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = cls
        user32.RegisterClassW(ctypes.byref(wc))

        ex = (WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
              | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        self._hwnd = user32.CreateWindowExW(
            ex, cls, "overlay", WS_POPUP, 0, 0, self.W, self.H,
            None, None, hinst, None)
        if not self._hwnd:
            raise OSError(f"CreateWindowExW 失败 {ctypes.get_last_error()}")

        self._init_graphics()
        self._update_position()

    # -------------------------------------------------- 图形资源（睡眠/换分辨率后要重建）
    def _init_graphics(self):

        self._scr_dc = user32.GetDC(None)
        self._memdc = gdi32.CreateCompatibleDC(self._scr_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.W
        bmi.bmiHeader.biHeight = -self.H
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        self._bitmap = gdi32.CreateDIBSection(
            self._scr_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0)
        self._bits = bits
        self._old_bitmap = gdi32.SelectObject(self._memdc, self._bitmap)

    def _update_position(self):
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        self._x = (sw - self.W) // 2
        self._base_y = sh - self.H - int(92 * self.s)

    def _release_graphics(self):
        try:
            if self._memdc and self._old_bitmap:
                gdi32.SelectObject(self._memdc, self._old_bitmap)
            if self._memdc:
                gdi32.DeleteDC(self._memdc)
            if self._bitmap:
                gdi32.DeleteObject(self._bitmap)
            if self._scr_dc:
                user32.ReleaseDC(None, self._scr_dc)
        except Exception:
            pass
        self._scr_dc = None
        self._memdc = None
        self._bitmap = None
        self._old_bitmap = None
        self._bits = None

    def _rebuild_graphics(self):
        """重建 DC / DIB / 静态层，并强制重新显示（睡眠唤醒后必做）。"""
        self._release_graphics()
        try:
            self._init_graphics()
            self._update_position()
        except Exception as e:
            print(f"[warn] 重建图形资源失败: {e}")
        self._static = None
        self._glass_img = None
        self._visible = False
        self._blit_fail = 0
        self._gfx_dirty = False
        self._img_cache = {}
        self._bar_shader = None

    def _recreate_window(self):
        """窗口句柄都没了（会话切换/桌面重建）时整个重来。"""
        hwnd = self._hwnd
        self._hwnd = None
        try:
            if hwnd:
                user32.DestroyWindow(hwnd)
        except Exception:
            pass
        try:
            self._create_window()
            print("[ui] 悬浮条已重建")
        except Exception as e:
            print(f"[warn] 重建悬浮条失败: {e}")

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TIMER:
                self._tick()
                return 0
            if msg == WM_POWERBROADCAST:
                # 从睡眠/休眠恢复：图形资源很可能已经失效
                if wp in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    self._gfx_dirty = True
                return 1
            if msg in (WM_DISPLAYCHANGE, WM_DPICHANGED):
                # 分辨率/缩放变了（外接显示器、投影、缩放调整）
                self._gfx_dirty = True
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    # -------------------------------------------------- 动画
    def _tick(self):
        now = time.monotonic()      # 系统改时间也不会让动画/计时跳变
        self._tick_n += 1

        # 图形资源失效（睡眠唤醒/换分辨率）→ 重建
        if self._gfx_dirty:
            self._rebuild_graphics()

        # 每秒核对一次窗口是否还活着
        if self._tick_n % 50 == 0 and self._hwnd:
            try:
                if not user32.IsWindow(self._hwnd):
                    self._recreate_window()
            except Exception:
                pass

        try:
            while True:
                state, text = self._q.get_nowait()
                self._accent_prev = self._accent_now
                self._state = state
                self._text = text
                self._state_t = now
                # 只有完全隐藏时才播“出现动画”，避免胶囊还亮着时再弹一次
                if self._alpha <= 0.02:
                    self._shown_t = now
                    self._last_t = now
                    self._appear = 0.0
        except queue.Empty:
            pass

        target = 0.0 if self._state == "idle" else 1.0
        if self._state in ("done", "error") and now - self._state_t > 1.1:
            target = 0.0

        dt = 1 / 50
        step = dt / 0.18 if target > self._alpha else dt / 0.30
        self._alpha += max(-1.0, min(1.0, target - self._alpha)) * min(1.0, step)
        self._alpha = max(0.0, min(1.0, self._alpha))

        if self._alpha <= 0.002:
            if self._visible:
                user32.ShowWindow(self._hwnd, SW_HIDE)
                self._visible = False
                self._glass_img = None
                self._static = None
            # 完全隐藏后重置动画，下次出现从头播
            self._shown_t = 0.0
            self._last_t = 0.0
            if any(self._bars_v):
                self._bars_v = [0.0] * len(self._bars_v)
            return

        if not self._visible:
            if self._gfx_dirty:          # 极端情况：还没重建就先跳过这一帧
                return
            self._capture_backdrop()
            self._static = None
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True
            self._blit_fail = 0

        try:
            ok = self._blit(self._render(now), self._alpha)
        except Exception as e:
            ok = False
            print(f"[warn] 绘制失败: {e}")

        if not ok:
            self._blit_fail += 1
            if self._blit_fail >= 3:
                print("[warn] 悬浮条连续绘制失败，重建图形资源")
                self._gfx_dirty = True

    # -------------------------------------------------- 背景磨砂（每次出现时算一次）
    def _capture_backdrop(self):
        self._glass_img = None
        try:
            import numpy as np
            from PIL import Image, ImageGrab

            img = ImageGrab.grab(
                bbox=(self._x, self._base_y, self._x + self.W, self._base_y + self.H),
                all_screens=True,
            ).convert("RGB")
            # 自适应白度：背景暗就多加白，保证始终是"亮玻璃 + 深色内容"
            small = img.convert("L").resize((16, 8), Image.BILINEAR)
            v = small.tobytes()
            bg = sum(v) / max(1, len(v))
            target = 226.0
            if bg >= target - 6:
                w = self.d.white_top
            else:
                w = (target - bg) / max(1.0, 255.0 - bg)
            w = max(0.30, min(0.88, w))
            self._wl = (w, w * 0.72, w * 0.84)
            arr = np.asarray(img, dtype=np.uint8)
            rgba = np.dstack([arr, np.full(arr.shape[:2], 255, dtype=np.uint8)])
            src = skia.Image.fromarray(rgba, colorType=skia.kRGBA_8888_ColorType)

            surf = skia.Surface(self.W, self.H)
            c = surf.getCanvas()
            c.clear(skia.ColorTRANSPARENT)
            p = skia.Paint()
            sg = self.d.blur_sigma * self.s
            p.setImageFilter(skia.ImageFilters.Blur(sg, sg, skia.TileMode.kDecal))
            c.drawImage(src, 0, 0, skia.SamplingOptions(), p)
            self._glass_img = surf.makeImageSnapshot()
        except Exception as e:
            print(f"[warn] 背景磨砂失败（用纯色玻璃代替）: {e}")
            self._glass_img = None

    # -------------------------------------------------- 绘制辅助
    def _rr(self, x, y, w, h, r):
        return skia.RRect.MakeRectXY(skia.Rect.MakeXYWH(x, y, w, h), r, r)

    def _glass_paint(self, ss):
        p = skia.Paint(AntiAlias=True)
        p.setShader(skia.GradientShader.MakeLinear(
            [skia.Point(0, 0), skia.Point(0, self.ph * ss)],
            [C(255, 255, 255, int(255 * self._wl[0])),
             C(255, 255, 255, int(255 * self._wl[1])),
             C(255, 255, 255, int(255 * self._wl[2]))],
            [0.0, 0.55, 1.0]))
        return p

    def _glow_image(self, accent):
        """低分辨率柔光图（按颜色缓存）。每帧只贴一次图，不逐帧建渐变。"""
        key = ("glow", int(accent[0]) // 12, int(accent[1]) // 12, int(accent[2]) // 12)
        img = self._img_cache.get(key)
        if img is not None:
            return img
        gw, gh = max(16, self.W // 6), max(16, self.H // 6)
        surf = skia.Surface(gw, gh)
        c = surf.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        sx, sy = gw / self.W, gh / self.H
        p = skia.Paint(AntiAlias=True)
        p.setShader(skia.GradientShader.MakeRadial(
            skia.Point((self.pad + self.pw / 2) * sx, (self.pad + self.ph / 2) * sy),
            self.pw * 0.62 * sx,
            [C(*accent, 255), C(*accent, 120), C(*accent, 0)],
            [0.0, 0.50, 1.0]))
        c.drawRect(skia.Rect.MakeWH(gw, gh), p)
        img = surf.makeImageSnapshot()
        if len(self._img_cache) > 40:
            self._img_cache.clear()
        self._img_cache[key] = img
        return img

    def _sweep_image(self):
        """预渲染扫光带（白色横向渐变），每帧只旋转贴图。"""
        img = self._img_cache.get("sweep")
        if img is not None:
            return img
        bw_, bh_ = 96, 48
        surf = skia.Surface(bw_, bh_)
        c = surf.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        p = skia.Paint(AntiAlias=True)
        p.setShader(skia.GradientShader.MakeLinear(
            [skia.Point(0, 0), skia.Point(bw_, 0)],
            [C(255, 255, 255, 0), C(255, 255, 255, 255), C(255, 255, 255, 0)],
            [0.0, 0.5, 1.0]))
        c.drawRect(skia.Rect.MakeWH(bw_, bh_), p)
        img = surf.makeImageSnapshot()
        self._img_cache["sweep"] = img
        return img

    def _spinner_image(self):
        """预渲染彗尾圆弧（白色），每帧只旋转贴图。"""
        img = self._img_cache.get("spinner")
        if img is not None:
            return img
        d = 96
        surf = skia.Surface(d, d)
        c = surf.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        p = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style, StrokeWidth=6)
        p.setShader(skia.GradientShader.MakeSweep(
            d / 2, d / 2,
            [C(255, 255, 255, 0), C(255, 255, 255, 110),
             C(255, 255, 255, 205), C(255, 255, 255, 255)],
            [0.0, 0.40, 0.82, 1.0], startAngle=0, endAngle=360))
        p.setStrokeCap(skia.Paint.kRound_Cap)
        m_ = 8
        c.drawArc(skia.Rect.MakeXYWH(m_, m_, d - 2 * m_, d - 2 * m_), 0, 250, False, p)
        img = surf.makeImageSnapshot()
        self._img_cache["spinner"] = img
        return img

    # -------------------------------------------------- 静态层（投影 + 玻璃 + 边）
    def _build_static(self):
        ss = self.SS
        s = self.s * ss
        W, H = self.W * ss, self.H * ss
        surf = skia.Surface(W, H)
        c = surf.getCanvas()
        c.clear(skia.ColorTRANSPARENT)

        px, py = self.pad * ss, self.pad * ss
        pw, ph = self.pw * ss, self.ph * ss
        radius = self.d.radius * s
        body = self._rr(px, py, pw, ph, radius)

        for dy, sigma, alpha in ((1.0, 1.6, 0.12), (4.0, 7.0, 0.10), (14.0, 22.0, 0.12)):
            sp = skia.Paint(AntiAlias=True, Color=C(8, 10, 16, int(255 * alpha)))
            sp.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, sigma * s))
            c.save()
            c.translate(0, dy * s)
            c.drawRRect(body, sp)
            c.restore()

        c.save()
        c.clipRRect(body, True)
        if self._glass_img is not None:
            z = self.d.lens_zoom
            dw, dh = pw * z, ph * z
            dst = skia.Rect.MakeXYWH(px - (dw - pw) / 2, py - (dh - ph) / 2, dw, dh)
            c.drawImageRect(self._glass_img,
                            skia.Rect.MakeWH(self._glass_img.width(),
                                             self._glass_img.height()),
                            dst, skia.SamplingOptions(skia.FilterMode.kLinear),
                            skia.Paint())
        else:
            c.drawRect(skia.Rect.MakeXYWH(px, py, pw, ph),
                       skia.Paint(AntiAlias=True, Color=C(226, 229, 234, 235)))

        c.drawRect(skia.Rect.MakeXYWH(px, py, pw, ph), self._glass_paint(ss))

        spec = skia.Paint(AntiAlias=True)
        spec.setShader(skia.GradientShader.MakeRadial(
            skia.Point(px + pw * 0.26, py - ph * 0.10),
            pw * 0.72,
            [C(255, 255, 255, int(255 * self.d.spec_alpha)),
             C(255, 255, 255, int(255 * self.d.spec_alpha * 0.3)),
             C(255, 255, 255, 0)],
            [0.0, 0.45, 1.0]))
        c.drawRect(skia.Rect.MakeXYWH(px, py, pw, ph), spec)
        c.restore()

        c.drawRRect(body, skia.Paint(
            AntiAlias=True, Style=skia.Paint.kStroke_Style,
            StrokeWidth=1.0 * s, Color=C(20, 24, 32, 28)))

        top_rim = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                             StrokeWidth=1.3 * s)
        top_rim.setShader(skia.GradientShader.MakeLinear(
            [skia.Point(0, py), skia.Point(0, py + ph)],
            [C(255, 255, 255, 235), C(255, 255, 255, 120), C(255, 255, 255, 40)],
            [0.0, 0.35, 1.0]))
        inset = 0.7 * s
        c.drawRRect(self._rr(px + inset, py + inset, pw - inset * 2,
                             ph - inset * 2, radius - inset), top_rim)

        bot = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                         StrokeWidth=3.0 * s)
        bot.setShader(skia.GradientShader.MakeLinear(
            [skia.Point(0, py), skia.Point(0, py + ph)],
            [C(30, 34, 44, 0), C(30, 34, 44, 10), C(30, 34, 44, 42)],
            [0.0, 0.6, 1.0]))
        bot.setMaskFilter(skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, 1.6 * s))
        ins2 = 2.0 * s
        c.drawRRect(self._rr(px + ins2, py + ins2, pw - ins2 * 2,
                             ph - ins2 * 2, radius - ins2), bot)

        return surf.makeImageSnapshot()

    # -------------------------------------------------- 每帧渲染（只画内容）
    def _motion(self, now: float, dt: float):
        """推进所有随时间变化的量：出现动画、颜色过渡、波形平滑。"""
        if not self._shown_t:
            self._shown_t = now
        self._appear = min(1.0, max(0.0, (now - self._shown_t) / 0.55))

        # 状态色平滑过渡（避免红/蓝/绿硬切）
        st = self._state if self._state in SK_ACCENTS else "recording"
        mix = min(1.0, (now - self._state_t) / 0.30)
        self._accent_now = _mixc(self._accent_prev, SK_ACCENTS[st], _ease_out(mix))

        # 波形：32 根，起音快、回落慢；两种状态共用同一个缓冲，切换时自然过渡
        n = len(self._bars_v)
        k = min(2.0, dt * 50.0)
        if st == "recording":
            levels = self.meter.snapshot()
            m = len(levels)
            for i in range(n):
                if m:
                    src = levels[min(m - 1, int(round(i * (m - 1) / max(1, n - 1))))]
                else:
                    src = 0.0
                t = i / max(1, n - 1)
                env = 0.62 + 0.46 * math.sin(math.pi * t) ** 0.7
                target = min(1.0, src * env)
                v = self._bars_v[i]
                v += (target - v) * min(1.0, (0.62 if target > v else 0.16) * k)
                self._bars_v[i] = v
        elif st == "busy":
            for i in range(n):
                t = i / max(1, n - 1)
                w1 = 0.5 + 0.5 * math.sin(i * 0.62 - now * 6.2)
                w2 = 0.5 + 0.5 * math.sin(i * 0.23 + now * 2.4)
                target = 0.16 + 0.44 * (0.78 * w1 + 0.22 * w2) * (0.50 + 0.50 * math.sin(math.pi * t))
                v = self._bars_v[i]
                v += (target - v) * min(1.0, 0.20 * k)
                self._bars_v[i] = v
        else:                                    # done / error：波形退场
            for i in range(n):
                self._bars_v[i] *= max(0.0, 1.0 - 0.22 * k)

    def _lerp_bars(self, x0, x1, cy, s, progress):
        """条形波形：深蓝渐变 + 底部微光。"""
        n = len(self._bars_v)
        if n < 2:
            return
        pitch = (x1 - x0) / n
        bw = max(2.2 * s, min(pitch * 0.42, self.d.bar_w * 1.15 * s))
        max_h = self.d.bar_max_h * s
        peak = max(self._bars_v) if self._bars_v else 0.0
        base_a = int(135 * max(0.0, 1.0 - peak / 0.14) * progress)
        if base_a > 0 and self._state in ("recording", "busy"):
            self._canvas.drawRRect(
                self._rr(x0, cy - 1.1 * s, x1 - x0, 2.2 * s, 1.1 * s),
                skia.Paint(AntiAlias=True, Color=C(96, 150, 235, base_a)))

        # 一条 Path 装下所有柱子 + 倒影：2 次绘制而不是 64 次
        path = skia.Path()
        mirror = skia.Path()
        for i, v in enumerate(self._bars_v):
            v = v * progress
            if v <= 0.045:
                continue
            cx = x0 + (i + 0.5) * pitch
            h = max(2.4 * s, v * max_h)
            path.addRRect(self._rr(cx - bw / 2, cy - h / 2, bw, h, bw / 2))
            rh = h * 0.26
            mirror.addRRect(self._rr(cx - bw / 2, cy + h / 2 + 2.0 * s, bw, rh, bw / 2))

        if self._bar_shader is None:
            self._bar_shader = skia.GradientShader.MakeLinear(
                [skia.Point(x0, 0), skia.Point(x1, 0)],
                [C(*SK_BAR_LEFT, 235), C(*SK_BAR_RIGHT, 235)],
                [0.0, 1.0])
        paint = skia.Paint(AntiAlias=True)
        paint.setShader(self._bar_shader)
        self._canvas.drawPath(path, paint)

        mp = skia.Paint(AntiAlias=True, Color=C(*SK_BAR_RIGHT, 16))
        self._canvas.drawPath(mirror, mp)

    def _draw_dot(self, cx, cy, s, now, accent, progress):
        """录音指示点：实心红点 + 双层扩散光晕，随说话音量微微放大。"""
        lvl = self._bars_v[-4] if len(self._bars_v) >= 4 else 0.0
        base = self.d.dot_r * s * (1.0 + 0.55 * min(1.0, lvl * 1.8))
        self._canvas.drawCircle(
            cx, cy, base * (1.0 + 0.06 * math.sin(now * 5.2)),
            skia.Paint(AntiAlias=True, Color=C(*accent, int(255 * progress))))
        for k in (0.0, 0.5):
            ph = ((now * 0.78) + k) % 1.0
            r = base * (1.5 + 3.4 * ph)
            a = int(110 * (1.0 - ph) ** 2 * progress)
            if a <= 0:
                continue
            ring = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                              StrokeWidth=1.5 * s, Color=C(*accent, a))
            self._canvas.drawCircle(cx, cy, r, ring)
        self._canvas.drawCircle(cx - base * 0.30, cy - base * 0.34, base * 0.32,
                                skia.Paint(AntiAlias=True, Color=C(255, 255, 255, int(120 * progress))))

    def _draw_sweep(self, body, px, py, pw, ph, s, now, progress):
        """玻璃上滑过的高光带（出现时扫一次，监听时每 3.6 秒扫一次）。"""
        t = (now - self._shown_t)
        if t < 0:
            return
        cycle = 3.6 if self._state == "recording" else 1e9
        p = (t % cycle) / 0.95
        if p > 1.0:
            return
        x = px - pw * 0.35 + p * pw * 1.7
        w = pw * 0.38
        img = self._sweep_image()
        band = skia.Paint(AntiAlias=True)
        band.setAlphaf(0.22 * progress)
        self._canvas.save()
        self._canvas.clipRRect(body, True)
        self._canvas.rotate(-16, px + pw / 2, py + ph / 2)
        self._canvas.drawImageRect(img, skia.Rect.MakeWH(img.width(), img.height()),
                                   skia.Rect.MakeXYWH(x, py - ph * 0.6, w, ph * 2.2),
                                   skia.SamplingOptions(skia.FilterMode.kLinear), band)
        self._canvas.restore()

    def _draw_spinner(self, ccx, cy, cr, s, now, accent, progress):
        """识别中：锥形渐变彗尾 + 头部光点，呼吸式半径。"""
        spin = (now * 430.0) % 360.0
        r = cr * (0.44 + 0.025 * math.sin(now * 3.4))
        img = self._spinner_image()
        size = r * 2 * 1.85
        p = skia.Paint(AntiAlias=True)
        p.setAlphaf(min(1.0, 1.05 * progress))
        self._canvas.save()
        self._canvas.rotate(spin + 250, ccx, cy)
        self._canvas.drawImageRect(img, skia.Rect.MakeWH(img.width(), img.height()),
                                   skia.Rect.MakeXYWH(ccx - size / 2, cy - size / 2,
                                                      size, size),
                                   skia.SamplingOptions(skia.FilterMode.kLinear), p)
        self._canvas.restore()
        # 头部光点
        a = math.radians(spin + 250)
        hx, hy = ccx + r * math.cos(a), cy + r * math.sin(a)
        self._canvas.drawCircle(hx, hy, 1.9 * s,
                                skia.Paint(AntiAlias=True, Color=C(255, 255, 255, int(245 * progress))))
        self._canvas.drawCircle(hx, hy, 4.2 * s,
                                skia.Paint(AntiAlias=True, Color=C(*accent, int(45 * progress))))
        # 外圈三颗缓慢环绕的小点
        for i in range(3):
            ang = math.radians(spin * 0.35 + i * 120)
            r2 = cr * 0.72
            self._canvas.drawCircle(
                ccx + r2 * math.cos(ang), cy + r2 * math.sin(ang), 1.5 * s,
                skia.Paint(AntiAlias=True,
                           Color=C(*accent, int((70 + 40 * i) * progress))))

    def _draw_check(self, ccx, cy, cr, s, p, progress):
        """完成：对勾用路径逐段画出 + 圆环扩散 + 小火花。"""
        path = skia.Path()
        path.moveTo(ccx - 6.6 * s, cy + 0.6 * s)
        path.lineTo(ccx - 2.0 * s, cy + 5.0 * s)
        path.lineTo(ccx + 6.6 * s, cy - 5.0 * s)
        pm = skia.PathMeasure(path, False)
        dst = skia.Path()
        seg = _ease_out(min(1.0, max(0.0, (p - 0.02) / 0.26)))
        pm.getSegment(0.0, pm.getLength() * seg, dst, True)
        ck = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                        StrokeWidth=2.6 * s, Color=C(255, 255, 255, int(255 * progress)))
        ck.setStrokeCap(skia.Paint.kRound_Cap)
        ck.setStrokeJoin(skia.Paint.kRound_Join)
        self._canvas.drawPath(dst, ck)

        # 圆环扩散
        if p < 0.9:
            q = p / 0.9
            rr = cr + 34 * self.s * _ease_out(q)
            ring = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                              StrokeWidth=2.0 * self.s * (1.0 - 0.6 * q),
                              Color=C(*SK_ACCENTS["done"], int(120 * (1 - q) ** 1.6 * progress)))
            self._canvas.drawCircle(ccx, cy, rr, ring)

        # 六颗小火花
        if p < 0.75:
            q = p / 0.75
            for i in range(6):
                ang = math.radians(-150 + i * 60 + (i % 2) * 12)
                dist = cr * 1.15 + 30 * self.s * _ease_out(q) * (0.7 + 0.3 * (i % 3) / 2)
                sx = ccx + dist * math.cos(ang)
                sy = cy + dist * math.sin(ang) + 6 * self.s * q * q
                a = int(210 * (1 - q) ** 1.4 * progress)
                if a > 0:
                    self._canvas.drawCircle(
                        sx, sy, (2.1 - 1.2 * q) * self.s,
                        skia.Paint(AntiAlias=True,
                                   Color=C(*SK_ACCENTS["done"], a)))

    def _draw_bang(self, ccx, cy, s, now, progress):
        """出错：感叹号弹跳进入。"""
        t = min(1.0, (now - self._state_t) / 0.32)
        k = 1.0 + 0.55 * (1.0 - _ease_out(t)) * math.cos(t * 9.0)
        bomp = skia.Paint(AntiAlias=True, Color=C(255, 255, 255, int(250 * progress)))
        self._canvas.save()
        self._canvas.translate(ccx, cy)
        self._canvas.scale(k, k)
        self._canvas.translate(-ccx, -cy)
        self._canvas.drawRRect(self._rr(ccx - 1.5 * s, cy - 8.5 * s, 3.0 * s, 11.0 * s, 1.5 * s), bomp)
        self._canvas.drawCircle(ccx, cy + 6.4 * s, 1.6 * s, bomp)
        self._canvas.restore()

    def _bar_progress(self, now):
        """波形整体出现/退场进度。"""
        if self._state == "recording":
            return min(1.0, (now - self._state_t) / 0.22)
        if self._state == "busy":
            return 1.0
        return 1.0

    def _render(self, now: float):
        ss = self.SS
        s = self.s * ss
        W, H = self.W * ss, self.H * ss
        if self._static is None:
            self._static = self._build_static()

        dt = max(0.004, min(0.05, now - self._last_t)) if self._last_t else 0.02
        self._last_t = now
        self._motion(now, dt)

        surf = skia.Surface(W, H)
        c = surf.getCanvas()
        self._canvas = c
        c.clear(skia.ColorTRANSPARENT)

        px, py = self.pad * ss, self.pad * ss
        pw, ph = self.pw * ss, self.ph * ss
        radius = self.d.radius * s
        body = self._rr(px, py, pw, ph, radius)
        cx0, cy0 = px + pw / 2, py + ph / 2
        cy = py + ph / 2

        st = self._state if self._state in SK_ACCENTS else "recording"
        accent = self._accent_now

        # ---- 出现动画：缩放 + 上浮 + 轻微回弹
        appear = _ease_out_back(self._appear) if self._appear < 1 else 1.0
        k = 0.90 + 0.10 * appear
        dy = 13.0 * (1.0 - _ease_out(self._appear)) * self.s
        shake = 0.0
        if st == "error":
            t = now - self._state_t
            shake = 7.0 * math.sin(t * 33.0) * math.exp(-t * 7.0) * self.s

        c.save()
        c.translate(shake, dy)
        c.translate(cx0, cy0)
        c.scale(k, k)
        c.translate(-cx0, -cy0)

        # ---- 状态色光晕（在玻璃后面，透出来）
        glow_a = 0.0
        if st == "recording":
            glow_a = 0.10 + 0.03 * math.sin(now * 2.2)
        elif st == "busy":
            glow_a = 0.12 + 0.04 * math.sin(now * 3.6)
        elif st == "done":
            glow_a = 0.14 * max(0.0, 1.0 - (now - self._state_t) / 0.9)
        elif st == "error":
            glow_a = 0.10
        if glow_a > 0.002:
            gi = self._glow_image(accent)
            gp = skia.Paint(AntiAlias=True)
            gp.setAlphaf(min(1.0, glow_a))
            c.drawImageRect(gi, skia.Rect.MakeWH(gi.width(), gi.height()),
                            skia.Rect.MakeXYWH(px - 18 * s, py - 14 * s,
                                               pw + 36 * s, ph + 32 * s),
                            skia.SamplingOptions(skia.FilterMode.kLinear), gp)

        c.drawImage(self._static, 0, 0)

        if st == "done":
            ft = (now - self._state_t) / 0.45
            if ft < 1.0:
                fp = skia.Paint(AntiAlias=True)
                fp.setShader(skia.GradientShader.MakeRadial(
                    skia.Point(px + pw * 0.82, cy), pw * 0.55,
                    [C(*accent, int(46 * (1 - ft) ** 2)), C(*accent, 0)],
                    [0.0, 1.0]))
                self._canvas.save()
                self._canvas.clipRRect(body, True)
                self._canvas.drawRect(skia.Rect.MakeXYWH(px, py, pw, ph), fp)
                self._canvas.restore()

        # ---- 状态色内描边（把红/蓝/绿“贴”到胶囊边缘）
        rim = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                         StrokeWidth=1.4 * s, Color=C(*accent, 34))
        c.drawRRect(body, rim)

        # ---- 玻璃内：扫光 + 波形 + 红点
        progress = self._bar_progress(now)
        self._draw_sweep(body, px, py, pw, ph, s, now, self._alpha if self._alpha > 0 else 1.0)

        c.save()
        c.clipRRect(body, True)
        x0 = px + self.d.bars_x0 * s
        x1 = px + pw - self.d.bars_right_pad * s
        self._lerp_bars(x0, x1, cy, s, progress)
        c.restore()

        dot_cx = px + self.d.dot_x * s
        self._draw_dot(dot_cx, cy, s, now, accent, 1.0)

        # ---- 右侧圆形按钮
        cr = self.d.circle_r * s
        ccx = px + pw - (self.d.circle_right_pad * s + cr)
        pop = 1.0
        if st == "done":
            t = now - self._state_t
            pop = 1.0 + 0.11 * math.exp(-8.0 * t) * math.cos(16.0 * t)
        c.save()
        c.translate(ccx, cy)
        c.scale(pop, pop)
        c.translate(-ccx, -cy)

        bt = skia.Paint(AntiAlias=True, Color=C(16, 16, 18, 250))
        c.drawCircle(ccx, cy, cr, bt)
        ring = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                          StrokeWidth=1.3 * s)
        ring.setShader(skia.GradientShader.MakeLinear(
            [skia.Point(ccx - cr, cy - cr), skia.Point(ccx + cr, cy + cr)],
            [C(*accent, 110), C(*accent, 0)]))
        c.drawCircle(ccx, cy, cr - 0.9 * s, ring)

        if st == "recording":
            el = now - self._state_t
            frac = (el % 1.0)
            kk = 1.0 + 0.055 * math.exp(-9.0 * frac)
            c.save()
            c.translate(ccx, cy)
            c.scale(kk, kk)
            c.translate(-ccx, -cy)
            self._center_text(c, "%d:%02d" % (int(el // 60), int(el % 60)),
                              ccx, cy, self._font_btn, C(255, 255, 255, 250))
            c.restore()
        elif st == "busy":
            self._draw_spinner(ccx, cy, cr, s, now, accent, 1.0)
        elif st == "done":
            t = now - self._state_t
            self._draw_check(ccx, cy, cr, s, t, 1.0)
        else:
            self._draw_bang(ccx, cy, s, now, 1.0)
        c.restore()

        # ---- 文字（完成 / 出错）
        if st in ("done", "error"):
            t = now - self._state_t
            msg = "已上屏" if st == "done" else (self._text or "出错了")
            ap = min(1.0, max(0.0, (t - 0.10) / 0.26))
            txt_cx = (px + self.d.dot_x * s + ccx - cr) / 2
            f = self._font_msg
            limit = (ccx - cr - (px + self.d.dot_x * s) - 34 * s)
            while msg and f.measureText(msg) > limit:
                msg = msg[:-1]
            if msg:
                self._center_text(c, msg, txt_cx + (1.0 - ap) * 10 * s, cy, f,
                                  C(26, 26, 30, int(255 * ap)))

        c.restore()

        img = surf.makeImageSnapshot()
        if ss > 1:
            small = skia.Surface(self.W, self.H)
            sc = small.getCanvas()
            sc.clear(skia.ColorTRANSPARENT)
            sc.drawImageRect(img,
                             skia.Rect.MakeWH(img.width(), img.height()),
                             skia.Rect.MakeWH(self.W, self.H),
                             skia.SamplingOptions(skia.CubicResampler.Mitchell()),
                             skia.Paint())
            img = small.makeImageSnapshot()
        return img

    def _center_text(self, c, text, cx, cy, font, color):
        w = font.measureText(text)
        m = font.getMetrics()
        y = cy - (m.fAscent + m.fDescent) / 2
        c.drawString(text, cx - w / 2, y,
                     font, skia.Paint(AntiAlias=True, Color=color))

    # -------------------------------------------------- 上屏
    def _blit(self, img, alpha: float):
        # 输出的就是预乘 BGRA，正好是 UpdateLayeredWindow 要的格式
        arr = img.toarray(colorType=skia.kBGRA_8888_ColorType,
                          alphaType=skia.kPremul_AlphaType)
        if alpha < 0.999:
            arr = arr.copy()
            arr[..., :] = (arr.astype("float32") * alpha).astype("uint8")
        ctypes.memmove(self._bits, arr.tobytes(), arr.nbytes)
        blend = BLENDFUNCTION(0, 0, 255, 1)
        dst = POINT(self._x, self._base_y)
        src = POINT(0, 0)
        size = SIZE(self.W, self.H)
        return bool(user32.UpdateLayeredWindow(
            self._hwnd, self._scr_dc, ctypes.byref(dst), ctypes.byref(size),
            self._memdc, ctypes.byref(src), 0, ctypes.byref(blend), WLW_ALPHA))


# ==============================================================================
#  4. 系统托盘
# ==============================================================================

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

try:
    import pystray
except Exception:  # 没装就自动跳过，不影响主功能
    pystray = None

APP_NAME = "LocalVoiceInput"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


# ---------------------------------------------------------------- 图标


def _icon_image(color, paused: bool = False, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 3
    d.rounded_rectangle(
        [pad, pad, size - pad, size - pad], int(size * 0.26),
        fill=(34, 36, 44, 255), outline=(255, 255, 255, 40), width=1,
    )
    c = color if not paused else (120, 122, 130)
    # 话筒胶囊
    w, h = size * 0.20, size * 0.30
    cx, cy = size * 0.5, size * 0.42
    d.rounded_rectangle(
        [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], w / 2, fill=c + (255,)
    )
    # 支架弧线
    aw = size * 0.30
    ay = size * 0.50
    d.arc(
        [cx - aw / 2, ay - h * 0.15, cx + aw / 2, ay + h * 0.75],
        start=0, end=180, fill=c + (255,), width=max(2, int(size * 0.05)),
    )
    # 底座
    d.line(
        [(cx, size * 0.80), (cx, size * 0.86)],
        fill=c + (255,), width=max(2, int(size * 0.05)),
    )
    d.line(
        [(cx - size * 0.12, size * 0.86), (cx + size * 0.12, size * 0.86)],
        fill=c + (255,), width=max(2, int(size * 0.05)),
    )
    if paused:
        d.line(
            [(size * 0.34, size * 0.66), (size * 0.66, size * 0.34)],
            fill=(255, 90, 90, 255), width=max(3, int(size * 0.07)),
        )
    return img


ICONS = {
    "idle": _icon_image((150, 200, 255)),
    "recording": _icon_image((255, 82, 82)),
    "busy": _icon_image((64, 165, 245)),
    "done": _icon_image((52, 199, 123)),
    "paused": _icon_image((150, 200, 255), paused=True),
}


# ---------------------------------------------------------------- 开机自启


def _launch_command() -> str:
    if getattr(sys, 'frozen', False):   # PyInstaller 打包后直接指向 exe
        return f'"{Path(sys.executable)}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    return f'"{runner}" "{ROOT / "voice_input.py"}"'


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, APP_NAME)
            return bool(v)
    except Exception:
        return False


def set_autostart(enable: bool):
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as k:
            if enable:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        print(f"[warn] 设置开机自启失败: {e}")
        return False


# ---------------------------------------------------------------- 托盘


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = None
        self._state = "idle"

    # ---- 供 App 调用
    def set_state(self, state: str):
        """idle / recording / busy / done / paused"""
        if not self.icon:
            return
        if state == self._state:
            return
        self._state = state
        key = "paused" if self.app.paused else state
        self.icon.icon = ICONS.get(key, ICONS["idle"])
        self.icon.title = {
            "idle": "本地语音输入 · 待命",
            "recording": "本地语音输入 · 录音中",
            "busy": "本地语音输入 · 识别中",
            "done": "本地语音输入 · 已上屏",
            "paused": "本地语音输入 · 已暂停",
        }.get(key, "本地语音输入")
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def start(self) -> bool:
        if pystray is None:
            print("[warn] 没装 pystray，托盘不可用")
            return False
        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: "已暂停（点击恢复）" if self.app.paused else "暂停监听",
                self._toggle_pause,
                checked=lambda item: self.app.paused,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "开机自启",
                self._toggle_autostart,
                checked=lambda item: autostart_enabled(),
            ),
            pystray.MenuItem("打开配置", self._open_config),
            pystray.MenuItem("查看日志", self._open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._quit),
        )
        self.icon = pystray.Icon(
            "voice_input",
            ICONS["idle"],
            "本地语音输入 · 待命",
            menu,
        )
        self.icon.run_detached()
        return True

    def stop(self):
        """非阻塞停止：pystray 的 stop 偶尔会卡住，丢到线程里。"""
        if not self.icon:
            return
        import threading

        icon = self.icon
        self.icon = None

        def _do():
            try:
                icon.stop()
            except Exception:
                pass

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(1.5)

    # ---- 菜单回调
    def _toggle_pause(self, icon=None, item=None):
        self.app.set_paused(not self.app.paused)
        self.set_state(self._state)

    def _toggle_autostart(self, icon=None, item=None):
        set_autostart(not autostart_enabled())

    def _open_config(self, icon=None, item=None):
        try:
            os.startfile(str(ROOT / "config.toml"))
        except Exception as e:
            print(f"[warn] 打开配置失败: {e}")

    def _open_logs(self, icon=None, item=None):
        try:
            os.startfile(str(ROOT))
        except Exception as e:
            print(f"[warn] 打开目录失败: {e}")

    def _quit(self, icon=None, item=None):
        self.app.quit()


# ==============================================================================
#  5. 命令行工具：--setup（一键准备）/ --test（测链路）/ --make-icon
# ==============================================================================

LLAMA_BUILD = "b11304"
LLAMA_ASSETS = {
    # backend: (主程序包, 额外运行库包)
    "cuda":   ("llama-{b}-bin-win-cuda-12.4-x64.zip",
               "cudart-llama-bin-win-cuda-12.4-x64.zip"),
    "vulkan": ("llama-{b}-bin-win-vulkan-x64.zip", None),
    "cpu":    ("llama-{b}-bin-win-cpu-x64.zip", None),
}
LLAMA_URL = "https://github.com/ggml-org/llama.cpp/releases/download/{b}/{asset}"

HF = "https://huggingface.co"
MODEL_FILES = {
    "asr": [
        ("ggml-org/Qwen3-ASR-1.7B-GGUF/resolve/main/Qwen3-ASR-1.7B-Q8_0.gguf",
         "Qwen3-ASR-1.7B-Q8_0.gguf", "ASR 识别模型 (Q8_0, 2.2 GB)"),
        ("ggml-org/Qwen3-ASR-1.7B-GGUF/resolve/main/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf",
         "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf", "ASR 音频编码器 (0.34 GB)"),
    ],
    "polish": [
        ("unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf",
         "Qwen3.5-4B-Q4_K_M.gguf", "润色模型 (Q4_K_M, 2.9 GB)"),
    ],
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def download(url: str, dest: Path, label: str) -> bool:
    """带断点续传和进度显示的下载。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if dest.exists() and not part.exists():
        print(f"[skip] {label}  已存在 ({human(dest.stat().st_size)})")
        return True

    start = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "voice-input/1.0"})
    if start:
        req.add_header("Range", f"bytes={start}-")
        print(f"[resume] {label}  从 {human(start)} 继续")

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status == 200 and start:      # 服务端不支持 Range
                start = 0
                part.unlink(missing_ok=True)
            total = int(r.headers.get("Content-Length") or 0)
            total = total + start if r.status == 206 else total
            print(f"[get ] {label}  {human(total) if total else ''}")
            done, t0, last = start, time.time(), 0.0
            with open(part, "ab" if start else "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last >= 0.5:
                        last = now
                        speed = (done - start) / max(1e-6, now - t0)
                        pct = f"{done * 100 / total:5.1f}%" if total else "  ?  "
                        eta = f"  ETA {(total - done) / max(1e-6, speed):.0f}s" if total and speed else ""
                        print(f"\r       {pct}  {human(done)} / {human(total) if total else '?'}"
                              f"  {human(speed)}/s{eta}", end="", flush=True)
            print()
            if total and done < total:
                print(f"[warn] {label} 不完整，重跑可续传")
                return False
    except urllib.error.HTTPError as e:
        if e.code != 416:
            print(f"\n[err ] {label}: HTTP {e.code} {e.reason}")
            return False
    except Exception as e:
        print(f"\n[err ] {label}: {type(e).__name__}: {e}")
        return False

    part.replace(dest)
    print(f"[ok  ] {label}  ->  {dest.relative_to(ROOT)}")
    return True


def _extract_zip(zip_path: Path, dest: Path):
    """解压；如果压缩包只有一个顶层目录，去掉这层。"""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if n and not n.endswith("/")]
        tops = {n.split("/")[0] for n in names}
        strip = len(tops) == 1 and all("/" in n for n in names)
        top = next(iter(tops)) if strip else ""
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if strip:
                if not name.startswith(top + "/"):
                    continue
                name = name[len(top) + 1:]
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)


def fetch_llama(backend: str = "cuda") -> bool:
    exe = ROOT / "llama" / "llama-server.exe"
    if exe.exists():
        print("[skip] llama.cpp 已就绪: llama\\llama-server.exe")
        return True
    if backend not in LLAMA_ASSETS:
        print(f"[err ] 未知后端: {backend}")
        return False

    main_asset, cudart = LLAMA_ASSETS[backend]
    tmp = ROOT / "_download"
    tmp.mkdir(parents=True, exist_ok=True)
    ok = True
    for asset in [main_asset.format(b=LLAMA_BUILD), cudart]:
        if not asset:
            continue
        url = LLAMA_URL.format(b=LLAMA_BUILD, asset=asset)
        zip_path = tmp / asset
        if not download(url, zip_path, f"llama.cpp {backend}  {asset}"):
            return False
        try:
            _extract_zip(zip_path, ROOT / "llama")
            zip_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"[err ] 解压失败 {asset}: {e}")
            ok = False
    if ok and not exe.exists():
        print("[err ] 解压后没找到 llama\\llama-server.exe，请手动检查")
        ok = False
    try:
        tmp.rmdir()
    except Exception:
        pass
    return ok


def fetch_models(only: str | None = None, endpoint: str | None = None) -> bool:
    endpoint = (endpoint or os.environ.get("HF_ENDPOINT") or HF).rstrip("/")
    groups = [only] if only else ["asr", "polish"]
    print(f"模型目录: {ROOT / 'models'}\n下载源  : {endpoint}\n")
    ok = True
    for group in groups:
        for path, name, label in MODEL_FILES[group]:
            ok = download(f"{endpoint}/{path}", ROOT / "models" / name, label) and ok
    return ok


def ensure_deps() -> bool:
    need = []
    for mod, pkg in [("numpy", "numpy"), ("sounddevice", "sounddevice"),
                     ("pyperclip", "pyperclip"), ("pynput", "pynput"),
                     ("PIL", "Pillow"), ("pystray", "pystray")]:
        try:
            __import__(mod)
        except Exception:
            need.append(pkg)
    if not need:
        return True
    print(f"[pip ] 缺少依赖: {', '.join(need)}，正在安装…")
    req = ROOT / "requirements.txt"
    cmd = ([sys.executable, "-m", "pip", "install", "-r", str(req)]
           if req.exists() else [sys.executable, "-m", "pip", "install", *need])
    if subprocess.call(cmd) != 0:
        print("[err ] 依赖安装失败，请手动运行: pip install -r requirements.txt")
        return False
    return True


def setup_all(args) -> int:
    print("=" * 60)
    print("  一次性准备：llama.cpp → 模型")
    print("=" * 60 + "\n")
    ok = True
    if not args.no_llama:
        ok = fetch_llama(args.backend) and ok
    if not args.no_models:
        ok = fetch_models(args.only) and ok
    print()
    if ok:
        print("准备完成。双击 VoiceInput.bat 或运行 python voice_input.py 即可。")
        return 0
    print("有步骤失败，修好后重跑：python voice_input.py --setup")
    return 1


def check_env() -> int:
    """检查 llama.cpp 和模型是否就位；0 = 可以启动。"""
    try:
        cfg = load_cfg()
    except Exception as e:
        print(f"[X] 读不到 config.toml: {e}")
        return 1
    L = cfg.get("llama", {})
    missing = []
    exe = resolve_path(L.get("dir", "llama")) / "llama-server.exe"
    if not exe.exists():
        missing.append(f"llama-server.exe  ->  {exe}")
    for k in ("asr_model", "asr_mmproj"):
        p = resolve_path(L.get(k, ""))
        if not p.exists():
            missing.append(f"{k:<12}  ->  {p}")
    if missing:
        print("[X] 缺少运行文件：")
        for m in missing:
            print("    - " + m)
        print("\n    一键下载: python voice_input.py --setup")
        return 1
    pol = resolve_path(L.get("polish_model", ""))
    if cfg.get("polish", {}).get("enabled", True) and not pol.exists():
        print("[!] 没有润色模型，启动时会自动关闭润色：")
        print("    - " + str(pol))
    print("[OK] 环境就绪：llama.cpp + 识别模型都在。")
    return 0


def load_wav_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16)
    elif sw == 4:
        a = (np.frombuffer(raw, dtype=np.int32) >> 16).astype(np.int16)
    elif sw == 1:
        a = ((np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8)
    else:
        raise SystemExit(f"不支持的位深 {sw * 8}bit")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000:
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.arange(n) * (sr / 16000), np.arange(len(a)), a).astype(np.int16)
    return a


def test_wav(path: str, do_polish: bool = True) -> int:
    p = Path(path)
    if not p.is_absolute():
        p = (Path.cwd() / p) if (Path.cwd() / p).exists() else (ROOT / p)
    if not p.exists():
        print(f"文件不存在: {p}")
        return 2
    cfg = load_cfg()
    audio = load_wav_16k(p)
    print(f"音频: {p.name}  {len(audio) / 16000:.2f}s\n")
    t0 = time.time()
    try:
        raw = transcribe(to_wav(audio, 16000), cfg)
    except urllib.error.URLError as e:
        print(f"[ASR 连接失败] {e.reason}  先启动服务：VoiceInput.bat 或 python voice_input.py")
        return 3
    print(f"[ASR {time.time() - t0:.2f}s]\n{raw}\n")
    if do_polish:
        t1 = time.time()
        try:
            out = polish(raw, cfg)
            print(f"[润色 {time.time() - t1:.2f}s]\n{out}")
        except Exception as e:
            print(f"[润色失败] {type(e).__name__}: {e}")
    return 0


def make_icon(path: str) -> int:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    img = _icon_image((150, 200, 255), size=256)
    img.save(p, format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"已生成图标: {p}")
    return 0


def cli():
    ap = argparse.ArgumentParser(
        prog="voice_input.py",
        description="本地离线语音输入（Qwen3-ASR + Qwen3.5，llama.cpp 本地推理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("不带参数 = 启动语音输入。\n"
                "  python voice_input.py --setup                  准备 llama.cpp + 模型\n"
                "  python voice_input.py --setup --backend vulkan 换 Vulkan 后端\n"
                "  python voice_input.py --test sample.wav        不用麦克风，测整条链路\n"),
    )
    ap.add_argument("--setup", action="store_true", help="一次性准备运行环境")
    ap.add_argument("--backend", default="cuda", choices=["cuda", "vulkan", "cpu"],
                    help="llama.cpp 后端（默认 cuda）")
    ap.add_argument("--no-llama", action="store_true", help="--setup 时跳过 llama.cpp")
    ap.add_argument("--no-models", action="store_true", help="--setup 时跳过模型下载")
    ap.add_argument("--only", choices=["asr", "polish"], help="--setup 时只下其中一组模型")
    ap.add_argument("--test", metavar="WAV", help="用 wav 文件测 ASR（不录音）")
    ap.add_argument("--no-polish", action="store_true", help="--test 时跳过润色")
    ap.add_argument("--make-icon", metavar="ICO", help="导出托盘图标为 .ico（打包用）")
    ap.add_argument("--check", action="store_true", help="检查 llama.cpp / 模型是否就位")
    ap.add_argument("--config", metavar="TOML", help="使用指定的配置文件")
    return ap.parse_args()


if __name__ == "__main__":
    _args = cli()
    if _args.config:
        os.environ["VOICE_INPUT_CONFIG"] = str(Path(_args.config).resolve())
    if _args.make_icon:
        sys.exit(make_icon(_args.make_icon))
    if _args.check:
        sys.exit(check_env())
    if _args.setup:
        if not ensure_deps():
            sys.exit(1)
        sys.exit(setup_all(_args))
    if _args.test:
        sys.exit(test_wav(_args.test, not _args.no_polish))
    sys.exit(main())
