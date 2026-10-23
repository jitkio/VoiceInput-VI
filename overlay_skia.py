#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""悬浮胶囊 —— Skia 渲染版（替代 Pillow）。

为什么换 Skia：
  * 亚像素抗锯齿 → 边缘不再有锯齿/毛刺
  * 真高斯模糊（MaskFilter / ImageFilter）→ 阴影柔和衰减，不是"降采样再放大"的假模糊
  * 多层渐变（linear + radial）→ 玻璃高光有方向、有衰减
  * 输出预乘 alpha → 正好是 UpdateLayeredWindow 要的格式（Pillow 版非预乘，边缘会发虚）
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

import skia

from overlay_win import (
    BLENDFUNCTION, DIB_RGB_COLORS, POINT, SIZE, SW_HIDE, SW_SHOWNOACTIVATE,
    WLW_ALPHA, WM_DESTROY, WM_TIMER, WNDCLASSW, WNDPROC, WS_EX_LAYERED,
    WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT,
    WS_POPUP, gdi32, kernel32, user32,
)

# ---------------------------------------------------------------- 配色

ACCENTS = {
    "recording": (255, 64, 54),
    "busy": (10, 132, 255),
    "done": (46, 196, 112),
    "error": (255, 159, 28),
}

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


class LevelMeter:
    """麦克风电平 + 自动增益（相对峰值归一 + 幂曲线 + 噪声门）。"""

    def __init__(self, n: int = 30):
        self._buf = deque([0.0] * n, maxlen=n)
        self._lock = threading.Lock()
        self._agc = 0.006

    def push(self, peak: float):
        v = min(1.0, max(0.0, peak))
        self._agc = max(v, self._agc * 0.985)
        with self._lock:
            self._buf.append(v)

    def snapshot(self):
        with self._lock:
            ref = max(self._agc, 1e-4)
            out = []
            for v in self._buf:
                r = v / ref
                out.append(0.0 if r < 0.06 else min(1.0, r ** 0.55))
            return out

    def clear(self):
        with self._lock:
            self._buf = deque([0.0] * self._buf.maxlen, maxlen=self._buf.maxlen)
            self._agc = 0.006


# ---------------------------------------------------------------- 几何 / 设计令牌

class Design:
    """所有设计参数集中在这里，改这个就能换风格。"""

    pill_w = 380.0          # 胶囊宽（逻辑像素）
    pill_h = 76.0           # 胶囊高
    pad = 34.0              # 窗口留白（给投影/发光）

    # 内容布局（相对胶囊左边界）
    dot_x = 34.0
    dot_r = 3.4
    bars_x0 = 56.0
    bars_right_pad = 92.0   # 右侧给黑圆留出的宽度
    bars_n = 26
    bar_w = 2.4
    bar_max_h = 28.0        # 波形最大高度（胶囊高的 ~37%）

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
        self._font_btn = skia.Font(self._tf_bold, 14.0 * s * self.SS)
        self._font_msg = skia.Font(self._tf_reg, 17.0 * s * self.SS)
        self._paint_cache = {}

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
        from overlay_win import BITMAPINFO, BITMAPINFOHEADER

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
        now = time.time()
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
                self._glass_img = None
                self._static = None
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
    def _render(self, now: float):
        ss = self.SS
        s = self.s * ss
        W, H = self.W * ss, self.H * ss
        if self._static is None:
            self._static = self._build_static()

        surf = skia.Surface(W, H)
        c = surf.getCanvas()
        c.clear(skia.ColorTRANSPARENT)
        c.drawImage(self._static, 0, 0)

        px, py = self.pad * ss, self.pad * ss
        pw, ph = self.pw * ss, self.ph * ss
        radius = self.d.radius * s
        body = self._rr(px, py, pw, ph, radius)
        cy = py + ph / 2

        accent = ACCENTS.get(self._state, ACCENTS["recording"])
        ink = C(26, 26, 30, 255)

        levels = self.meter.snapshot() if self._state == "recording" else []
        lv = levels[-1] if levels else 0.0
        self._smooth += (lv - self._smooth) * 0.3

        dr = self.d.dot_r * s * (1.0 + (0.08 * math.sin(now * 4.6)
                                        if self._state == "recording" else 0.0))
        dx = px + self.d.dot_x * s
        c.drawCircle(dx, cy, dr, skia.Paint(AntiAlias=True, Color=C(*accent, 255)))
        c.drawCircle(dx - dr * 0.30, cy - dr * 0.34, dr * 0.30,
                     skia.Paint(AntiAlias=True, Color=C(255, 255, 255, 115)))

        heights = self._heights(levels, now)
        if heights:
            c.save()
            c.clipRRect(body, True)
            x0 = px + self.d.bars_x0 * s
            x1 = px + pw - self.d.bars_right_pad * s
            self._draw_bars(c, heights, x0, x1, cy, s, ink, accent)
            c.restore()

        cr = self.d.circle_r * s
        ccx = px + pw - (self.d.circle_right_pad * s + cr)
        c.drawCircle(ccx, cy, cr, skia.Paint(AntiAlias=True, Color=C(16, 16, 18, 255)))
        ring = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                          StrokeWidth=1.2 * s)
        ring.setShader(skia.GradientShader.MakeLinear(
            [skia.Point(ccx - cr, cy - cr), skia.Point(ccx + cr, cy + cr)],
            [C(255, 255, 255, 55), C(255, 255, 255, 0)]))
        c.drawCircle(ccx, cy, cr - 0.8 * s, ring)

        if self._state == "recording":
            el = now - self._state_t
            self._center_text(c, "%d:%02d" % (int(el // 60), int(el % 60)),
                              ccx, cy, self._font_btn, C(255, 255, 255, 250))
        elif self._state == "busy":
            ar = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                            StrokeWidth=2.2 * s, Color=C(255, 255, 255, 235))
            ar.setStrokeCap(skia.Paint.kRound_Cap)
            a0 = (now * 400) % 360
            r = cr * 0.42
            c.drawArc(skia.Rect.MakeXYWH(ccx - r, cy - r, r * 2, r * 2),
                      a0, 265, False, ar)
        elif self._state == "done":
            ck = skia.Paint(AntiAlias=True, Style=skia.Paint.kStroke_Style,
                            StrokeWidth=2.4 * s, Color=C(255, 255, 255, 255))
            ck.setStrokeCap(skia.Paint.kRound_Cap)
            ck.setStrokeJoin(skia.Paint.kRound_Join)
            path = skia.Path()
            path.moveTo(ccx - 6.2 * s, cy + 0.4 * s)
            path.lineTo(ccx - 1.8 * s, cy + 4.6 * s)
            path.lineTo(ccx + 6.2 * s, cy - 4.8 * s)
            c.drawPath(path, ck)
        else:
            self._center_text(c, "!", ccx, cy, self._font_btn, C(255, 255, 255, 250))

        if self._state in ("done", "error"):
            msg = "已上屏" if self._state == "done" else (self._text or "出错了")
            painter_cx = (px + self.d.dot_x * s + ccx - cr) / 2
            f = self._font_msg
            while f.measureText(msg) > (ccx - cr - (px + self.d.dot_x * s) - 40 * s) and msg:
                msg = msg[:-1]
            if msg:
                self._center_text(c, msg, painter_cx, cy, f, ink)

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

    def _heights(self, levels, now):
        n = self.d.bars_n
        if self._state == "recording" and levels:
            seq = ([0.0] * n + levels)[-n:]
            seq = [v * (0.8 + 0.2 * (i / (n - 1))) for i, v in enumerate(seq)]
            sm = []
            for i in range(len(seq)):
                a = seq[max(0, i - 1)]
                b = seq[min(len(seq) - 1, i + 1)]
                sm.append(0.25 * a + 0.5 * seq[i] + 0.25 * b)
            return sm
        if self._state == "busy":
            return [0.16 + 0.30 * (0.5 + 0.5 * math.sin(i * 0.62 - now * 5.2))
                    for i in range(n)]
        return []

    def _draw_bars(self, c, heights, x0, x1, cy, s, ink, accent):
        n = len(heights)
        if n < 2:
            return
        pitch = (x1 - x0) / n
        bw = max(1.8 * s, self.d.bar_w * s)
        max_h = self.d.bar_max_h * s
        for i, v in enumerate(heights):
            cx = x0 + (i + 0.5) * pitch
            h = max(2.2 * s, v * max_h)
            t = i / max(1, n - 1)
            col = C(int(26 + accent[0] * 0.08), int(26 + accent[1] * 0.08),
                    int(30 + accent[2] * 0.08), int(150 + 105 * (t ** 0.6)))
            c.drawRRect(self._rr(cx - bw / 2, cy - h / 2, bw, h, bw / 2),
                        skia.Paint(AntiAlias=True, Color=col))

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
