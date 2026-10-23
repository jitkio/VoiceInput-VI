#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试脚本：把一个 wav 文件丢给本地 ASR 服务，看识别结果 + 润色结果。

用法：  python test_asr.py <文件.wav>           自己录/准备一段 16k 单声道 wav
        python test_asr.py <文件.wav> --no-polish

需要先启动推理服务（start_all.bat，或手动 start_asr.bat / start_polish.bat）。
"""
from __future__ import annotations

import io
import sys
import time
import tomllib
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_input as vi  # noqa: E402

ROOT = Path(__file__).resolve().parent


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
        raise SystemExit(f"不支持的位深 {sw*8}bit")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != 16000:
        n = int(len(a) * 16000 / sr)
        a = np.interp(np.arange(n) * (sr / 16000), np.arange(len(a)), a).astype(np.int16)
    return a


def main():
    args = [a for a in sys.argv[1:]]
    do_polish = "--no-polish" not in args
    files = [a for a in args if not a.startswith("--")]
    path = Path(files[0]) if files else ROOT / "sample.wav"
    if not path.exists():
        raise SystemExit(f"文件不存在: {path}\n用法: python test_asr.py <文件.wav> [--no-polish]")

    cfg = vi.load_cfg()
    audio = load_wav_16k(path)
    print(f"音频: {path.name}  {len(audio)/16000:.2f}s\n")

    t0 = time.time()
    raw = vi.transcribe(vi.to_wav(audio, 16000), cfg)
    t1 = time.time()
    print(f"[ASR {t1-t0:.2f}s]\n{raw}\n")

    if do_polish:
        try:
            t2 = time.time()
            out = vi.polish(raw, cfg)
            print(f"[润色 {time.time()-t2:.2f}s]\n{out}")
        except Exception as e:
            print(f"[润色失败] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
