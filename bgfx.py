"""随音乐而动的背景（柔和光晕版）。

为什么不用「真频谱」
--------------------
Kivy 这边拿不到音频数据：Android 的 MediaPlayer 不暴露 PCM 缓冲，
要取真实频谱只能挂 android.media.audiofx.Visualizer，而它需要
**RECORD_AUDIO 权限** —— 一个音乐下载器申请麦克风权限，用户第一反应
就是「这玩意儿要偷听我」。不值得。所以用「播放状态驱动」。

为什么不再用实心圆
------------------
上一版用 graphics.Ellipse 直接画半透明圆 —— 那是**硬边**的，
看起来就是「几个圆圈」，很廉价。这一版改成运行时生成的
**径向渐变贴图**（中心亮、向外平滑衰减到全透明），
用 Rectangle 贴上去，才是光晕该有的样子。

另外补了一层竖向渐变底（顶部更深、底部略亮），整体不再是一块死黑。

绘制顺序：必须在 attach_bg(root, C_BG) 之后创建 —— canvas.before 里的
指令按插入顺序绘制，背景要压在底色之上、卡片之下。
"""
import math

from kivy.clock import Clock
from kivy.graphics import Color, Rectangle
from kivy.graphics.texture import Texture

from appenv import log_exc

# 光晕贴图边长。64 已经足够柔和（会被拉伸到几百 dp），
# 再大只是徒增启动耗时。
GLOW_PX = 64


def glow_alpha(px, py, size=GLOW_PX):
    """光晕贴图在 (px,py) 处的 alpha（0..255）。

    抽成纯函数是为了能在没有 GL 的机器上验证衰减形状 ——
    贴图本身要 Texture.create，那一步需要 GL 上下文，测试机跑不了。
    衰减用 (1-d)^2.5：比线性柔和得多，也不像高斯那样糊成一片。
    """
    c = (size - 1) / 2.0
    if c <= 0:
        return 255
    dx = (px - c) / c
    dy = (py - c) / c
    d = math.sqrt(dx * dx + dy * dy)
    if d >= 1.0:
        return 0
    return int(255 * (1.0 - d) ** 2.5)


def make_glow_texture(size=GLOW_PX):
    """生成一张白色径向渐变贴图（中心不透明、边缘全透明）。

    白色 + 让 Color 提供颜色，这样一张贴图就能染成任意色，省内存。
    必须用渐变贴图而不是 graphics.Ellipse：实心椭圆是**硬边**的，
    画出来就是「几个圆圈」，很廉价（用户反馈过）。
    """
    buf = bytearray()
    for y in range(size):
        for x in range(size):
            buf += bytes((255, 255, 255, glow_alpha(x, y, size)))
    tex = Texture.create(size=(size, size), colorfmt="rgba")
    tex.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
    tex.wrap = "clamp_to_edge"
    tex.mag_filter = "linear"
    tex.min_filter = "linear"
    return tex


def make_vgradient_texture(top_rgba, bottom_rgba, height=64):
    """生成竖向渐变贴图（横向拉伸铺满即可）"""
    buf = bytearray()
    for y in range(height):
        t = y / float(height - 1)
        row = []
        for _ in range(2):          # 只要 2 像素宽，拉伸后足够
            row.append(tuple(
                int(255 * (top_rgba[i] + (bottom_rgba[i] - top_rgba[i]) * t))
                for i in range(4)))
        for px in row:
            buf += bytes(px)
    tex = Texture.create(size=(2, height), colorfmt="rgba")
    tex.blit_buffer(bytes(buf), colorfmt="rgba", bufferfmt="ubyte")
    tex.wrap = "clamp_to_edge"
    tex.mag_filter = "linear"
    tex.min_filter = "linear"
    return tex


class MusicBackground:
    """跟着播放状态动的背景：渐变底 + 若干柔和光晕。"""

    # 光晕基色（主题蓝 / 青 / 紫），与界面强调色同族
    TINTS = (
        (0.29, 0.56, 0.95),
        (0.18, 0.70, 0.84),
        (0.46, 0.36, 0.90),
        (0.14, 0.58, 0.70),
        (0.36, 0.30, 0.80),
    )
    # 渐变底：顶部偏冷偏暗 -> 底部略亮
    BG_TOP = (0.055, 0.065, 0.090, 1)
    BG_BOTTOM = (0.105, 0.118, 0.150, 1)

    def __init__(self, host, fps=24):
        self._host = host
        self._fps = float(fps)
        self._t = 0.0
        self._level = 0.0
        self._playing = False
        self._ev = None
        self._glows = []

        # ---- 渐变底 ----
        self._bg_col = Color(1, 1, 1, 1)
        self._bg_tex = make_vgradient_texture(self.BG_TOP, self.BG_BOTTOM)
        self._bg_rect = Rectangle(pos=(0, 0), size=(1, 1), texture=self._bg_tex)
        host.canvas.before.add(self._bg_col)
        host.canvas.before.add(self._bg_rect)

        # ---- 光晕 ----
        glow_tex = make_glow_texture()
        for i, (r, g, b) in enumerate(self.TINTS):
            col = Color(r, g, b, 0.0)
            rect = Rectangle(pos=(0, 0), size=(1, 1), texture=glow_tex)
            host.canvas.before.add(col)
            host.canvas.before.add(rect)
            self._glows.append({
                "col": col, "rect": rect,
                "ph": i * 1.37,
                # 每个光晕自己的漂移速度/半径系数 —— 速度不同才有纵深
                "sp": 0.09 + 0.035 * i,
                "scale": 0.75 + 0.18 * ((i * 3) % 4),
            })

        host.bind(pos=self._layout, size=self._layout)
        self._layout()

    # ---------- 对外 ----------
    def set_playing(self, playing):
        playing = bool(playing)
        if playing == self._playing:
            return
        self._playing = playing
        if playing:
            self._start()

    # ---------- 内部 ----------
    def _start(self):
        if self._ev is None:
            self._ev = Clock.schedule_interval(self._step, 1.0 / self._fps)

    def _stop(self):
        if self._ev is not None:
            self._ev.cancel()
            self._ev = None

    def _layout(self, *_):
        try:
            self._bg_rect.pos = self._host.pos
            self._bg_rect.size = self._host.size
            self._place()
        except Exception:
            log_exc("background.layout")

    def _place(self):
        w = max(1.0, self._host.width)
        h = max(1.0, self._host.height)
        base = min(w, h)
        for g in self._glows:
            ph, sp = g["ph"], g["sp"]
            # 漂移：两条不同周期的正弦，避免看出明显往复
            x = 0.5 + 0.34 * math.sin(self._t * sp + ph)
            y = 0.5 + 0.30 * math.cos(self._t * sp * 0.83 + ph * 1.4)
            # 呼吸：播放时明显，静止时几乎不动
            breath = 0.5 + 0.5 * math.sin(self._t * 1.25 + ph * 2.1)
            grow = 0.30 + 0.70 * self._level
            size = base * g["scale"] * (0.85 + 0.30 * breath) * grow
            g["rect"].size = (size, size)
            g["rect"].pos = (w * x - size / 2.0, h * y - size / 2.0)
            # 静止时压到几乎不可见，别抢注意力
            g["col"].a = 0.045 + 0.155 * self._level * (0.55 + 0.45 * breath)

    def _step(self, dt):
        try:
            target = 1.0 if self._playing else 0.0
            # 平滑跟随：暂停后缓下来，不会「啪」地定格
            self._level += (target - self._level) * min(1.0, dt * 2.0)
            self._t += dt
            self._place()
            if not self._playing and self._level < 0.01:
                self._level = 0.0
                self._place()          # 收到静止态再画最后一帧
                self._stop()           # 完全静下来就停 tick，不播放不耗电
        except Exception:
            log_exc("background.step")
