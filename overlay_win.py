#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""悬浮状态条 —— Liquid Glass。

Win32 分层窗口 + 2 倍超采样 + 桌面截屏毛玻璃 + 外发光 / 投影 / 细长波形。
失败时自动降级成 Tk 版本。
"""
from __future__ import annotations

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
        try:
            from overlay_skia import SkiaOverlay
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
