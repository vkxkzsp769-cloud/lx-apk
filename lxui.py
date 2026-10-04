"""iOS 风格界面层 —— 只有呈现，没有业务。

这一层是干什么的
----------------
把「界面长什么样」从 main.py 里剥出来单独放这儿。main.py 只负责业务编排
（搜索 / 解析 / 下载 / 播放），它拿到的是一堆控件，改的是控件的
text / value；控件怎么画、圆角多少、动画怎么走，全在本文件里。
本文件**不允许** import searchers / downloader / songinfo / player 这类
业务模块 —— 它只认数据（文本、数值、回调）。

设计依据（苹果 HIG / WWDC）
---------------------------
* 颜色材质：iOS 暗色模式。背景 #121212（不是死黑）、卡片 #1C1C1E、
  列表项 / 输入框 #2C2C2E；重点区域（底部卡片、弹窗）用毛玻璃近似。
* 动效曲线：不用 ease-in-out，用**弹簧物理**。参数直接取苹果官方值：
      抽屉 / 底部弹窗 sheet → 阻尼比 0.8、响应 0.3s
      移动 / 画中画         → 阻尼比 1.0、响应 0.4s
  弹簧没有固定时长，所以这里按帧积分（spring_curve + Spring），
  而不是用 Kivy 的 Animation 近似 —— 那样没法中途打断、也做不出过冲。
* 层级：弹窗弹出时铺一层 rgba(0,0,0,0.5) 遮罩，并且**吞掉**触摸事件。
  原来那层只是画了个黑色矩形，完全不挡点击，所以会出现「UI 叠在一起 +
  点到下面的东西」。
* 排版：分组列表行高 52dp、分组间距 16dp、容器圆角 20~24dp、
  触摸目标 ≥44dp、底部卡片顶部 24dp 圆角 + 拖拽指示条。

关于「毛玻璃」的诚实说明
------------------------
Kivy 的 canvas 拿不到「背后的像素」，做不了真正的 backdrop-blur。
这里的做法（glass()）是：半透明填充 + 顶部受光渐变 + 极淡描边 ——
观感接近，但严格说不是模糊。别对外宣称是真 blur。

对外约定（main.py 依赖的名字，改名会连环崩）
------------------------------------------
控件：FlatButton / SpringButton / IconButton / CNSpinner / CNDropdown /
      CNSpinnerOption / SearchInput / GroupCard / GroupRow / ActionRow /
      GroupHeader / ModalLayer / PickerSheet / BarView / Disc / Scrim /
      Grabber / SongRow
纯函数：icon_parts / draw_icon / grad_buffer / attach_bg / attach_border /
      attach_glow / attach_gradient / spring_curve / rounded / soft_shadow /
      glass / follow

字体：本文件里创建的每个文本控件都自动带中文字体（fonts.font_kwargs()），
      不再靠调用方传 **self.F —— 历史上「漏传字体 → 中文全变方块」
      就是这么来的，能自动就别让人记。
"""
import math

from kivy.clock import Clock
from kivy.graphics import Color, Ellipse, Line, Mesh, Rectangle, RoundedRectangle
from kivy.metrics import dp
from kivy.properties import (BooleanProperty, ListProperty, NumericProperty,
                             StringProperty)
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.dropdown import DropDown
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

import bgfx
import fonts
from appenv import log_exc

# ============================================================
#  设计令牌
# ============================================================
C_BG = (0.071, 0.071, 0.071, 1)          # #121212 页面底色（不用死黑）
C_SHEET = (0.110, 0.110, 0.118, 1)       # #1C1C1E 卡片 / 弹窗
C_ROW = (0.173, 0.173, 0.180, 1)         # #2C2C2E 列表项 / 输入框
C_ROW_DOWN = (0.243, 0.243, 0.255, 1)    # #3E3E41 按下态（暗色里要「提亮」才看得见）
C_SEP = (0.235, 0.235, 0.247, 0.75)      # 分隔线 / 描边
C_TEXT = (0.976, 0.976, 0.984, 1)        # 主文本
C_DIM = (0.922, 0.922, 0.961, 0.60)      # 次级文本（iOS secondaryLabel）
C_FAINT = (0.922, 0.922, 0.961, 0.30)    # 三级文本（iOS tertiaryLabel）
C_ACCENT = (0.039, 0.518, 1.0, 1)        # #0A84FF iOS 系统蓝
C_ACCENT_D = (0.0, 0.408, 0.859, 1)
C_OK = (0.188, 0.820, 0.345, 1)          # #30D158
C_ERR = (1.0, 0.271, 0.227, 1)           # #FF453A
C_WHITE = (1, 1, 1, 1)
C_BLACK = (0, 0, 0, 1)
# 渐变两端（搜索按钮 / 播放按钮 / 进度条填充）
GRAD_A = (0.039, 0.518, 1.0)             # iOS 蓝
GRAD_B = (0.369, 0.361, 0.902)           # iOS 靛

# 旧名字的别名：main.py 里到处在用，保持兼容
C_CARD = C_SHEET
C_CTRL = C_ROW
C_ITEM = C_ROW

# 圆角
R_SHEET = 24      # 底部卡片顶角
R_CARD = 20       # 分组卡片
R_ROW = 12        # 列表行
R_CAPSULE = 22    # 胶囊（搜索框 / 按钮）
# 旧名字
R_LG = 24
R_MD = 16
R_SM = 12

# 尺寸
H_ROW = 52        # 分组列表行高（HIG 建议 ≥44）
H_TOUCH = 44      # 最小触摸目标
H_SEARCH = 44     # 搜索框与搜索按钮统一高度
GAP_GROUP = 16    # 分组之间的灰色间距
SCRIM_ALPHA = 0.5  # 遮罩不透明度

# 毛玻璃近似的两个参数
GLASS_FILL = (1, 1, 1, 0.05)
GLASS_HI = 0.09

# 弹簧参数（response 秒, damping 阻尼比）—— 苹果官方交互参数
SPRING_SHEET = (0.30, 0.80)   # 抽屉 / 底部弹窗
SPRING_MOVE = (0.40, 1.00)    # 移动 / 画中画（临界阻尼，不过冲）
SPRING_PRESS = (0.22, 0.90)   # 按钮按下回弹
FRAME = 1.0 / 60.0            # 按 60fps 积分
FUSE_DELAY = 1.8              # 弹窗动画的保险丝（秒）


# ============================================================
#  弹簧物理
# ============================================================
def spring_curve(t, response=0.4, damping=1.0):
    """二阶弹簧的阶跃响应：t 时刻的位置（0 → 1）。

    抽成纯函数有两个原因：
      1) 能在没有窗口 / 没有 GL 的机器上直接断言它的性质（不过冲、过冲量）
      2) 阻尼比不同要解不同的解析式，写错一个符号整个动效就废了，
         放在一处好核对

    阻尼比 <1 会过冲（弹一下再落回），=1 恰好不过冲，>1 慢慢贴上去。
    """
    if t <= 0.0:
        return 0.0
    if response <= 0.0:
        return 1.0
    w = 2.0 * math.pi / float(response)
    z = max(0.0, float(damping))

    if abs(z - 1.0) < 1e-6:                      # 临界阻尼
        return 1.0 - (1.0 + w * t) * math.exp(-w * t)
    if z < 1.0:                                  # 欠阻尼（有过冲）
        wd = w * math.sqrt(1.0 - z * z)
        return 1.0 - math.exp(-z * w * t) * (
            math.cos(wd * t) + (z * w / wd) * math.sin(wd * t))
    r = w * math.sqrt(z * z - 1.0)               # 过阻尼（两个实根）
    r1, r2 = -z * w + r, -z * w - r
    return 1.0 + (r2 * math.exp(r1 * t) - r1 * math.exp(r2 * t)) / (r1 - r2)


class Spring:
    """Clock 驱动的弹簧，**可中途打断**。

    苹果流体的核心要求是「动画随时可被打断，从屏幕上的实时位置重新开始」。
    Kivy 的 Animation 做不到（它按固定时长从起点跑到终点，重新起一个
    动画会从旧起点跳一下），所以这里自己按帧积分：

        value(t) = from + (to - from) * spring_curve(t)

    打断时把**当前值**当新的 from，于是视线里的运动是连续的。
    """

    def __init__(self, owner, prop, response=0.4, damping=1.0):
        self.owner = owner
        self.prop = prop
        self.response = float(response)
        self.damping = float(damping)
        self._ev = None
        self._t = 0.0
        self._from = 0.0
        self._to = 0.0
        self._on_done = None

    def to(self, target, on_done=None):
        try:
            self._from = float(getattr(self.owner, self.prop) or 0.0)
        except Exception:
            self._from = 0.0
        self._to = float(target)
        self._t = 0.0
        self._on_done = on_done
        if abs(self._to - self._from) < 1e-6:
            self._write(self._to)
            self._finish()
            return self
        if self._ev is None:
            self._ev = Clock.schedule_interval(self._step, FRAME)
        return self

    def cancel(self):
        self._finish()

    def _write(self, v):
        try:
            setattr(self.owner, self.prop, v)
        except Exception:
            log_exc("spring.write")

    def _finish(self):
        if self._ev is not None:
            self._ev.cancel()
            self._ev = None
        cb, self._on_done = self._on_done, None
        if cb is not None:
            try:
                cb()
            except Exception:
                log_exc("spring.on_done")

    def _step(self, dt):
        try:
            self._t += dt
            k = spring_curve(self._t, self.response, self.damping)
            self._write(self._from + (self._to - self._from) * k)
            # 收尾：曲线贴到 1 就停 —— 不然时钟一直挂着白耗电。
            # 后面那个条件是保险丝（万一算不出「贴到 1」也不会永远跑）。
            if (self._t >= self.response * 1.6 and abs(k - 1.0) < 0.002) \
                    or self._t > self.response * 6 + 0.5:
                self._write(self._to)
                self._finish()
        except Exception:
            log_exc("spring.step")
            self._finish()


def spring_to(owner, prop, target, response=0.4, damping=1.0, on_done=None):
    """给 owner.prop 起一个弹簧。

    同一个控件的同一个属性复用**同一个驱动对象** —— 这样连续调用
    （比如用户快速开关弹窗）天然是「从当前位置重新出发」，不会跳。
    """
    reg = getattr(owner, "_springs", None)
    if reg is None:
        reg = {}
        owner._springs = reg
    sp = reg.get(prop)
    if sp is None:
        sp = Spring(owner, prop, response, damping)
        reg[prop] = sp
    else:
        sp.response, sp.damping = float(response), float(damping)
    return sp.to(target, on_done)


# ============================================================
#  基础图元
# ============================================================
def attach_bg(widget, color, radius=0):
    """给控件加背景（可选圆角）。

    Kivy 没有 canvas_before 这个属性（它是 widget.canvas.before 对象），
    当构造参数传会抛 TypeError 导致启动即崩 —— 必须建好后再加图元。
    """
    with widget.canvas.before:
        c = Color(*color)
        if radius:
            shape = RoundedRectangle(pos=widget.pos, size=widget.size,
                                     radius=[dp(radius)])
        else:
            shape = Rectangle(pos=widget.pos, size=widget.size)
    widget._r_color = c
    widget._r_shape = shape

    def _sync(w, *_):
        shape.pos = w.pos
        shape.size = w.size

    widget.bind(pos=_sync, size=_sync)
    return widget


def rounded(widget, color, radius=R_ROW, corners=None):
    """圆角填充；corners 可只圆某几个角。

    iOS 的分组列表是「整张卡片四角圆、行与行之间是直角」，Kivy 不会裁剪
    子控件，所以要按行给不同的角：首行 [r,r,0,0]、中间 [0,0,0,0]、
    末行 [0,0,r,r]。
    """
    rs = corners if corners is not None else [dp(radius)] * 4
    with widget.canvas.before:
        c = Color(*color)
        shape = RoundedRectangle(pos=widget.pos, size=widget.size, radius=rs)
    widget._fill_color = c
    widget._r_shape = shape

    def _sync(w, *_):
        shape.pos = w.pos
        shape.size = w.size

    widget.bind(pos=_sync, size=_sync)
    return widget


def attach_border(widget, radius=R_ROW, color=None, width=1.0):
    """细微描边高光。

    暗色界面里卡片如果只有填充色，会「糊」在一起分不出层级 ——
    一圈极淡的描边是最省力的分层手段（border highlight）。
    """
    col = color or C_SEP
    with widget.canvas.after:
        c = Color(*col)
        line = Line(width=dp(width), rounded_rectangle=(0, 0, 1, 1, dp(radius)))

    def _sync(w, *_):
        line.rounded_rectangle = (w.x, w.y, w.width, w.height, dp(radius))

    widget.bind(pos=_sync, size=_sync)
    _sync(widget)
    widget._border_line = line
    widget._border_color = c
    return widget


def attach_glow(widget, color=None, spread=None, alpha=0.30):
    """柔和的**彩色**光（暗色界面里黑色阴影看不见，得用光）。

    必须在 attach_bg / rounded **之前**调用：canvas.before 按插入顺序绘制，
    后加的会盖在前面。

    注意 spread 的默认值写成 None、在函数体里算 dp()：默认参数在
    **模块导入时**求值，那时还没有窗口，dp() 会把导入搞崩（实测踩到）。
    """
    if spread is None:
        spread = dp(18)
    r, g, b = (color or C_ACCENT_D)[:3]
    tex = _glow_tex()
    if tex is None:
        return widget
    with widget.canvas.before:
        c = Color(r, g, b, alpha)
        rect = Rectangle(pos=widget.pos, size=widget.size, texture=tex)

    def _sync(w, *_):
        rect.pos = (w.x - spread, w.y - spread)
        rect.size = (w.width + spread * 2, w.height + spread * 2)

    widget.bind(pos=_sync, size=_sync)
    _sync(widget)
    widget._glow_rect = rect
    widget._glow_color = c
    return widget


def soft_shadow(widget, spread=None, alpha=0.40):
    """柔和投影（黑色，柔化过的 —— 不是 Kivy 默认那种硬边阴影）。"""
    return attach_glow(widget, color=(0, 0, 0), spread=spread, alpha=alpha)


_GLOW = []


def _glow_tex():
    """径向渐变贴图（惰性创建，建不出来就返回 None 让装饰优雅降级）"""
    if not _GLOW:
        try:
            _GLOW.append(bgfx.make_glow_texture())
        except Exception:
            log_exc("光晕贴图")
            _GLOW.append(None)
    return _GLOW[0]


_VGRAD = {}


def _sheen_tex(hi=GLASS_HI):
    """竖向渐变贴图：顶部受光、向下淡出。做毛玻璃的「受光面」。"""
    key = round(float(hi), 3)
    if key not in _VGRAD:
        try:
            _VGRAD[key] = bgfx.make_vgradient_texture(
                (1, 1, 1, float(hi)), (1, 1, 1, 0.0), 48)
        except Exception:
            log_exc("毛玻璃渐变贴图")
            _VGRAD[key] = None
    return _VGRAD[key]


def glass(widget, radius=R_CAPSULE, tint=None, hi=None, border=True):
    """毛玻璃近似（三层叠加，见文件头「关于毛玻璃的诚实说明」）。

    绘制顺序（都在 canvas.before 里，按插入顺序）：
      1. 半透明填充 —— 让背后的背景光晕透出来（这是「玻璃」的关键）
      2. 顶部受光渐变 —— 白色自上而下淡出，模拟玻璃的高光面
      3. 极淡描边（在 canvas.after，勾出边缘）
    """
    tint = GLASS_FILL if tint is None else tint
    tex = _sheen_tex(GLASS_HI if hi is None else hi)
    with widget.canvas.before:
        c1 = Color(*tint)
        s1 = RoundedRectangle(pos=widget.pos, size=widget.size,
                              radius=[dp(radius)])
        c2 = Color(1, 1, 1, 1) if tex is not None else Color(0, 0, 0, 0)
        s2 = RoundedRectangle(pos=widget.pos, size=widget.size,
                              radius=[dp(radius)], texture=tex)

    def _sync(w, *_):
        s1.pos = w.pos
        s1.size = w.size
        s2.pos = w.pos
        s2.size = w.size

    widget.bind(pos=_sync, size=_sync)
    widget._glass_fill = c1
    widget._glass_sheen = c2
    if border:
        attach_border(widget, radius=radius, color=(1, 1, 1, 0.10))
    return widget


def grad_buffer(c1, c2, size=64, horizontal=True):
    """按 colorfmt='rgb' 生成渐变像素缓冲。

    抽出来是为了能**在没有 GL 的机器上验证字节数** ——
    贴图本身要 Texture.create（要 GL），但缓冲区长度对不对是纯算术。

    ⚠ 这里踩过一个坑：colorfmt="rgb" 是**每像素 3 字节**，
    但最初写成 `bytes(px + (255,))` 往每像素塞了 4 字节 ——
    长度对不上，纹理读进去就是错位数据，整片渐变色乱掉。
    """
    per_px = 3
    out = bytearray()
    for i in range(size):
        t = i / float(size - 1)
        px = bytes(int(255 * (c1[k] + (c2[k] - c1[k]) * t)) for k in range(3))
        if len(px) != per_px:                     # 自检，防止再写回 4 字节
            raise AssertionError("每像素必须是 %d 字节，实际 %d"
                                 % (per_px, len(px)))
        # 横向贴图是 size×1，竖向是 1×size —— 两种情况都是 size 个像素，
        # 所以都只追加一个像素，不能乘 size。
        out += px
    expect = size * per_px
    if len(out) != expect:
        raise AssertionError("缓冲长度 %d 不等于 w*h*3=%d" % (len(out), expect))
    return bytes(out)


_GRAD_TEX = {}


def make_grad_texture(c1, c2, size=64, horizontal=True):
    """生成两端渐变的贴图（胶囊按钮 / 进度条填充用）。

    Kivy 的 Color/Rectangle 只能画纯色，没有渐变；自己算一张小贴图拉伸
    是唯一的路子。同一组颜色复用，别每次都建贴图。
    """
    from kivy.graphics.texture import Texture
    key = (tuple(round(v, 4) for v in c1[:3]),
           tuple(round(v, 4) for v in c2[:3]), size, bool(horizontal))
    if key in _GRAD_TEX:
        return _GRAD_TEX[key]
    dims = (size, 1) if horizontal else (1, size)
    tex = Texture.create(size=dims, colorfmt="rgb")
    tex.blit_buffer(grad_buffer(c1, c2, size, horizontal),
                    colorfmt="rgb", bufferfmt="ubyte")
    tex.wrap = "clamp_to_edge"
    tex.mag_filter = "linear"
    tex.min_filter = "linear"
    _GRAD_TEX[key] = tex
    return tex


def attach_gradient(widget, c1, c2, radius=R_CAPSULE, horizontal=True):
    """给控件加渐变圆角背景（描边由 attach_border 单独负责）"""
    tex = make_grad_texture(c1, c2, horizontal=horizontal)
    with widget.canvas.before:
        Color(1, 1, 1, 1)
        shape = RoundedRectangle(pos=widget.pos, size=widget.size,
                                 radius=[dp(radius)], texture=tex)

    def _sync(w, *_):
        shape.pos = w.pos
        shape.size = w.size

    widget.bind(pos=_sync, size=_sync)
    # 记下来给 SpringButton 用；同时把 FlatButton 自带的纯色底压成全透明，
    # 否则会「纯色底 + 渐变底」两层叠着画，颜色不对。
    widget._grad_shape = shape
    plain = getattr(widget, "_bg_color_i", None)
    if plain is not None:
        plain.rgba = (0, 0, 0, 0)
    return widget


# ============================================================
#  图标（自己画，不用字符 —— 自带中文字体里没有 emoji / 几何符号）
# ============================================================
def icon_parts(kind, w, h, ox=0.0, oy=0.0, unit=1.0):
    """把图标拆成**父坐标系**下的图元描述（纯函数，不碰 Kivy）。

    背景：往 widget.canvas 加的图元用的是**父坐标系**（和 widget.pos 同一
    空间），早期版本用本地坐标 `cx, cy = w/2, h/2` 算几何 —— 图标被画到
    父容器原点附近去了（用户反馈「叉不在应该在的地方」）。几何抽在这里，
    就能在没有 OpenGL 的机器上直接断言坐标对不对。

    ox/oy = 控件在父坐标系里的原点（widget.x / widget.y）；
    unit  = 一个 dp 对应的像素数（线宽要用它换算 —— 纯函数里不能调 dp()）。

    返回 [(op, kwargs), ...]，op ∈ {"line", "mesh"}。
    """
    s = min(w, h) * 0.5
    cx = ox + w / 2.0
    cy = oy + h / 2.0
    out = []
    if kind == "settings":
        # 三条滑杆（现代感的「设置」图标，比齿轮更好画也更好看）
        lw = 1.6 * unit
        for i, off in enumerate((-0.42, 0.0, 0.42)):
            y = cy + s * off
            out.append(("line", {"points": [cx - s * 0.72, y, cx + s * 0.72, y],
                                 "width": lw}))
            knx = cx + s * (0.34 if i % 2 == 0 else -0.30)
            out.append(("line", {"circle": (knx, y, 2.4 * unit), "width": lw}))
    elif kind == "search":
        out.append(("line", {"circle": (cx - s * 0.18, cy + s * 0.18, s * 0.52),
                             "width": 1.7 * unit}))
        out.append(("line", {"points": [cx + s * 0.20, cy - s * 0.20,
                                        cx + s * 0.64, cy - s * 0.64],
                             "width": 1.7 * unit}))
    elif kind == "close":
        out.append(("line", {"points": [cx - s * 0.42, cy - s * 0.42,
                                        cx + s * 0.42, cy + s * 0.42],
                             "width": 1.7 * unit}))
        out.append(("line", {"points": [cx - s * 0.42, cy + s * 0.42,
                                        cx + s * 0.42, cy - s * 0.42],
                             "width": 1.7 * unit}))
    elif kind == "play":
        # SF Symbols 的 play.fill：略偏右的实心三角（视觉重心才对）
        out.append(("mesh", {"vertices": [cx - s * 0.34, cy - s * 0.50, 0, 0,
                                          cx - s * 0.34, cy + s * 0.50, 0, 0,
                                          cx + s * 0.54, cy, 0, 0],
                             "indices": [0, 1, 2], "mode": "triangles"}))
    elif kind == "pause":
        # pause.fill：两条**实心**竖条。
        # 必须是填充而不是描边：Line(rectangle=...) 画出来是空心方块，一眼假。
        # 图元统一用 mesh 出（和 play 同一个 op 类型），这样本函数的输出只有
        # line / mesh 两种，外部按「图元种类」解析坐标时不用再学新写法。
        bw = s * 0.30
        for dx in (-0.36, 0.06):
            x0, x1 = cx + s * dx, cx + s * dx + bw
            y0, y1 = cy - s * 0.48, cy + s * 0.48
            out.append(("mesh", {
                "vertices": [x0, y0, 0, 0, x1, y0, 0, 0, x1, y1, 0, 0,
                             x0, y0, 0, 0, x1, y1, 0, 0, x0, y1, 0, 0],
                "indices": list(range(12)), "mode": "triangles"}))
    elif kind == "download":
        out.append(("line", {"points": [cx, cy + s * 0.55, cx, cy - s * 0.20],
                             "width": 1.8 * unit}))
        out.append(("line", {"points": [cx - s * 0.34, cy + s * 0.10,
                                        cx, cy - s * 0.24,
                                        cx + s * 0.34, cy + s * 0.10],
                             "width": 1.8 * unit}))
        out.append(("line", {"points": [cx - s * 0.46, cy - s * 0.55,
                                        cx + s * 0.46, cy - s * 0.55],
                             "width": 1.8 * unit}))
    elif kind == "expand":
        out.append(("line", {"points": [cx - s * 0.5, cy - s * 0.12,
                                        cx, cy + s * 0.36,
                                        cx + s * 0.5, cy - s * 0.12],
                             "width": 1.8 * unit}))
    elif kind == "chevron":
        # iOS 列表行右侧那个「>」
        out.append(("line", {"points": [cx - s * 0.18, cy + s * 0.40,
                                        cx + s * 0.24, cy,
                                        cx - s * 0.18, cy - s * 0.40],
                             "width": 2.0 * unit}))
    elif kind == "back":
        out.append(("line", {"points": [cx + s * 0.22, cy + s * 0.44,
                                        cx - s * 0.24, cy,
                                        cx + s * 0.22, cy - s * 0.44],
                             "width": 2.2 * unit}))
    elif kind == "check":
        out.append(("line", {"points": [cx - s * 0.46, cy + s * 0.04,
                                        cx - s * 0.12, cy - s * 0.34,
                                        cx + s * 0.50, cy + s * 0.44],
                             "width": 2.0 * unit}))
    elif kind == "folder":
        out.append(("line", {"rectangle": (cx - s * 0.60, cy - s * 0.42,
                                           s * 1.20, s * 0.84),
                             "width": 1.6 * unit}))
        out.append(("line", {"points": [cx - s * 0.60, cy + s * 0.42,
                                        cx - s * 0.42, cy + s * 0.42,
                                        cx - s * 0.30, cy + s * 0.60,
                                        cx + s * 0.10, cy + s * 0.60],
                             "width": 1.6 * unit}))
    return out


def draw_icon(widget, kind, color=None, size=None):
    """把图标**画**在控件上，而不是用字符。

    为什么不写 ⚙ / 🔍 这类字符：自带的中文字体是从 Noto Sans SC 裁出来的，
    emoji 和大部分几何符号根本不在里面，写上去就是方块。

    ⚠ 图元必须用**父坐标系**（widget.x / widget.y 当原点）：
    往 widget.canvas 加的东西和 widget.pos 在同一个坐标空间，
    用本地坐标会把图标画到父容器原点上去。
    """
    col = color or C_TEXT
    _DRAW = {"line": Line, "mesh": Mesh}

    # 换图标时会再调一次本函数（例如播放/暂停切换）。Kivy 的 bind 不会去重，
    # 不解绑的话每切一次就多挂一个回调，几百次之后每次重绘都要跑一堆。
    prev = getattr(widget, "_icon_redraw", None)
    if prev is not None:
        try:
            widget.unbind(pos=prev, size=prev)
        except Exception:
            pass

    def _redraw(*_):
        w, h = max(1.0, widget.width), max(1.0, widget.height)
        # 图标画进自己的 InstructionGroup：不能用 canvas.after.clear()，
        # 那会把 attach_border 加在同一 canvas.after 上的边框一起清掉。
        grp = getattr(widget, "_icon_group", None)
        if grp is None or grp not in widget.canvas.after.children:
            from kivy.graphics import InstructionGroup
            grp = InstructionGroup()
            widget._icon_group = grp
            widget.canvas.after.add(grp)
        grp.clear()
        grp.add(Color(*col))
        for op, kw in icon_parts(kind, w, h, widget.x, widget.y, dp(1.0)):
            grp.add(_DRAW[op](**kw))

    widget.bind(pos=_redraw, size=_redraw)
    _redraw()
    widget._icon_redraw = _redraw
    return widget


def vibrate(ms=12):
    """极短的触感反馈（没权限 / 不在 Android 上就静默跳过）。

    触感只是锦上添花，绝不能因为它让点击失效。
    """
    try:
        from appenv import IS_ANDROID
    except Exception:
        return
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        svc = act.getSystemService("vibrator")
        svc.vibrate(int(ms))
    except Exception:
        pass


# ============================================================
#  基础控件
# ============================================================
class FlatButton(Button):
    """扁平圆角按钮（背景自绘，所以关掉 Kivy 默认的灰色贴图）。

    down_color 是给「列表行」用的：暗色界面里按下时把行**提亮**才看得见，
    按 0.78 压暗会变成一块脏颜色（原来就是这个行为）。
    """

    bg_color = ListProperty([0.24, 0.27, 0.34, 1])
    down_color = ListProperty([0, 0, 0, 0])
    radius = NumericProperty(R_ROW)

    def __init__(self, **kw):
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", (0, 0, 0, 0))   # 关掉默认灰底
        kw.setdefault("font_size", dp(15))
        for k, v in fonts.font_kwargs().items():
            kw.setdefault(k, v)
        super().__init__(**kw)
        # 注意：bg_color 是通过 kwargs 设进来的，Kivy 会在 super().__init__()
        # 里就触发 on_bg_color，那时 canvas 图元还不存在 —— 所以那里判空。
        self._bg_color_i = Color(*self.bg_color)
        self._bg_shape = RoundedRectangle(
            pos=self.pos, size=self.size, radius=[dp(self.radius)] * 4)
        self.canvas.before.add(self._bg_color_i)
        self.canvas.before.add(self._bg_shape)
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_):
        self._bg_shape.pos = self.pos
        self._bg_shape.size = self.size

    def _apply_bg(self):
        c = getattr(self, "_bg_color_i", None)
        if c is None:
            return
        # 渐变按钮：纯色底已经被 attach_gradient 压成全透明，
        # 这里**不能**再按 bg_color 画回来 —— 否则一按下去就会冒出一块
        # 纯色盖住渐变（用户反馈「颜色乱了」的原因之一）。
        if getattr(self, "_grad_shape", None) is not None:
            return
        if self.state == "down" and self.down_color[3] > 0:
            c.rgba = self.down_color
            return
        r, g, b, a = self.bg_color
        # 按下时压暗一点。原来 background_normal/down 都设成空串，
        # 于是按钮**完全没有触摸反馈**，点下去像没反应。
        k = 0.82 if self.state == "down" else 1.0
        c.rgba = (r * k, g * k, b * k, a)

    def on_bg_color(self, *_):
        self._apply_bg()

    def on_state(self, *_):
        self._apply_bg()


class SpringButton(FlatButton):
    """按下缩到 0.95、松开带一点过冲弹回 1.0 —— 弹簧手感。

    Kivy 的 Widget 没有 scale 变换（那得用 PushMatrix 改 canvas 矩阵），
    所以这里动画的是**背景形状的内缩比例**：视觉上缩小了，但控件本身的
    布局尺寸不动 —— 否则会和 BoxLayout 的排布打架。

    曲线走 spring_curve（阻尼比 0.9 会有一次很轻的过冲），不是 out_back。
    """

    press_scale = NumericProperty(0.95)
    _press = NumericProperty(0.0)      # 0=常态 1=完全按下

    def __init__(self, **kw):
        self._haptic = kw.pop("haptic", True)
        super().__init__(**kw)
        self.bind(state=self._on_state, _press=self._sync)

    def _on_state(self, *_):
        down = self.state == "down"
        if down and self._haptic:
            vibrate(10)
        r, d = SPRING_PRESS
        spring_to(self, "_press", 1.0 if down else 0.0, r, d)

    def _sync(self, *_):
        shape = getattr(self, "_grad_shape", None) or getattr(self, "_bg_shape", None)
        if shape is None:
            return
        grow = 1.0 - self._press * (1.0 - self.press_scale)
        w, h = self.width * grow, self.height * grow
        shape.pos = (self.x + (self.width - w) / 2.0,
                     self.y + (self.height - h) / 2.0)
        shape.size = (w, h)


class IconButton(SpringButton):
    """只有图标的圆形按钮"""

    def __init__(self, kind, dia=None, color=None, bg=(1, 1, 1, 0.06),
                 icon_color=None, **kw):
        # dp() 不能在默认参数里求值（导入期就要窗口），所以在这里算
        if dia is None:
            dia = dp(40)
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dia, dia))
        # 图标必须自己声明垂直居中：Kivy 的 BoxLayout 在交叉轴（竖直方向）
        # **不会**居中固定尺寸的子控件，而是把它贴到内容区底部 ——
        # 于是图标高/矮于同行文字时就错位。
        kw.setdefault("pos_hint", {"center_y": 0.5})
        kw.setdefault("bg_color", bg)
        kw.setdefault("color", (0, 0, 0, 0))   # 不显示文字
        super().__init__(**kw)
        self._bg_shape.radius = [dia / 2.0] * 4    # 正圆
        draw_icon(self, kind, color=icon_color)


class Disc(Widget):
    """空状态用的「极度模糊唱片」占位图。

    真模糊做不到（见文件头说明），所以用叠出来的假失焦：
      三团低透明度彩色光斑（= 被糊开的彩色边缘，半径依次收缩、颜色错开）
      + 一张暗色圆片 + 两圈极淡的纹路 + 中心孔
    观感是一团安静的、失焦的圆，不抢注意力。
    """

    def __init__(self, dia=None, **kw):
        super().__init__(**kw)
        if dia is None:
            dia = dp(190)
        self._dia = float(dia)
        self.size_hint = (None, None)
        self.size = (dia * 1.6, dia * 1.6)
        tex = _glow_tex()
        # 直接存引用、在 _layout 里改几何 —— 比事后从 canvas 里翻指令稳得多
        self._spots = []
        self._disc = None
        self._rings = []
        self._hole = None
        if tex is not None:
            for spread, col, alpha in ((1.32, (0.16, 0.42, 0.95), 0.30),
                                       (1.04, (0.42, 0.32, 0.85), 0.22),
                                       (0.88, (0.10, 0.55, 0.72), 0.15)):
                self.canvas.add(Color(col[0], col[1], col[2], alpha))
                rect = Rectangle(pos=(0, 0), size=(1, 1), texture=tex)
                self.canvas.add(rect)
                self._spots.append((rect, spread))
        self.canvas.add(Color(0, 0, 0, 0.45))
        self._disc = RoundedRectangle(pos=(0, 0), size=(1, 1), radius=[1])
        self.canvas.add(self._disc)
        for k in range(2):
            self.canvas.add(Color(1, 1, 1, 0.05))
            ln = Line(circle=(0, 0, 1), width=dp(1))
            self.canvas.add(ln)
            self._rings.append(ln)
        self.canvas.add(Color(0.922, 0.922, 0.961, 0.18))
        self._hole = Ellipse(pos=(0, 0), size=(1, 1))
        self.canvas.add(self._hole)
        self.bind(pos=self._layout, size=self._layout)
        self._layout()

    def _layout(self, *_):
        try:
            cx, cy = self.center
            d = self._dia
            for rect, spread in self._spots:
                sz = d * spread
                rect.size = (sz, sz)
                rect.pos = (cx - sz / 2.0, cy - sz / 2.0)
            self._disc.size = (d, d)
            self._disc.pos = (cx - d / 2.0, cy - d / 2.0)
            self._disc.radius = [d / 2.0] * 4
            for k, ln in enumerate(self._rings):
                ln.circle = (cx, cy, d * (0.40 - 0.10 * k))
            hd = d * 0.17
            self._hole.size = (hd, hd)
            self._hole.pos = (cx - hd / 2.0, cy - hd / 2.0)
        except Exception:
            log_exc("disc.layout")


# ============================================================
#  下拉框（深色 + 中文字体）
# ============================================================
class CNSpinnerOption(SpinnerOption):
    """下拉列表项。

    Spinner 的选项项默认是 Kivy 自带的灰底渐变按钮 —— 就是用户说的
    「像老安卓」。这里换成和界面一致的深色圆角行 + 中文字体 + 左对齐。

    两个坑：
      * 选项项**不会**继承 Spinner 的字体，不传就是方块
      * Spinner._update_dropdown_size 会把每项高度强制设成 Spinner 的高度，
        所以高度不用自己定，但 size_hint_y 必须是 None
    """

    def __init__(self, **kw):
        for k, v in fonts.font_kwargs().items():
            kw.setdefault(k, v)
        kw.setdefault("background_normal", "")      # 关掉默认灰底贴图
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", (0, 0, 0, 0))
        kw.setdefault("size_hint_y", None)
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "middle")
        kw.setdefault("font_size", dp(15))
        kw.setdefault("color", C_TEXT)
        super().__init__(**kw)
        attach_bg(self, C_ROW, radius=R_ROW)
        self.bind(size=lambda b, v: setattr(b, "text_size", (v[0] - dp(22), None)))


class CNDropdown(DropDown):
    """下拉框本体。

    默认 DropDown 是个裸 ScrollView + 裸 GridLayout：没有背景、没有留白、
    还带一条滚动条，拉开就是一列灰色方块。这里给它铺上深色圆角底、
    加内边距、隐藏滚动条，并限制最大高度。
    """

    def __init__(self, **kw):
        kw.setdefault("max_height", dp(340))
        kw.setdefault("bar_width", 0)          # 隐藏滚动条，靠留白区分
        super().__init__(**kw)
        try:
            c = self.container
            if c is not None:
                c.padding = (dp(6), dp(6))
                c.spacing = dp(2)
                attach_bg(c, C_SHEET, radius=R_ROW)
        except Exception:
            log_exc("CNDropdown 背景")


class CNSpinner(Spinner):
    """下拉框（深色 + 中文字体）。

    注意：不要重新声明 font_name！
    Label 自己就有 font_name（默认 'Roboto'），重新声明成
    StringProperty(None) 会把默认值覆盖成 None，于是 Kivy 的
    resolve_font_name() 拿到 None 后崩：
      AttributeError: 'NoneType' object has no attribute 'endswith'
    这个崩只在「没找到中文字体」时触发（那时不会传 font_name），很容易漏测。

    新界面里它通常只当**状态容器**用（见 main._build_panel 的说明）：
    值由 iOS 风格的行来显示，展开选择由二级页 PickerSheet 负责。
    """

    def __init__(self, **kw):
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", C_ROW)
        kw.setdefault("color", C_TEXT)
        kw.setdefault("option_cls", CNSpinnerOption)   # 下拉项：深色圆角 + 中文字体
        kw.setdefault("dropdown_cls", CNDropdown)      # 下拉框：深色圆角 + 留白
        for k, v in fonts.font_kwargs().items():
            kw.setdefault(k, v)
        super().__init__(**kw)


class SearchInput(TextInput):
    """搜索输入框。

    用户反馈：点搜索框弹出软键盘，用返回键收起之后，**再点搜索框也弹不出
    键盘了**。原因是 Android 上 Kivy 的 TextInput 这时 `focus` 仍然是 True，
    系统认为「已经聚焦，不必再弹键盘」，状态就这么卡住。

    这里在触摸时检查一次「自以为聚焦、但键盘其实不在」，做一次
    「失焦 → 延时聚焦」，把键盘重新拉起来。键盘正常在的时候什么都不做。
    """

    def _keyboard_gone(self):
        try:
            from kivy.core.window import Window
            return not Window.keyboard_height
        except Exception:
            return False

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.focus and self._keyboard_gone():
            self.focus = False
            Clock.schedule_once(lambda *_: setattr(self, "focus", True), 0.05)
            return True
        return super().on_touch_down(touch)


# ============================================================
#  进度 / 拖拽条（视觉与状态分离）
# ============================================================
class BarView(Widget):
    """圆润进度条 —— **只负责画**。

    iOS 的进度条是「浅色轨道 + 亮色填充」，两端都是圆头。Kivy 的
    ProgressBar / Slider 画的是方角矩形、也上不了渐变，所以视觉单独放在
    这里；真正的状态仍然留在原来的 Slider / ProgressBar 上（它们被设成
    透明，只当状态和触摸目标），业务代码一行不用改。

    value/max 决定填充比例：播放条用 0..1，下载条用 0..100，
    这样 main 里那些「除以 100 再乘回去」的算式不用动。
    """

    value = NumericProperty(0.0)        # 0..max
    max = NumericProperty(1.0)          # 满值（播放条 1.0，下载条 100.0）
    thickness = NumericProperty(8)      # px
    color_a = ListProperty(list(GRAD_A))
    color_b = ListProperty(list(GRAD_B))
    track_color = ListProperty([1, 1, 1, 0.13])
    show_knob = BooleanProperty(False)

    def __init__(self, **kw):
        kw.setdefault("height", dp(8))
        super().__init__(**kw)
        self.size_hint_y = None
        from kivy.graphics import InstructionGroup
        self._g = InstructionGroup()
        self.canvas.add(self._g)
        self.bind(pos=self._redraw, size=self._redraw, value=self._redraw,
                  max=self._redraw, thickness=self._redraw)
        self._redraw()

    def _redraw(self, *_):
        try:
            th = max(2.0, dp(float(self.thickness)))
            r = th / 2.0
            w = max(1.0, self.width)
            h = max(1.0, self.height)
            y = self.y + (h - th) / 2.0
            top = float(self.max) or 1.0
            v = min(1.0, max(0.0, float(self.value) / top))
            self._g.clear()
            self._g.add(Color(*self.track_color))
            self._g.add(RoundedRectangle(pos=(self.x, y), size=(w, th),
                                         radius=[r] * 4))
            fw = w * v
            if fw > 0.5:
                self._g.add(Color(1, 1, 1, 1))
                self._g.add(RoundedRectangle(
                    pos=(self.x, y), size=(max(th, fw), th), radius=[r] * 4,
                    texture=make_grad_texture(self.color_a, self.color_b)))
            # 进度为 0 时不画旋钮：否则它会被挤到轨道左端外面去
            # （fw=0 → 圆心落在 self.x - kd/2），看着像个掉出来的点。
            if self.show_knob and fw > 0.5:
                kd = th * 1.9
                self._g.add(Color(1, 1, 1, 1))
                self._g.add(Ellipse(pos=(self.x + fw - kd / 2.0,
                                         self.y + (h - kd) / 2.0),
                                    size=(kd, kd)))
        except Exception:
            log_exc("BarView.redraw")


class Scrim(Widget):
    """全屏遮罩：rgba(0,0,0,0.5)，并且**吞掉**所有触摸。

    为什么必须是真控件而不是画上去的矩形：画出来的东西不参与触摸分发，
    底层控件照样能点到 —— 用户反馈的「UI 叠加 + 误触」就是这么来的。
    """

    fill_alpha = NumericProperty(0.0)     # 0..SCRIM_ALPHA（动画驱动）
    active = BooleanProperty(False)

    def __init__(self, on_tap=None, **kw):
        super().__init__(**kw)
        self._on_tap = on_tap
        with self.canvas.before:
            self._col = Color(0, 0, 0, 0)
            self._rect = Rectangle(pos=self.pos, size=self.size)

        def _sync(w, *_):
            self._rect.pos = w.pos
            self._rect.size = w.size

        self.bind(pos=_sync, size=_sync, fill_alpha=self._apply)
        _sync(self)

    def _apply(self, *_):
        self._col.rgba = (0, 0, 0, max(0.0, min(1.0, self.fill_alpha)))

    def on_touch_down(self, touch):
        if not self.active:
            return False
        if self._on_tap is not None:
            self._on_tap()
        return True        # 吞掉：不往下传

    def on_touch_move(self, touch):
        return bool(self.active)

    def on_touch_up(self, touch):
        return bool(self.active)


class Grabber(Widget):
    """底部卡片顶部中央的拖拽指示条（36×5 圆角灰条）。

    HIG 里这是「这块可以往下拖走」的唯一视觉线索，少了它用户不知道能关。
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.size_hint = (None, None)
        self.size = (dp(36), dp(5))
        with self.canvas.before:
            self._col = Color(0.922, 0.922, 0.961, 0.28)
            self._rr = RoundedRectangle(pos=self.pos, size=self.size,
                                        radius=[dp(2.5)] * 4)

        def _sync(w, *_):
            self._rr.pos = w.pos
            self._rr.size = w.size

        self.bind(pos=_sync, size=_sync)


# ============================================================
#  分组列表（iOS Grouped List）
# ============================================================
class GroupHeader(Label):
    """分组上方的灰色小标题（iOS 的 section header）"""

    def __init__(self, text="", **kw):
        kw.setdefault("text", text)
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", dp(22))
        kw.setdefault("font_size", dp(13))
        kw.setdefault("color", C_FAINT)
        kw.setdefault("bold", True)
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "middle")
        for k, v in fonts.font_kwargs().items():
            kw.setdefault(k, v)
        super().__init__(**kw)
        self.bind(size=lambda b, v: setattr(b, "text_size",
                                            (max(1.0, v[0] - dp(4)), None)))


class GroupRow(FloatLayout):
    """iOS 分组列表的一行：左标题（灰/白）+ 右侧当前值 + 「>」。

    为什么用 FloatLayout 套一个按钮，而不是直接拿 Button 显示两段文字：
    Button 只有一个 text，做不到「左边标题左对齐、右边值右对齐」。
    所以按钮只负责背景和触摸，文字由叠在上面的两个 Label 负责。

    行为由 GroupRow 自己实现（on_release / on_tap），外部只给回调，
    所以它跟业务没关系 —— 谁调它、点完干什么，由 main.py 决定。
    """

    value = StringProperty("")
    title = StringProperty("")

    def __init__(self, title="", value="", on_tap=None, trailing="chevron",
                 accent=False, corners=None, sep_below=True, h=None,
                 full_title=False, **kw):
        super().__init__(size_hint_y=None, height=h or dp(H_ROW), **kw)
        self._h = h or dp(H_ROW)
        self.title = title
        # 背景按钮：铺满整行；圆角只圆卡片外沿（见 rounded 的说明）
        self.btn = FlatButton(text="", bg_color=C_ROW, down_color=C_ROW_DOWN,
                              radius=0)
        self.btn.size_hint = (1, 1)
        self.add_widget(self.btn)
        if corners is not None:
            self.btn._bg_shape.radius = corners
        if on_tap is not None:
            self.btn.bind(on_release=lambda *_: on_tap())

        # 内容行（铺在按钮之上）
        row = BoxLayout(orientation="horizontal", size_hint=(1, 1),
                        padding=(dp(16), 0), spacing=dp(8))
        # 标题宽度：有右侧值时要给它留地方，所以固定宽度；动作行 /
        # 选择页的标题就是内容本身，让它撑满（否则长标签会被切掉）。
        self.lbl_title = Label(text=title,
                               size_hint_x=1 if full_title else None,
                               width=0 if full_title else dp(112),
                               font_size=dp(16),
                               color=C_ACCENT if accent else C_TEXT,
                               halign="left", valign="middle",
                               **fonts.font_kwargs())
        self.lbl_title.bind(size=lambda b, v: setattr(
            b, "text_size", (max(1.0, v[0]), None)))
        row.add_widget(self.lbl_title)

        self.lbl_value = Label(text=value, font_size=dp(16), color=C_DIM,
                               halign="right", valign="middle",
                               shorten=True, shorten_from="left",
                               **fonts.font_kwargs())
        self.lbl_value.bind(size=lambda b, v: setattr(
            b, "text_size", (max(1.0, v[0]), None)))
        row.add_widget(self.lbl_value)
        self.add_widget(row)

        # 记下尾部图标类型：纯粹为了可自省（探针/测试想知道这行有没有
        # 「>」或「✓」），不参与绘制。
        self.trailing = trailing
        self.ico = Widget(size_hint=(None, None), size=(dp(20), self._h),
                          pos_hint={"right": 1.0})
        if trailing:
            draw_icon(self.ico, trailing,
                      color=C_ACCENT if trailing == "check" else C_FAINT)
        self.add_widget(self.ico)
        self.bind(value=self._on_value)

        # 行间分隔线画在行自己的画布上：左边内缩 16dp（和 iOS 一致），
        # 而且不用额外加一个 0.5dp 高的控件 —— 那种控件在 dp 取整后
        # 经常直接消失，是「分隔线时有时无」的经典原因。
        self._sep = None
        if sep_below:
            with self.canvas.after:
                Color(*C_SEP)
                self._sep = Line(width=dp(1))
            self.bind(pos=self._sync_sep, size=self._sync_sep)
            self._sync_sep()

    def _sync_sep(self, *_):
        if self._sep is None:
            return
        self._sep.points = [self.x + dp(16), self.y, self.x + self.width,
                            self.y]

    def _on_value(self, *_):
        self.lbl_value.text = self.value

    @property
    def row_height(self):
        return self._h


class GroupCard(BoxLayout):
    """iOS 分组卡片：一堆行 + 行间分隔线，四角圆、内部直角。

    Kivy 不会裁剪子控件，所以圆角是按行分配的：
      第一行圆上两角、最后一行圆下两角、中间直角。
    分隔线画在行自己的画布上（左侧留 16dp 内缩，和 iOS 一致），
    这样不用多加一个 0.5dp 高的控件（那种控件在 dp 取整后经常消失）。
    """

    def __init__(self, radius=R_CARD, **kw):
        super().__init__(orientation="vertical", size_hint_y=None,
                         spacing=0, **kw)
        self.bind(minimum_height=self.setter("height"))
        self._radius = radius

    def add_row(self, row, sep_below=True):
        row.size_hint_y = None
        row.height = row.row_height if hasattr(row, "row_height") else dp(H_ROW)
        self.add_widget(row)
        self._restyle()
        return row

    def _restyle(self):
        """按行在卡片里的位置分配圆角（首行圆上、末行圆下、中间直角）"""
        rs = [dp(self._radius)] * 4
        n = len(self.children)          # children 是倒序
        for i, ch in enumerate(reversed(self.children)):     # i=0 是第一行
            shape = getattr(getattr(ch, "btn", ch), "_bg_shape", None)
            if shape is None:
                continue
            if n == 1:
                shape.radius = rs
            elif i == 0:
                shape.radius = [rs[0], rs[1], 0, 0]
            elif i == n - 1:
                shape.radius = [0, 0, rs[2], rs[3]]
            else:
                shape.radius = [0, 0, 0, 0]


class ActionRow(GroupRow):
    """分组里的「动作行」（例如「从文件选择音源…」）—— 蓝色文字，无值。"""

    def __init__(self, title="", **kw):
        kw.setdefault("value", "")
        kw.setdefault("trailing", None)
        kw.setdefault("accent", True)
        kw.setdefault("full_title", True)
        super().__init__(title=title, **kw)


# ============================================================
#  弹窗（遮罩 + 底部卡片）
# ============================================================
class ModalLayer(FloatLayout):
    """全屏遮罩 + 贴着底部的卡片 —— 设置抽屉与二级选择页都用这一套。

    要点：
      * 遮罩是**真控件**（Scrim），所以它会吞掉触摸事件，底层控件点不到。
      * 卡片动的是 **height**：在 FloatLayout 里 pos 会被布局按 pos_hint
        重算，动 pos 会被抢回去（Kivy 的既有行为），height 才是安全的自由度。
        卡片里的内容是贴着卡片顶部排的，所以 height 长起来时看起来就是
        「整块从底部滑上来」，和 iOS 一致。
      * 弹出用 SPRING_SHEET（阻尼比 0.8 / 响应 0.3）—— 苹果官方给
        抽屉和底部弹窗的参数，落地时会有一点阻尼感而不是硬停。
    """

    def __init__(self, height, scrim_alpha=SCRIM_ALPHA, dismiss_on_tap=True,
                 radius=R_SHEET, on_closed=None, **kw):
        super().__init__(**kw)
        self.height_max = float(height)
        # 收回动画跑完（或保险丝到点）后回调一次 —— 调用方用它把
        # 用完的选择页从控件树上摘掉，不然会一直堆在 root 里。
        self._on_closed = on_closed
        self.scrim = Scrim(on_tap=self.dismiss if dismiss_on_tap else None,
                           active=False)
        self.scrim.fill_alpha = 0.0
        self.add_widget(self.scrim)

        self.card = BoxLayout(orientation="vertical", size_hint=(1, None),
                              height=0, padding=(0, 0))
        # 顺序：先 glass（半透明+受光），再铺一层更实的底，
        # 否则背后的内容会透得太厉害，文字发灰看不清。
        rounded(self.card, C_SHEET, corners=[dp(radius)] * 4)
        glass(self.card, radius=radius, tint=(1, 1, 1, 0.035))
        self.add_widget(self.card)

        # 头部：拖拽指示条 + 右侧可放关闭按钮
        self.head = FloatLayout(size_hint_y=None, height=dp(30))
        self.grabber = Grabber(pos_hint={"center_x": 0.5, "center_y": 0.55})
        self.head.add_widget(self.grabber)
        self._drag = None
        self.head.bind(on_touch_down=self._head_down,
                       on_touch_move=self._head_move,
                       on_touch_up=self._head_up)
        self.card.add_widget(self.head)

        self.sv = ScrollView(do_scroll_x=False, bar_width=dp(2),
                             bar_color=(1, 1, 1, 0.18),
                             bar_inactive_color=(1, 1, 1, 0.08),
                             size_hint_y=None)
        self.body = BoxLayout(orientation="vertical", size_hint_y=None,
                              padding=(dp(16), dp(4)), spacing=dp(GAP_GROUP))
        self.body.bind(minimum_height=self.body.setter("height"))
        self.sv.add_widget(self.body)
        self.card.add_widget(self.sv)

        # ScrollView 的高度跟着卡片走（卡片高度是动画出来的）
        self.card.bind(height=self._sync_sv)
        self.card.bind(size=self._sync_card)
        self._scrim_alpha = scrim_alpha
        self._open = False
        self._fuse = None
        self._sync_card()

    def _sync_card(self, *_):
        self.card.size = (self.width, self.card.height)
        self.scrim.size = self.size
        self.scrim.pos = self.pos

    def _sync_sv(self, *_):
        h = max(dp(40), self.card.height - self.head.height
                - self.card.padding[1] * 2)
        self.sv.height = h

    # ---- 头部拖拽（往下拖就关掉，iOS 的标准手势）----
    def _head_down(self, w, touch):
        if w.collide_point(*touch.pos) and self._open:
            self._drag = touch.y
        return False

    def _head_move(self, w, touch):
        if self._drag is not None and self._open:
            dy = self._drag - touch.y
            if dy > dp(6):
                # 跟手：手指把卡片往下带
                self.card.height = max(0.0, self.height_max - dy)
            return True
        return False

    def _head_up(self, w, touch):
        if self._drag is None:
            return False
        dy = self._drag - touch.y
        self._drag = None
        if dy > dp(60):
            self.dismiss()
        else:
            spring_to(self.card, "height", self.height_max, *SPRING_SHEET)
        return True

    # ---- 弹出 / 收回 ----
    def present(self, on_done=None):
        self._open = True
        self.scrim.active = True
        spring_to(self.card, "height", self.height_max, *SPRING_SHEET,
                  on_done=on_done)
        spring_to(self.scrim, "fill_alpha", self._scrim_alpha, *SPRING_MOVE)
        self._arm_fuse(True)
        return self

    def dismiss(self, on_done=None):
        self._open = False

        def _done():
            self._notify_closed()
            if on_done is not None:
                on_done()

        spring_to(self.scrim, "fill_alpha", 0.0, *SPRING_MOVE,
                  on_done=lambda: setattr(self.scrim, "active", False))
        spring_to(self.card, "height", 0.0, *SPRING_SHEET, on_done=_done)
        self._arm_fuse(False, extra=self._notify_closed)
        return self

    def _arm_fuse(self, opened, extra=None):
        """保险丝：动画没跑起来也要落到终态，否则卡片卡在半开 ——
        那比没有动效糟得多（用户看不到设置，也点不到东西）。

        必须是**可取消**的一条：先弹出再很快收回（用户手快）时，
        弹出那条晚 1.8s 触发会把已经收好的卡片又顶开 —— 看起来就是
        「关不掉的抽屉」。
        """
        self._cancel_fuse()

        def _fire(*_):
            self._fuse = None
            self.snap(opened)
            if extra is not None:
                extra()

        self._fuse = Clock.schedule_once(_fire, FUSE_DELAY)

    def _cancel_fuse(self):
        if self._fuse is not None:
            self._fuse.cancel()
            self._fuse = None

    def _notify_closed(self):
        """收回完成回调（幂等：弹簧和保险丝都会调，只生效一次）"""
        cb, self._on_closed = self._on_closed, None
        if cb is not None:
            try:
                cb()
            except Exception:
                log_exc("modal.on_closed")

    def snap(self, opened):
        """把状态直接落到终态（保险丝 / 测试用）"""
        try:
            if opened:
                self.card.height = self.height_max
                self.scrim.fill_alpha = self._scrim_alpha
                self.scrim.active = True
                self._open = True
            else:
                self.card.height = 0.0
                self.scrim.fill_alpha = 0.0
                self.scrim.active = False
                self._open = False
        except Exception:
            log_exc("modal.snap")

    @property
    def is_open(self):
        return self._open


class PickerSheet(ModalLayer):
    """二级选择页：底部再长出一层列表（iOS 的推入式选择页等价物）。

    用法（main.py 里）：
        p = PickerSheet(title, options, current, on_pick, actions=[...])
        p.present()
    选完自动收回。选择结果只通过 on_pick 回调往外给一个字串 ——
    本文件不认识「音源」「品质」这些概念，也不碰任何业务状态。
    """

    def __init__(self, title="选择", options=(), current=None, on_pick=None,
                 actions=(), **kw):
        try:
            from kivy.core.window import Window
            h = min(dp(560), max(dp(260), Window.height * 0.62))
        except Exception:
            h = dp(420)
        super().__init__(height=h, **kw)
        self._on_pick = on_pick

        # 头部：返回按钮 + 标题
        back = IconButton("back", dia=dp(34), icon_color=C_ACCENT, bg=(0, 0, 0, 0))
        back.pos_hint = {"x": 0.02, "center_y": 0.5}
        back.bind(on_release=lambda *_: self.dismiss())
        self.head.add_widget(back)
        cap = Label(text=title, font_size=dp(17), bold=True, color=C_TEXT,
                    halign="center", valign="middle", **fonts.font_kwargs())
        cap.size_hint = (0.7, 1)
        cap.pos_hint = {"center_x": 0.5}
        cap.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        self.head.add_widget(cap)

        card = GroupCard()
        for text in options:
            picked = str(text) == str(current)
            row = GroupRow(title=str(text), trailing="check" if picked else None,
                           full_title=True, sep_below=True)
            row.lbl_title.color = C_ACCENT if picked else C_TEXT
            row.btn.bind(on_release=lambda *_, t=text: self._pick(t))
            card.add_row(row)
        for label, cb in actions:
            row = ActionRow(title=label, sep_below=bool(options))
            row.btn.bind(on_release=lambda *_, c=cb: self._action(c))
            card.add_row(row)
        self.body.add_widget(card)
        self.body.add_widget(Widget(size_hint_y=None, height=dp(8)))

    def _pick(self, text):
        cb = self._on_pick
        self.dismiss()
        if cb is not None:
            cb(text)

    def _action(self, cb):
        self.dismiss()
        Clock.schedule_once(lambda *_: cb(), 0.05)


# ============================================================
#  搜索结果行
# ============================================================
class SongRow(FloatLayout):
    """搜索结果的一行：**点歌名直接播放**，点右侧图标下载。

    两个动作分在两层：整行是一个按钮（播放），右边浮一个图标按钮（下载）。
    用 FloatLayout 而不是水平 BoxLayout，是为了让「播放按钮铺满整行、
    下载按钮浮在右侧」—— 这样点歌名那一大片都有效，符合 HIG 的
    「触摸目标要大」。

    序号弱化、歌名主文本、歌手/时长再降一档 —— iOS 列表的层级感靠这个
    「同一行里三种明度」，不是靠框线。
    """

    def __init__(self, index=1, title="", subtitle="", on_play=None,
                 on_download=None, height=None, **kw):
        super().__init__(size_hint_y=None, height=height or dp(58), **kw)
        self.btn = FlatButton(text="", bg_color=C_ROW, down_color=C_ROW_DOWN,
                              radius=R_ROW)
        self.btn.size_hint = (1, 1)
        if on_play is not None:
            self.btn.bind(on_release=lambda *_: on_play())
        self.add_widget(self.btn)

        # 右侧下载按钮：固定 44dp（HIG 的最小触摸目标），垂直居中
        self.btn_dl = IconButton("download", dia=dp(H_TOUCH),
                                 icon_color=C_ACCENT, bg=(0, 0, 0, 0))
        self.btn_dl.pos_hint = {"right": 0.97, "center_y": 0.5}
        if on_download is not None:
            self.btn_dl.bind(on_release=lambda *_: on_download())
        self.add_widget(self.btn_dl)

        # 文本块：右边留出按钮的宽度，否则长歌名会钻到按钮下面
        col = BoxLayout(orientation="vertical", size_hint=(1, 1),
                        padding=(dp(14), dp(8), dp(H_TOUCH + 8), dp(8)),
                        spacing=dp(1))
        top = BoxLayout(orientation="horizontal", size_hint_y=0.56,
                        spacing=dp(6))
        self.lbl_idx = Label(text=str(index), size_hint_x=None, width=dp(24),
                             font_size=dp(13), color=C_FAINT,
                             halign="left", valign="middle",
                             **fonts.font_kwargs())
        self.lbl_idx.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        self.lbl_name = Label(text=title, font_size=dp(15), color=C_TEXT,
                              halign="left", valign="middle",
                              shorten=True, shorten_from="right",
                              **fonts.font_kwargs())
        self.lbl_name.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        top.add_widget(self.lbl_idx)
        top.add_widget(self.lbl_name)

        self.lbl_sub = Label(text=subtitle, size_hint_y=0.44,
                             font_size=dp(12), color=C_DIM,
                             halign="left", valign="middle",
                             **fonts.font_kwargs())
        self.lbl_sub.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        col.add_widget(top)
        col.add_widget(self.lbl_sub)
        self.add_widget(col)


def follow(row, src, transform=None):
    """让列表行的显示值跟着某个控件走（**只读**，不反向写）。

    main.py 里那些 spinner 是状态的真身，行只是把它显示出来。
    这样「谁拥有数据」很清楚：行永远不写状态，只读。
    """

    def _sync(*_):
        try:
            v = getattr(src, "text", "") or ""
            row.value = transform(v) if transform else v
        except Exception:
            log_exc("follow")

    src.bind(text=_sync)
    _sync()
    return row
