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
已征得原作者同意）；Kivy 侧用 Fbo+矩阵指令树复刻（零顶点重建）。
"""
import math

from kivy.animation import Animation
from kivy.clock import Clock
from kivy.graphics import (Color, Rectangle, RoundedRectangle, Fbo, PushMatrix, PopMatrix, Translate, Rotate, Scale, ClearColor, ClearBuffers, Line)
from kivy.metrics import dp
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.widget import Widget

import bgfx            # 渐变/光晕贴图工厂（纯装饰层，复用没问题）
import fonts            # 中文字体：本层每个文本控件自动带上（历史坑：漏传=方块）
from appenv import log_exc


def _bar_color(t):
    """内→外 三段渐变：(120,180,255)→(180,140,255)→(120,220,255)"""
    a, b = _C_IN, _C_MID
    f = t
    if f > 0.6:
        a, b, f = _C_MID, _C_OUT, (f - 0.6) / 0.4
    else:
        f = f / 0.6
    return (int((a[0] + (b[0] - a[0]) * f) * 255),
            int((a[1] + (b[1] - a[1]) * f) * 255),
            int((a[2] + (b[2] - a[2]) * f) * 255),
            int((a[3] + (b[3] - a[3]) * f) * 255))


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

    def __init__(self, on_close=None, on_play_pause=None, on_scrub=None,
                 on_retry_vis=None, **kw):
        kw.setdefault("size_hint", (1, 1))
        super(NowPlaying, self).__init__(**kw)
        self._on_close = on_close
        self._on_play_pause = on_play_pause
        self._on_scrub = on_scrub
        self._on_retry_vis = on_retry_vis
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
        # 「舞台」= 封面圆+光晕+频谱环+描边 全部画进一张固定 RGBA 纹理，
        # 每帧 blit_buffer 原地重传（glTexSubImage2D）——**不新建任何顶点缓冲、
        # 不做临界圆角 tessellation**。真机 2.9.1 的白线放射爆炸（Mesh 每帧
        # 重建缓冲时旧索引读到未初始化原点顶点）到此根治：绘制只剩一个
        # 固定位置的 Rectangle，桌面/手机驱动行为完全一致。
        self._STAGE_PX = 512                     # Fbo 边长
        self._stage_fbo = Fbo(size=(self._STAGE_PX, self._STAGE_PX))
        PX = self._STAGE_PX
        C2 = (PX - 1) / 2.0
        with self._stage_fbo:
            ClearColor(0, 0, 0, 0)
            ClearBuffers()
            # 光晕：6 圈固定 glow 矩形（尺寸=封面光晕带，layout 时更新 size）
            self._halos = []
            for gi in range(6):
                c = Color(0.59, 0.67, 1.0, 0.05)
                r = Rectangle(texture=self._glow_tex)
                self._halos.append((c, r))
            # 封面（黑胶）：push→translate(心)→rotate→圆纹理→pop
            self._cv_push = PushMatrix()
            self._cv_tr = Translate(C2, C2, 0)
            self._cv_rot = Rotate(axis=(0, 0, 1))
            self._cv_c = Color(1, 1, 1, 1)
            self._cv_r = RoundedRectangle(radius=[PX * 0.12])
            self._cv_pop = PopMatrix()
            # 占位圆盘（无封面时露出）
            self._ph_c = Color(0.20, 0.24, 0.36, 0.9)
            self._ph_r = RoundedRectangle(radius=[PX * 0.12])
            # 封面白描边：Line circle（仅 layout 时改半径）
            self._ed_c = Color(1, 1, 1, 0.25)
            self._edge = Line(width=2.0)
            # 96 根谱条：每根 [push, translate(心), rotate(角), scale(长),
            # 固定基准矩形(1×1 沿 +x)] pop —— 播放期**只改 rotate.angle 与
            # scale.x**，零顶点重建（真机 2.9.1 白线爆炸的根治）
            self._bars = []
            n = 96
            for b in range(n):
                push = PushMatrix()
                tr = Translate(C2, C2, 0)
                rot = Rotate(axis=(0, 0, 1))
                sc = Scale(1, 1, 1)
                col = Color(0.47, 0.71, 1.0, 0.9)
                rect = Rectangle(size=(1, 1))       # 基准 1×1，scale 拉长度与粗细
                pop = PopMatrix()
                self._bars.append({"push": push, "tr": tr, "rot": rot,
                                   "sc": sc, "col": col, "rect": rect,
                                   "pop": pop, "ang": -90.0 + b * 360.0 / n})
        with self.canvas.before:
            self._bg_c = Color(1, 1, 1, 1)
            self._bg_r = Rectangle(texture=self._bg_tex)
            self._a1_c = Color(0.16, 0.34, 0.85, 0.20)
            self._a1_r = Rectangle(texture=self._glow_tex)
            self._a2_c = Color(0.42, 0.24, 0.85, 0.14)
            self._a2_r = Rectangle(texture=self._glow_tex)
            self._st_c = Color(1, 1, 1, 1)
            self._st_r = Rectangle(texture=self._stage_fbo.texture)

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
        # 右上「音波」小圆钮：点一下重新申请频谱权限；
        # 亮=真频谱在跳，暗=兜底律动 —— 数据源状态一眼可见
        self.btn_wave = _RoundGhost("wave", on_press=self._tap_wave)
        self.add_widget(self.btn_wave)
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
        """封面纹理直接挂进舞台 Fbo 的旋转圆；None → 占位圆盘"""
        try:
            if texture is not None:
                self._cv_r.texture = texture
                self._cv_c.a = 1.0
                self._ph_r.size = (0, 0)
            else:
                self._cv_r.size = (0, 0)
                self._ph_c.a = 0.9
                self._ph_r.size = self._cv_size_hint
        except Exception:
            log_exc("set_cover")
        self._stage_dirty = True

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
        self._stage_dirty = True
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
    def _tap_wave(self, *_):
        try:
            if self._on_retry_vis:
                self._on_retry_vis()
        except Exception:
            log_exc("np retry_vis")

    def set_visualizer_live(self, on):
        # 亮/暗 = 真频谱/兜底律动
        try:
            self.btn_wave.set_active(bool(on))
        except Exception:
            pass

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
        # 舞台方形：边长 = 环外半径 × 2.35，位置围绕 (cx, cy)
        _, outer = self._ring()
        side = max(4.0, outer * 2.35)
        self._st_side = side
        self._st_r.pos = (cx - side / 2.0, cy - side / 2.0)
        self._st_r.size = (side, side)
        self._stage_dirty = True
        # 顶部
        self.lbl_artist.size_hint = (None, None)
        self.lbl_artist.size = (w - dp(140), dp(20))
        self.lbl_artist.pos = (self.x + dp(70), self.top - dp(64))
        self.lbl_name.size_hint = (None, None)
        self.lbl_name.size = (w - dp(140), dp(30))
        self.lbl_name.pos = (self.x + dp(70), self.top - dp(94))
        self.btn_close.pos = (self.x + dp(18), self.top - dp(76))
        self.btn_wave.pos = (self.right - dp(58), self.top - dp(76))
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
        top = cy - self._cover_r() - min(self.width, self.height) / 2.5 \
            * 0.14 - dp(46)
        self._lyr_top = top
        # 频谱（舞台纹理）+ 歌词内容刷新
        self._render_stage()
        self._update_lyrics()
        self._update_track()

    def _relayout_bottom(self):
        self._update_track()

    # —— 舞台 Fbo：每帧只改矩阵/颜色参数，然后 fbo.draw() ——
    _SP_N = 96

    def _render_stage(self):
        """把 96 根谱条的长度/颜色、封面转角/半径、光晕强度写进静态指令树，
        再渲染 Fbo。**全程零顶点缓冲重建**：angle/scale/alpha 都是矩阵与
        状态参数（glUniform 级），不触发任何 buffer 分配 —— 2.9.1 真机
        '白线放射到左下角'（Mesh 每帧重建缓冲 + 异步分配读到原点顶点）
        的根治方案。"""
        try:
            inner, outer = self._ring()
            if outer <= inner:
                return
            PX = self._STAGE_PX
            C2 = (PX - 1) / 2.0
            # Fbo 纹理是 512px 方形；世界 side 与之对应
            side = getattr(self, "_st_side", 1.0)
            k = (PX - 1.0) / max(1.0, side)        # 世界 px → Fbo px
            r_in = inner * k
            r_cov = self._cover_r() * k
            band = (outer - inner) * k
            thick = max(3.0, r_in * 0.055)
            # 封面圆 + 占位盘 + 描边 + 光晕（随 layout 变化的静态几何）
            d_cov = max(2.0, r_cov * 2.0)
            self._cv_size_hint = (d_cov, d_cov)
            self._cv_r.size = (d_cov, d_cov)
            self._cv_r.pos = (C2 - r_cov, C2 - r_cov)
            self._cv_r.radius = [d_cov / 2.0 * 0.99]
            self._ph_r.size = self._cv_r.size
            self._ph_r.pos = self._cv_r.pos
            self._ph_r.radius = self._cv_r.radius
            self._edge.circle = (C2, C2, max(2.0, r_cov + 1), 2.0, 64)
            self._cv_tr.xyz = (C2, C2, 0)
            self._cv_rot.angle = math.degrees(self._rot) % 360.0
            ga = 0.10 + 0.20 * self._vis
            for gi, (col, rect) in enumerate(self._halos):
                rr = d_cov * (0.56 + 0.075 * gi) * 2.0
                rect.size = (rr, rr)
                rect.pos = (C2 - rr / 2.0, C2 - rr / 2.0)
                col.a = ga * (1.0 - gi / 6.0) * 0.5
            # 96 根谱条：角度固定；长度=scale.x、粗细=scale.y、色随高度插值
            bands = self._bands
            nb = len(bands)
            for b, g in enumerate(self._bars):
                h = bands[b] if b < nb else 0.0
                try:
                    h = float(h)
                except Exception:
                    h = 0.0
                if h != h:
                    h = 0.0                       # NaN 自检
                h = 0.0 if h < 0.0 else (1.0 if h > 1.0 else h)
                L = max(thick, band * (0.15 + 0.85 * h))
                g["tr"].xyz = (C2, C2, 0)
                g["rot"].angle = g["ang"]
                g["sc"].x = L
                g["sc"].y = thick
                g["rect"].pos = (r_in, -thick / 2.0)
                t = h
                # 三段渐变（同极光原版 120,180,255 → 180,140,255 → 120,220,255）
                if t < 0.6:
                    f = t / 0.6
                    r_ = (120 + 60 * f) / 255.0
                    gg = (180 - 40 * f) / 255.0
                    bb = 1.0
                else:
                    f = (t - 0.6) / 0.4
                    r_ = (180 - 60 * f) / 255.0
                    gg = (140 + 80 * f) / 255.0
                    bb = 1.0
                g["col"].rgba = (r_, gg, bb, 0.55 + 0.45 * h)
            self._stage_fbo.draw()
            self._stage_dirty = False
        except Exception:
            log_exc("_render_stage")

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

    # —— 进度条：独立小 Fbo（512×64），静态几何 + scale/translate，
    #    和舞台同款零重建（滑块被白线波及的那条也根治）——
    _PB_PX = 512
    _PB_PY = 64

    def _pb_ensure(self):
        if hasattr(self, "_pb_fbo"):
            return True
        try:
            self._pb_fbo = Fbo(size=(self._PB_PX, self._PB_PY))
            H2 = self._PB_PY / 2.0
            with self._pb_fbo:
                ClearColor(0, 0, 0, 0)
                ClearBuffers()
                self._tk_c = Color(1, 1, 1, 0.28)
                self._tkt = RoundedRectangle(radius=[dp(3)])
                self._tkt.pos = (8, H2 - 3)
                self._tkt.size = (self._PB_PX - 16, 6)
                # fill：push+translate 原点+scale 拉宽度+圆角底 rect+pop
                self._f_push = PushMatrix()
                self._f_tr = Translate(8, 0, 0)
                self._f_sc = Scale(1, 1, 1)
                self._f_c = Color(0.36, 0.55, 1.0, 1)
                self._fk = RoundedRectangle(radius=[dp(3)])
                self._fk.pos = (0, H2 - 3)
                self._fk.size = (self._PB_PX - 16, 6)
                self._f_pop = PopMatrix()
                # 滑块：固定白圆，translate 跟进度走
                self._k_c = Color(1, 1, 1, 1)
                self._k = RoundedRectangle(radius=[dp(9)])
                self._k.pos = (8 - 9, H2 - 9)
                self._k.size = (18, 18)
            with self.canvas.after:
                self._pbc_c = Color(1, 1, 1, 1)
                self._pbc_r = Rectangle(texture=self._pb_fbo.texture)
            return True
        except Exception:
            log_exc("_pb_ensure")
            return False

    def _update_track(self):
        if not self._pb_ensure():
            return
        x0, w, y = self._seek_x0(), self._seek_w(), self._seek_y()
        self._pbc_r.pos = (x0 - dp(6), y - dp(15))
        self._pbc_r.size = (w + dp(12), dp(30))
        H2 = self._PB_PY / 2.0
        f = max(0.0, min(1.0, self._pos_frac))
        usable = self._PB_PX - 32
        self._f_sc.x = max(0.001, usable * f / max(1.0, usable)) \
            if f > 0.001 else 0.001
        self._fk.pos = (0, H2 - 3)
        self._k.pos = (8 + usable * f - 9, H2 - 9)
        try:
            self._pb_fbo.draw()
        except Exception:
            log_exc("pb draw")

    # —— 自转呼吸（封面持续旋转 + 无数据时的静默） ——
    def _tick_paint(self, dt):
        self._t += dt
        try:
            peak = max(self._bands) if self._bands else 0.0
        except Exception:
            peak = 0.0
        self._vis += (peak - self._vis) * 0.08
        self._rot += (2.0 / 60.0) * (2 * math.pi) * dt   # 黑胶 2 圈/分
        if self.opened:
            self._stage_dirty = True
            self._update_lyrics()
            if self._stage_dirty:
                self._render_stage()


def _esc(s):
    return s or ""


def _fmt(sec):
    try:
        sec = int(max(0, sec))
        return "%02d:%02d" % (sec // 60, sec % 60)
    except Exception:
        return "00:00"


class _RoundGhost(Widget):
    """顶部按钮：半透明白圆底 + 静态几何（wave/close）。
    位置变化**只挪 Translate**，不重建任何顶点缓冲（防驱动竞态）。"""

    def __init__(self, kind, on_press=None, **kw):
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dp(40), dp(40)))
        super(_RoundGhost, self).__init__(**kw)
        self._on_press = on_press
        self._kind = kind
        from kivy.graphics import (Line, PopMatrix, PushMatrix, Translate)
        with self.canvas:
            self._bgc = Color(1, 1, 1, 0.14)
            self._bg = RoundedRectangle(radius=[dp(20)])
            self._grp = PushMatrix()
            self._tr = Translate(0, 0, 0)
            self._lc = Color(1, 1, 1, 0.85)
            self._ln = Line(points=[], width=dp(1.8))
            self._pop = PopMatrix()
        self.bind(size=self._layout_bg, pos=self._move_only)
        self._layout_bg()
        self._build_glyph()

    def _layout_bg(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._build_glyph()

    def _move_only(self, *_):
        # 圆底跟位置；几何是局部坐标，只需平移
        self._bg.pos = self.pos
        try:
            self._tr.xyz = (self.x, self.y, 0)
        except Exception:
            pass

    def _build_glyph(self):
        """局部坐标 (0..size) 画一次；pos 变化不再进来"""
        u = min(self.width, self.height)
        if u < 4:
            return
        cx, cy = u / 2.0, u / 2.0
        if self._kind == "wave":
            pts = []
            for dx, hh in ((-0.24, 0.20), (0.0, 0.34), (0.24, 0.26)):
                pts += [cx + u * dx, cy - u * hh, cx + u * dx, cy + u * hh]
            self._ln.points = pts
            self._ln.width = dp(2.2)
        else:
            g = u * 0.22
            self._ln.points = [cx - g, cy - g, cx + g, cy + g,
                               cx - g, cy + g, cx + g, cy - g]
            self._ln.width = dp(1.8)
        try:
            self._tr.xyz = (self.x, self.y, 0)
            self._lc.a = getattr(self, "_alpha", 0.85)
        except Exception:
            pass

    def set_active(self, on):
        self._alpha = 1.0 if on else 0.45
        try:
            self._lc.a = self._alpha
        except Exception:
            pass

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
    """播放大圆钮：蓝底 + 白 play/pause 图标。
    图标 mesh 只在 kind/尺寸变化时重建（6-24 顶点），位置变化走 Translate。"""

    def __init__(self, kind, on_press=None, **kw):
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dp(64), dp(64)))
        super(_RoundBig, self).__init__(**kw)
        self._on_press = on_press
        self._kind = kind
        from kivy.graphics import (Mesh, PopMatrix, PushMatrix, Translate)
        with self.canvas:
            self._gl_c = Color(0.30, 0.45, 1.0, 0.35)
            self._gl = Rectangle()
            self._bg_c = Color(0.24, 0.42, 1.0, 1)
            self._bg = RoundedRectangle(radius=[dp(32)])
            self._grp = PushMatrix()
            self._tr = Translate(0, 0, 0)
            self._ic_c = Color(1, 1, 1, 0.98)
            self._mesh = Mesh(mode="triangles", vertices=[], indices=[])
            self._pop = PopMatrix()
        self._last_key = None
        self.bind(pos=self._move_only, size=self._layout)
        self._layout()

    def _layout(self, *_):
        self._bg.pos = self.pos
        self._bg.size = self.size
        self._bg.radius = [min(self.width, self.height) / 2.0 * 0.98]
        g = min(self.width, self.height) * 1.9
        self._gl.pos = (self.x + (self.width - g) / 2.0,
                        self.y + (self.height - g) / 2.0)
        self._gl.size = (g, g)
        self._build_icon()

    def _move_only(self, *_):
        self._bg.pos = self.pos
        g = min(self.width, self.height) * 1.9
        self._gl.pos = (self.x + (self.width - g) / 2.0,
                        self.y + (self.height - g) / 2.0)
        try:
            self._tr.xyz = (self.x, self.y, 0)
        except Exception:
            pass

    def _build_icon(self, *_):
        u = min(self.width, self.height) * 0.30
        if u < 4:
            return
        cx, cy = self.width / 2.0, self.height / 2.0
        v, idx = [], []
        if self._kind == "play":
            p = [(cx - u * 0.55, cy + u), (cx - u * 0.55, cy - u),
                 (cx + u * 0.85, cy)]
            for px, py in p:
                v += [px, py, 0, 0, 1, 1, 1, 1]
            idx = [0, 1, 2]
        else:
            bw = u * 0.42
            for dx in (-u * 0.62, u * 0.18):
                base = len(v) // 8
                x0, x1 = cx + dx, cx + dx + bw
                y0, y1 = cy - u * 1.05, cy + u * 1.05
                for px, py in ((x0, y0), (x1, y0), (x1, y1),
                               (x0, y0), (x1, y1), (x0, y1)):
                    v += [px, py, 0, 0, 1, 1, 1, 1]
                idx += [base, base + 1, base + 2,
                        base + 3, base + 4, base + 5]
        key = (self._kind, round(u, 1), round(cx, 1), round(cy, 1))
        if key == self._last_key:
            return                      # 同一帧多次 pos/size 触发时**不重建**
        self._last_key = key
        try:
            self._mesh.vertices = v
            self._mesh.indices = idx
            self._tr.xyz = (self.x, self.y, 0)
        except Exception:
            pass

    def set_kind(self, k):
        if k == self._kind:
            return
        self._kind = k
        self._last_key = None
        self._build_icon()

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self._down = True
            self._bg_c.rgba = (0.16, 0.32, 0.9, 1)
            w = self.width * 0.94
            h = self.height * 0.94
            self._bg.size = (w, h)
            self._bg.pos = (self.x + (self.width - w) / 2.0,
                            self.y + (self.height - h) / 2.0)
            return True
        return False

    def on_touch_up(self, touch):
        if getattr(self, "_down", False):
            self._down = False
            self._bg.pos = self.pos
            self._bg.size = self.size
            if self.collide_point(*touch.pos) and self._on_press:
                try:
                    self._on_press()
                except Exception:
                    log_exc("_RoundBig")
            return True
        return False
