"""「正在播放」全屏页 —— 极光舞台（圆形封面 + 音符频谱环 + 歌词）复刻。

呈现层，不含业务：所有数据由 main.py 推进来（set_* / update），
所有用户动作以回调抛出去（on_close / on_play_pause / on_scrub）。

版式（自上而下，左右对称，用户要求）：
    标题 / 歌手 / 关闭钮
    ┌ 圆形封面（黑胶旋转）+ 外圈 96 根频谱条 ┐   ← 视觉重心 ~62%
    歌词窗口（当前行居中高亮，上下对称渐隐）
    时间 · 进度条 · 播放大圆钮

频谱条与封面的几何/配色/平滑逐行复刻自插件
stage.js drawFrame（见仓库 6ac321b3..._02-画面预设插件 目录，
已征得原作者同意）；Kivy 侧用 Mesh 顶点色三角带替代 Canvas 渐变线。
"""
import math

from kivy.animation import Animation
from kivy.clock import Clock
from kivy.graphics import (Color, Mesh, PushMatrix, PopMatrix, Rectangle,
                           Rotate, RoundedRectangle)
from kivy.metrics import dp
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.widget import Widget

import bgfx            # 渐变/光晕贴图工厂（纯装饰层，复用没问题）
import fonts            # 中文字体：本层每个文本控件自动带上（历史坑：漏传=方块）
from appenv import log_exc


def _spring(p):
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    return 1.0 - math.exp(-6.2 * p) * math.cos(10.4 * p)


# 极光原版频谱环配色（rgba 0..255 → 0..1）
_C_IN = (120 / 255.0, 180 / 255.0, 255 / 255.0, 0.85)
_C_MID = (180 / 255.0, 140 / 255.0, 255 / 255.0, 0.90)
_C_OUT = (120 / 255.0, 220 / 255.0, 255 / 255.0, 0.95)
BG_TOP = (0.086, 0.125, 0.290, 1)        # #16204a 极光夜空
BG_MID = (0.043, 0.055, 0.102, 1)        # #0b0e1a
BG_BOT = (0.020, 0.024, 0.047, 1)        # #05060c


class NowPlaying(FloatLayout):
    """全屏播放层。open()/close() 从底部弹簧滑入滑出。"""

    def __init__(self, on_close=None, on_play_pause=None, on_scrub=None, **kw):
        kw.setdefault("size_hint", (1, 1))
        super(NowPlaying, self).__init__(**kw)
        self._on_close = on_close
        self._on_play_pause = on_play_pause
        self._on_scrub = on_scrub
        self._gen = 0                 # 开合保险丝世代号（吸取 2.8.7 竞态教训）
        self.opened = False
        self._t = 0.0
        self._rot = 0.0               # 黑胶转角
        self._lyr_off = 0.0           # 歌词滚动平滑值
        self._lyr_target = 0.0
        self._bands = [0.0] * 96
        self._vis = 0.0               # 视觉化呼吸（无数据时全静）
        self._scrubbing = False
        self._pos_frac = 0.0

        # —— 背景：竖向渐变 + 两团极光 + 中心光晕 ——
        self._bg_tex = bgfx.make_vgradient_texture(BG_TOP, BG_BOT)
        self._glow_tex = bgfx.make_glow_texture()
        with self.canvas.before:
            self._bg_c = Color(1, 1, 1, 1)
            self._bg_r = Rectangle(texture=self._bg_tex)
            self._a1_c = Color(0.16, 0.34, 0.85, 0.20)
            self._a1_r = Rectangle(texture=self._glow_tex)
            self._a2_c = Color(0.42, 0.24, 0.85, 0.14)
            self._a2_r = Rectangle(texture=self._glow_tex)
            self._halo_c = Color(0.30, 0.42, 0.95, 0.22)
            self._halo_r = Rectangle(texture=self._glow_tex)
            # 封面圆（黑胶旋转组）
            self._cv_push = PushMatrix()
            self._cv_rot = Rotate(axis=(0, 0, 1))
            self._cv_c = Color(1, 1, 1, 1)
            self._cv_r = RoundedRectangle(radius=[dp(8)])
            self._cv_pop = PopMatrix()
            # 无封面占位：暗色圆盘 + 简化音符（圆头+杆）
            self._ph_c = Color(0.20, 0.24, 0.36, 0.9)
            self._ph_r = RoundedRectangle(radius=[dp(8)])
            self._nt_c = Color(0.62, 0.72, 0.98, 0.9)
            self._note_head = RoundedRectangle(radius=[dp(6)])
            self._note_stem = Rectangle()
            # 封面描边（原版 rgba .25 白）
            from kivy.graphics import Line
            self._edge_c = Color(1, 1, 1, 0.25)
            self._edge = Line(rounded_rectangle=(0, 0, 10, 10, dp(8)),
                              width=dp(1.2))
            # 频谱环 Mesh（triangles + 顶点色，每帧更新顶点缓冲）
            self._sp_c = Color(1, 1, 1, 1)
            self._sp_mesh = Mesh(mode="triangles")

        # —— 顶部标题 ——
        self.lbl_name = Label(font_size=dp(21), bold=True,
                              color=(1, 1, 1, 0.96), halign="center",
                              valign="middle", **fonts.font_kwargs())
        self.lbl_name.bind(size=self._fit_lbl)
        self.lbl_artist = Label(font_size=dp(14),
                                color=(1, 1, 1, 0.55), halign="center",
                                valign="middle", **fonts.font_kwargs())
        self.lbl_artist.bind(size=self._fit_lbl)
        self.add_widget(self.lbl_name)
        self.add_widget(self.lbl_artist)

        # —— 歌词行池（11 行：中 1 + 上下各 5）——
        self._lyr = []
        for i in range(11):
            lb = Label(font_size=dp(17), halign="center",
                       valign="middle", color=(1, 1, 1, 0.35),
                       **fonts.font_kwargs())
            lb.bind(size=self._fit_lbl)
            self._lyr.append(lb)
            self.add_widget(lb)

        # —— 底部：时间 + 进度条 + 播放钮 ——
        self.lbl_t1 = Label(font_size=dp(12), color=(1, 1, 1, 0.5),
                            halign="right", valign="middle", text="00:00",
                            **fonts.font_kwargs())
        self.lbl_t1.bind(size=self._fit_lbl)
        self.lbl_t2 = Label(font_size=dp(12), color=(1, 1, 1, 0.5),
                            halign="left", valign="middle", text="00:00",
                            **fonts.font_kwargs())
        self.lbl_t2.bind(size=self._fit_lbl)
        self.add_widget(self.lbl_t1)
        self.add_widget(self.lbl_t2)
        self.btn_close = _RoundGhost("close", on_press=lambda *_: self.close())
        self.add_widget(self.btn_close)
        self.btn_play = _RoundBig("play", on_press=lambda *_: self._tap_play())
        self.add_widget(self.btn_play)

        # 进度条触摸热区（透明 Widget 叠在底部条上）
        self._seek_zone = Widget()
        self._seek_zone.apply_transform = lambda *a: None
        self.add_widget(self._seek_zone)

        self.bind(pos=self._relayout, size=self._relayout)
        self._lines = []
        self._cur_line = -1
        Clock.schedule_interval(self._tick_paint, 1.0 / 30.0)  # 装饰呼吸自转

    # ================= 对外 API（main.py 调用） =================
    def open(self):
        if self.opened:
            return
        self.opened = True
        self._gen += 1
        g = self._gen
        # 开/关共享 y 属性：不清掉上一个动画，两个动画会互相拉扯同一个
        # 属性（Kivy Animation 不指定 id 就不会自动互斥 —— 测试实测卡死）
        Animation.cancel_all(self)
        self.y = -self.height
        self.opacity = 1.0
        Animation(y=0, d=0.52, t=_spring).start(self)
        Clock.schedule_once(lambda *_: self._fuse_y(0, g), 1.0)

    def close(self):
        if not self.opened:
            return
        self.opened = False
        self._gen += 1
        g = self._gen
        Animation.cancel_all(self)
        Animation(y=-self.height, d=0.34, t="out_cubic").start(self)
        Clock.schedule_once(lambda *_: self._fuse_y(-self.height, g), 0.8)
        try:
            if self._on_close:
                self._on_close()
        except Exception:
            log_exc("np on_close")

    def _fuse_y(self, y, g):
        if self._gen == g:
            self.y = y

    def set_song(self, name, artist):
        self.lbl_name.text = _esc(name or "未知歌曲")
        self.lbl_artist.text = _esc(artist or "")

    def set_cover(self, texture):
        """texture=None → 占位圆（音符）；否则画进旋转黑胶"""
        self._cv_tex = texture
        self._relayout()

    def set_playing(self, on):
        self.btn_play.set_kind("pause" if on else "play")

    def set_lyrics(self, lines):
        """[(秒, 词)]，来自 songmedia.parse_lrc"""
        self._lines = list(lines or [])
        self._cur_line = -1
        self._lyr_off = 0.0
        self._lyr_target = 0.0

    def set_progress(self, pos, dur, bands=None):
        """pos/dur 秒；bands=None 时谱条向 0 缓落（释放慢，同原版手感）"""
        if dur and dur > 0:
            self._pos_frac = max(0.0, min(1.0, pos / float(dur)))
        self.lbl_t1.text = _fmt(pos)
        self.lbl_t2.text = _fmt(dur or 0)
        if bands:
            self._bands = bands
        else:
            for i, v in enumerate(self._bands):
                self._bands[i] = v * 0.90       # 释放慢
        # 歌词当前行
        if self._lines:
            idx = -1
            for i, (t, _w) in enumerate(self._lines):
                if t <= pos + 0.05:
                    idx = i
                else:
                    break
            if idx != self._cur_line:
                self._cur_line = idx
                self._lyr_target = idx * self._line_h()
        self._relayout_bottom()

    def set_bands_from_user(self, bands):
        self._bands = bands or self._bands

    # ================= 内部 =================
    def _tap_play(self):
        try:
            if self._on_play_pause:
                self._on_play_pause()
        except Exception:
            log_exc("np play")

    def on_touch_down(self, touch):
        if (self._seek_zone.collide_point(*touch.pos)
                and touch.pos[1] < self._seek_zone.top):
            self._scrubbing = True
            touch.grab(self)
            self._do_scrub(touch)
            return True
        return super(NowPlaying, self).on_touch_down(touch)

    def on_touch_move(self, touch):
        if self._scrubbing:
            self._do_scrub(touch)
            return True
        return super(NowPlaying, self).on_touch_move(touch)

    def on_touch_up(self, touch):
        if self._scrubbing:
            self._scrubbing = False
            try:
                touch.ungrab(self)
            except Exception:
                pass
            try:
                if self._on_scrub:
                    self._on_scrub(self._pos_frac)
            except Exception:
                log_exc("np scrub")
            return True
        return super(NowPlaying, self).on_touch_up(touch)

    def _do_scrub(self, touch):
        x0 = self._seek_x0()
        w = self._seek_w()
        if w > 0:
            self._pos_frac = max(0.0, min(1.0, (touch.x - x0) / w))
            self._relayout_bottom()

    def _fit_lbl(self, inst, sz):
        inst.text_size = (sz[0], None)

    def _line_h(self):
        return dp(34)

    def _cx(self):
        return self.x + self.width / 2.0

    def _cy(self):
        return self.y + self.height * 0.63

    def _cover_r(self):
        base = min(self.width, self.height) / 2.5
        return base * 0.5 * 0.62

    def _ring(self):
        """(ringInner, ringOuter) 复刻 stage.js 几何"""
        r = self._cover_r()
        inner = r + min(self.width, self.height) / 2.5 * 0.04
        outer = inner + min(self.width, self.height) / 2.5 * 0.14
        return inner, outer

    def _seek_y(self):
        return self.y + self.height * 0.155

    def _seek_x0(self):
        return self.x + dp(52)

    def _seek_w(self):
        return max(1.0, self.width - dp(104))

    def _relayout(self, *_):
        if self.width < 2:
            return
        cx, cy = self._cx(), self._cy()
        w, h = self.width, self.height
        # 背景与光团
        self._bg_r.pos = (self.x, self.y)
        self._bg_r.size = (w, h)
        import time as _t
        ph = _t.time()
        for i, (rc, rr) in enumerate(((self._a1_c, self._a1_r),
                                      (self._a2_c, self._a2_r))):
            bw = w * (0.9 if i == 0 else 1.05)
            bh = h * 0.55
            rc.a = (0.13 if i == 0 else 0.10) + 0.06 * self._vis
            rr.pos = (cx + math.sin(ph * 0.11 + i * 2.1) * w * 0.18 - bw / 2.0,
                      cy + (h * 0.10 if i == 0 else -h * 0.30) - bh / 2.0)
            rr.size = (bw, bh)
        # 封面光晕（原版 glowR=1.35R）
        g = self._cover_r() * 1.35 * 2
        self._halo_c.a = 0.16 + 0.10 * self._vis
        self._halo_r.pos = (cx - g / 2.0, cy - g / 2.0)
        self._halo_r.size = (g, g)
        # 封面圆
        r = self._cover_r()
        tex = getattr(self, "_cv_tex", None)
        if tex is not None:
            self._cv_r.texture = tex
            self._cv_c.a = 1.0
            self._cv_r.pos = (cx - r, cy - r)
            self._cv_r.size = (r * 2, r * 2)
            self._cv_r.radius = [r]
            self._ph_r.size = (0, 0)
            self._note_head.size = (0, 0)
            self._note_stem.size = (0, 0)
        else:
            self._cv_r.size = (0, 0)
            self._ph_r.pos = (cx - r, cy - r)
            self._ph_r.size = (r * 2, r * 2)
            self._ph_r.radius = [r]
            hr = r * 0.30
            self._note_head.pos = (cx - hr * 1.5, cy - hr * 1.2)
            self._note_head.size = (hr * 1.8, hr * 1.4)
            self._note_head.radius = [hr * 0.6]
            self._note_stem.pos = (cx + hr * 0.15, cy - hr * 0.4)
            self._note_stem.size = (hr * 0.28, r * 1.1)
        # 描边（Line.rounded_rectangle = x, y, w, h, radius）
        self._edge.rounded_rectangle = (cx - r, cy - r, r * 2, r * 2, r)
        self._edge.width = dp(1.2)
        # 顶部
        self.lbl_artist.size_hint = (None, None)
        self.lbl_artist.size = (w - dp(140), dp(20))
        self.lbl_artist.pos = (self.x + dp(70), self.top - dp(64))
        self.lbl_name.size_hint = (None, None)
        self.lbl_name.size = (w - dp(140), dp(30))
        self.lbl_name.pos = (self.x + dp(70), self.top - dp(94))
        self.btn_close.pos = (self.x + dp(18), self.top - dp(76))
        # 歌词区中心（封面下方与底部控制之间的对称位）
        # 底部控件
        self.btn_play.size = (dp(64), dp(64))
        self.btn_play.pos = (cx - dp(32), self.y + self.height * 0.035)
        self.lbl_t1.size_hint = (None, None)
        self.lbl_t1.size = (dp(44), dp(20))
        self.lbl_t1.pos = (self._seek_x0() - dp(52), self._seek_y() - dp(10))
        self.lbl_t2.size_hint = (None, None)
        self.lbl_t2.size = (dp(44), dp(20))
        self.lbl_t2.pos = (self._seek_x0() + self._seek_w() + dp(8),
                           self._seek_y() - dp(10))
        self._seek_zone.pos = (self._seek_x0() - dp(6), self._seek_y() - dp(16))
        self._seek_zone.size = (self._seek_w() + dp(12), dp(36))
        # 歌词位置（在封面与 seek 之间）
        top = cy - r - min(self.width, self.height) / 2.5 * 0.14 - dp(46)
        self._lyr_top = top
        # 频谱 + 歌词内容刷新
        self._update_spectrum()
        self._update_lyrics()
        self._update_track()

    def _relayout_bottom(self):
        self._update_track()

    # —— 频谱环 Mesh ——
    def _update_spectrum(self):
        inner, outer = self._ring()
        cx, cy = self._cx(), self._cy()
        nb = len(self._bands)
        n = max(24, min(180, nb))
        half_gap = math.pi / n
        verts, idx = [], []
        step = (math.pi * 2) / n
        vmin = 0.15
        for b in range(n):
            h = self._bands[b] if b < len(self._bands) else 0.0
            barlen = (outer - inner) * (vmin + max(0.0, min(1.0, h)) * 0.85)
            ang = b * step - math.pi / 2.0
            ca, sa = math.cos(ang), math.sin(ang)
            nx, ny = -sa, ca                       # 切向半宽方向
            wa = half_gap * inner * 0.62
            wb = half_gap * (inner + barlen) * 0.30
            x0, y0 = cx + ca * inner, cy + sa * inner
            x1, y1 = cx + ca * (inner + barlen), cy + sa * (inner + barlen)
            p1 = (x0 + nx * wa, y0 + ny * wa)
            p2 = (x0 - nx * wa, y0 - ny * wa)
            p3 = (x1 + nx * wb, y1 + ny * wb)
            p4 = (x1 - nx * wb, y1 - ny * wb)
            base = len(verts)
            # 4 顶点 2 三角形：内→外 顶点色三段混合近似渐变
            for (px, py), col in ((p1, _C_IN), (p2, _C_IN),
                                  (p3, _C_OUT), (p4, _C_OUT)):
                verts += (px, py, 0, 0, col[0], col[1], col[2], col[3])
            idx += [base, base + 1, base + 2, base + 1, base + 3, base + 2]
        try:
            self._sp_mesh.vertices = verts
            self._sp_mesh.indices = idx
        except Exception:
            log_exc("spectrum mesh")

    # —— 歌词 ——
    def _update_lyrics(self):
        self._lyr_off += (self._lyr_target - self._lyr_off) * 0.16
        top = getattr(self, "_lyr_top", self._cy())
        lines = self._lines
        for k, lb in enumerate(self._lyr):
            slot = k - 5
            i = self._cur_line + slot
            lh = self._line_h()
            y = top - lh / 2.0 - (self._lyr_off + slot * lh)
            lb.size_hint = (None, None)
            lb.size = (self.width - dp(48), lh)
            lb.pos = (self.x + dp(24), y)
            d = abs(slot)
            if 0 <= i < len(lines):
                lb.text = _esc(lines[i][1])
                if slot == 0:
                    lb.color = (1, 1, 1, 0.96)
                    lb.font_size = dp(18)
                    lb.bold = True
                else:
                    lb.color = (0.80, 0.85, 1.0, max(0.12, 0.55 - d * 0.09))
                    lb.font_size = dp(16)
                    lb.bold = False
            else:
                lb.text = ""

    # —— 进度条 ——
    def _update_track(self):
        f = self._pos_frac
        x0, w, y = self._seek_x0(), self._seek_w(), self._seek_y()
        th = dp(4)
        # track 复用 bg rectangle 组（独立 Color 略），用 edge 之外新增
        if not hasattr(self, "_tk_done"):
            with self.canvas.after:
                self._tkt_c = Color(1, 1, 1, 0.28)
                self._tkt = RoundedRectangle(radius=[dp(2)])
                self._tkf_c = Color(0.36, 0.55, 1.0, 1)
                self._tkf = RoundedRectangle(radius=[dp(2)])
                self._tkn_c = Color(1, 1, 1, 1)
                self._tkn = RoundedRectangle(radius=[dp(9)])
            self._tk_done = True
        self._tkt.pos = (x0, y - th / 2.0)
        self._tkt.size = (w, th)
        self._tkf.pos = (x0, y - th / 2.0)
        self._tkf.size = (max(th, w * f), th)
        kx = x0 + w * f
        self._tkn.pos = (kx - dp(9), y - dp(9))
        self._tkn.size = (dp(18), dp(18))

    # —— 自转呼吸（封面持续旋转 + 无数据时的静默） ——
    def _tick_paint(self, dt):
        self._t += dt
        try:
            peak = max(self._bands) if self._bands else 0.0
        except Exception:
            peak = 0.0
        self._vis += (peak - self._vis) * 0.08
        rot_speed = 2.0 / 60.0 * 2 * math.pi        # 圈/分钟 → rad/s
        self._rot += rot_speed * dt
        try:
            self._cv_rot.angle = math.degrees(self._rot) % 360
            self._cv_rot.origin = (self._cx(), self._cy(), 0)
        except Exception:
            pass
        if self.opened:
            self._update_spectrum()
            self._update_lyrics()


def _esc(s):
    return s or ""


def _fmt(sec):
    try:
        sec = int(max(0, sec))
        return "%02d:%02d" % (sec // 60, sec % 60)
    except Exception:
        return "00:00"


class _RoundGhost(Widget):
    """顶部关闭钮：半透明白圆 + 叉（按钮型，自绘命中）"""
    def __init__(self, kind, on_press=None, **kw):
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dp(40), dp(40)))
        super(_RoundGhost, self).__init__(**kw)
        self._on_press = on_press
        self._kind = kind
        from kivy.graphics import Line
        with self.canvas:
            self._bgc = Color(1, 1, 1, 0.14)
            self._bg = RoundedRectangle(radius=[dp(20)])
            self._lc = Color(1, 1, 1, 0.85)
            self._ln = Line(points=[], width=dp(1.8))
        self.bind(pos=self._draw, size=self._draw)
        self._draw()

    def _draw(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size
        cx = self.x + self.width / 2.0
        cy = self.y + self.height / 2.0
        s = min(self.width, self.height) * 0.22
        self._ln.points = [cx - s, cy - s, cx + s, cy + s,
                           cx - s, cy + s, cx + s, cy - s]
        self._ln.width = dp(1.8)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self._down = True
            self._bgc.rgba = (1, 1, 1, 0.28)
            return True
        return False

    def on_touch_up(self, touch):
        if getattr(self, "_down", False):
            self._down = False
            self._bgc.rgba = (1, 1, 1, 0.14)
            if self.collide_point(*touch.pos) and self._on_press:
                try:
                    self._on_press()
                except Exception:
                    log_exc("_RoundGhost")
            return True
        return False


class _RoundBig(Widget):
    """播放大圆钮：极光蓝底 + play/pause 图形（mesh 三角）"""
    def __init__(self, kind, on_press=None, **kw):
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dp(64), dp(64)))
        super(_RoundBig, self).__init__(**kw)
        self._on_press = on_press
        self._kind = kind
        from kivy.graphics import Mesh
        with self.canvas:
            self._gl_c = Color(0.30, 0.45, 1.0, 0.35)
            self._gl = Rectangle()
            self._bg_c = Color(0.24, 0.42, 1.0, 1)
            self._bg = RoundedRectangle(radius=[dp(32)])
            self._ic_c = Color(1, 1, 1, 0.98)
            self._mesh = Mesh(mode="triangles", vertices=[], indices=[])
        self.bind(pos=self._draw, size=self._draw)
        self._draw()

    def set_kind(self, k):
        self._kind = k
        self._draw()

    def _draw(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._bg.radius = [min(self.width, self.height) / 2.0]
        g = min(self.width, self.height) * 1.9
        self._gl.pos = (self.x + (self.width - g) / 2.0,
                        self.y + (self.height - g) / 2.0)
        self._gl.size = (g, g)
        cx = self.x + self.width / 2.0
        cy = self.y + self.height / 2.0
        u = min(self.width, self.height) * 0.30
        v, idx = [], []
        if self._kind == "play":
            p = [(cx - u * 0.55, cy + u), (cx - u * 0.55, cy - u),
                 (cx + u * 0.85, cy)]
            v = [p[0][0], p[0][1], 0, 0, 1, 1, 1, 1,
                 p[1][0], p[1][1], 0, 0, 1, 1, 1, 1,
                 p[2][0], p[2][1], 0, 0, 1, 1, 1, 1]
            idx = [0, 1, 2]
        else:
            bw = u * 0.42
            for dx in (-u * 0.62, u * 0.18):
                base = len(idx)
                x0, x1 = cx + dx, cx + dx + bw
                y0, y1 = cy - u * 1.05, cy + u * 1.05
                v += [x0, y0, 0, 0, 1, 1, 1, 1,
                      x1, y0, 0, 0, 1, 1, 1, 1,
                      x1, y1, 0, 0, 1, 1, 1, 1,
                      x0, y0, 0, 0, 1, 1, 1, 1,
                      x1, y1, 0, 0, 1, 1, 1, 1,
                      x0, y1, 0, 0, 1, 1, 1, 1]
                idx += [base, base + 1, base + 2,
                        base + 3, base + 4, base + 5]
        try:
            self._mesh.vertices = v
            self._mesh.indices = idx
        except Exception:
            pass

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self._down = True
            self._bg_c.rgba = (0.16, 0.32, 0.9, 1)
            self._bg.size = (self.width * 0.94, self.height * 0.94)
            self._bg.pos = (self.x + (self.width - self.width * 0.94) / 2.0,
                            self.y + (self.height - self.height * 0.94) / 2.0)
            return True
        return False

    def on_touch_up(self, touch):
        if getattr(self, "_down", False):
            self._down = False
            self._draw()
            if self.collide_point(*touch.pos) and self._on_press:
                try:
                    self._on_press()
                except Exception:
                    log_exc("_RoundBig")
            return True
        return False
