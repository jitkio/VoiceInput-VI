#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地语音输入 —— 按住热键说话，松开自动上屏。

链路：  麦克风 -> 掐静音 -> Qwen3-ASR-1.7B (llama.cpp) -> Qwen3.5-4B 润色 -> 粘贴到光标

启动前先跑 start_asr.bat / start_polish.bat。
"""
from __future__ import annotations

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

from overlay_win import LevelMeter, create_overlay
from tray_icon import Tray

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.toml"

# pythonw（开机自启）没有控制台，stdout 是 None，print 会炸 → 写日志文件
if sys.stdout is None:
    _early_log = open(ROOT / "client.log", "a", encoding="utf-8", buffering=1)
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
    # 防跑飞：长度离谱就直接用原文
    if len(out) > max(10, len(text) * p.get("max_ratio", 2.2)):
        print(f"[warn] 润色结果异常长，回退原文 ({len(out)} vs {len(text)})")
        return text
    return out


# ---------------------------------------------------------------- 上屏


def paste_text(text: str, cfg: dict):
    out = cfg["output"]
    old = None
    if out.get("restore_clipboard", True):
        try:
            old = pyperclip.paste()
        except Exception:
            old = None

    pyperclip.copy(text)
    if not out.get("paste", True):
        return
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
        """每 20 秒巡检一次推理服务；睡眠唤醒后模型进程可能已被系统回收。"""
        while not self._quit.wait(20):
            if self.busy or self.recording or self.servers is None:
                continue
            if not self._restart_lock.acquire(blocking=False):
                continue
            try:
                self.servers.ensure_running()
            except Exception as e:
                print(f"[看门狗] 重启失败: {e}")
            finally:
                self._restart_lock.release()

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
                self._show("idle")
                return

            self._show("busy")
            t0 = time.time()
            try:
                raw = transcribe(to_wav(audio, self.sr), self.cfg)
            except urllib.error.URLError as e:
                msg = "识别服务没连上（先运行 start_all.bat）"
                print(f"[error] {msg}: {e.reason}")
                self._show("error", msg)
                return
            t1 = time.time()
            print(f"[asr {dur:.2f}s audio / {t1 - t0:.2f}s] {raw}")

            if not raw.strip():
                self.overlay.set("error", "没识别出内容")
                return

            final = raw
            try:
                final = polish(raw, self.cfg)
                print(f"[polish {time.time() - t1:.2f}s] {final}")
            except Exception as e:
                print(f"[warn] 润色失败，用原文: {e}")

            paste_text(final, self.cfg)
            self._show("done")
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
        self.tray.start()
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
        if not polish_model.exists():
            raise SystemExit(f"缺少润色模型：{polish_model}")

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
            jobs.append(
                (
                    8082,
                    [exe, "-m", str(polish_model), "--port", "8082",
                     "--temp", "0.2"] + common,
                    "polish.log",
                )
            )
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
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def main():
    cfg = load_cfg()
    lock = acquire_single_instance()
    if lock is None:
        print("已经有一个语音输入在运行了，这个窗口会在 2 秒后自动关闭。")
        print("（要彻底退出，用托盘右键 → 退出，或双击 stop_all.bat）")
        time.sleep(2.0)
        return

    servers = None
    if cfg.get("llama", {}).get("manage", True):
        servers = ServerManager(cfg)
        servers.start()
        install_exit_hooks(servers.stop)

    app = App(cfg)
    app.servers = servers
    try:
        app.run()
    finally:
        if servers is not None:
            print("正在关闭模型服务…")
            servers.stop()
            print("已清理干净。")
        # 托盘/音频线程可能残留，强制退出；Job 句柄随进程关闭，子进程一定被回收
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
