#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""系统托盘：图标状态 + 右键菜单（暂停 / 自启 / 配置 / 日志 / 退出）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw

try:
    import pystray
except Exception:  # 没装就自动跳过，不影响主功能
    pystray = None

ROOT = Path(__file__).resolve().parent

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
