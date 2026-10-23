#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""下载本项目需要的 GGUF 模型（断点续传 + 进度条 + HF 镜像）。

默认来源：
  ASR    : ggml-org/Qwen3-ASR-1.7B-GGUF   (~2.2 GB + 0.3 GB mmproj)
  润色    : unsloth/Qwen3.5-4B-GGUF       (~2.9 GB Q4_K_M)

用法：
  python download_models.py                  # 全部下载
  python download_models.py --only asr       # 只下识别模型
  python download_models.py --only polish    # 只下润色模型

国内网络慢可以走镜像（二选一）：
  set HF_ENDPOINT=https://hf-mirror.com
  python download_models.py --endpoint https://hf-mirror.com

下载到 models/ 目录，文件名与 config.toml 里的一致。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"

HF = "https://huggingface.co"

ASR_REPO = "ggml-org/Qwen3-ASR-1.7B-GGUF"
POLISH_REPO = "unsloth/Qwen3.5-4B-GGUF"

TARGETS = {
    "asr": [
        (f"{ASR_REPO}/resolve/main/Qwen3-ASR-1.7B-Q8_0.gguf",
         "Qwen3-ASR-1.7B-Q8_0.gguf", "ASR 识别模型 (Q8_0)"),
        (f"{ASR_REPO}/resolve/main/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf",
         "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf", "ASR 音频编码器 (mmproj)"),
    ],
    "polish": [
        (f"{POLISH_REPO}/resolve/main/Qwen3.5-4B-Q4_K_M.gguf",
         "Qwen3.5-4B-Q4_K_M.gguf", "润色模型 (Q4_K_M)"),
    ],
}


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024


def download(url: str, dest: Path, label: str) -> bool:
    part = dest.with_name(dest.name + ".part")

    if dest.exists() and not part.exists():
        print(f"[skip] {label}  已存在: {dest.name} ({human(dest.stat().st_size)})")
        return True

    start = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "voice-input/1.0"})
    if start:
        req.add_header("Range", f"bytes={start}-")
        print(f"[resume] {label}  从 {human(start)} 继续")

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status == 200 and start:      # 服务端不支持 Range，重下
                start = 0
                part.unlink(missing_ok=True)
            total = int(r.headers.get("Content-Length") or 0)
            total = total + start if r.status == 206 else total

            print(f"[get ] {label}  {url.split('/')[-1]}  {human(total) if total else '?'}")
            t0, last = time.time(), 0.0
            done = start
            with open(part, "ab" if start else "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last >= 0.4:
                        last = now
                        speed = (done - start) / max(1e-6, now - t0)
                        pct = f"{done * 100 / total:5.1f}%" if total else "  ?  "
                        eta = f"  ETA {(total - done) / max(1e-6, speed):.0f}s" if total and speed else ""
                        print(f"\r       {pct}  {human(done)} / {human(total) if total else '?'}"
                              f"  {human(speed)}/s{eta}", end="", flush=True)
            print()
            if total and done < total:
                print(f"[warn] {label} 不完整 ({human(done)}/{human(total)})，重跑可续传")
                return False
    except urllib.error.HTTPError as e:
        if e.code == 416:                      # 已经下完了
            pass
        else:
            print(f"\n[err ] {label}: HTTP {e.code} {e.reason}")
            return False
    except Exception as e:
        print(f"\n[err ] {label}: {type(e).__name__}: {e}")
        return False

    part.replace(dest)
    print(f"[ok  ] {label}  ->  {dest.relative_to(ROOT)}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 Voice Input 所需的 GGUF 模型")
    ap.add_argument("--only", choices=["asr", "polish"], help="只下载其中一组")
    ap.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT") or HF,
                    help="Hugging Face 地址，默认 https://huggingface.co")
    args = ap.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    groups = [args.only] if args.only else ["asr", "polish"]

    print(f"目标目录: {MODELS}")
    print(f"下载源  : {args.endpoint}\n")

    ok = True
    for group in groups:
        for path, name, label in TARGETS[group]:
            url = f"{args.endpoint.rstrip('/')}/{path}"
            ok = download(url, MODELS / name, label) and ok
            print()

    if ok:
        print("全部就绪。现在可以双击 start_all.bat 了。")
    else:
        print("有文件没下完，重跑本脚本可以断点续传。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
