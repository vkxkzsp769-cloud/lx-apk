"""落雪音源下载器 —— App 入口（界面 + 编排）

模块划分
--------
  appenv      运行环境：日志、路径、存储权限、内置音源
  fonts       中文字体注册
  lxbridge    WebView JS 引擎（跑落雪音源 .js，取音频直链）
  searchers   多平台搜索（wy/tx/kw/kg/mg，音源本身不含搜索）
  downloader  下载音频 + 直链有效性校验
  main        界面与业务编排（本文件）

线程模型（全工程唯一的约定，务必遵守）
------------------------------------
  * Kivy 事件循环 == Python 主线程；所有控件只能在主线程改。
  * 会阻塞的事（搜索 / 取直链 / 下载 / JS 调用）一律放后台线程。
  * 后台线程要改界面，一律用 self.ui(fn) 转回主线程。
  * LxBridge 的 JS 调用必须在「非」主线程执行 —— 因为
    evaluateJavascript 的回调要靠主线程派发，在主线程阻塞会死锁。
"""
import json
import os
import threading
import traceback

from kivy.app import App
from kivy.animation import Animation
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.properties import ListProperty, NumericProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.slider import Slider
from kivy.uix.scrollview import ScrollView
from kivy.uix.dropdown import DropDown
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget
from kivy.uix.floatlayout import FloatLayout

import appenv
import bgfx
import downloader
import fonts
import searchers
import player as player_mod
import qqresolve
import songinfo
from appenv import (IS_ANDROID, SOURCE_FILE, default_source_path, diag,
                    download_dir, ensure_source, extract_bundled_sources, log,
                    log_exc, preset_dirs, request_all_files_access,
                    save_source, set_download_dir, source_title,
                    tree_uri_to_path)
from lxbridge import LxBridge

# ============================================================
#  主题
# ============================================================
#  设计令牌
# ============================================================
# 暗黑极简：深邃深灰底 + 稍亮卡片 + 细微边框高光。
# 主色刻意用**低饱和**蓝紫渐变，不用刺眼的纯色。
C_BG = (0.059, 0.059, 0.067, 1)       # #0F0F11 页面底色
C_CARD = (0.086, 0.086, 0.098, 0.94)  # 卡片（稍亮，略带透明让背景透出来）
C_CTRL = (0.118, 0.118, 0.137, 1)     # 输入/下拉
C_ITEM = (0.098, 0.098, 0.114, 1)     # 列表项
C_SEP = (1, 1, 1, 0.055)              # 边框高光 / 分隔线
C_ACCENT = (0.42, 0.45, 0.95, 1)      # 主色（低饱和蓝）
C_ACCENT_D = (0.30, 0.32, 0.72, 1)
# 蓝紫渐变的两端（胶囊按钮、进度条用）
GRAD_A = (0.42, 0.45, 0.95)           # #6B73F2 蓝
GRAD_B = (0.58, 0.42, 0.95)           # #946BF2 紫
C_TEXT = (0.96, 0.96, 0.97, 1)
C_DIM = (0.62, 0.63, 0.68, 1)
C_FAINT = (0.40, 0.41, 0.45, 1)
C_OK = (0.345, 0.800, 0.502, 1)
C_ERR = (0.949, 0.451, 0.451, 1)

# 圆角：大圆角是这个风格的关键（16~24）
R_LG = 24
R_MD = 16
R_SM = 12

# 「毛玻璃」近似参数。
# Kivy 的 canvas 没有实时模糊（backdrop-blur），所以做法是：
#   半透明填充 + 细微边框高光 + 一层低对比竖向渐变叠色。
# 观感接近毛玻璃，但严格说不是真模糊 —— 这一点跟用户说明过。
GLASS_FILL = (1, 1, 1, 0.045)
GLASS_HI = (1, 1, 1, 0.10)

PLATFORM_LABEL = {"wy": "网易云", "tx": "QQ音乐", "kw": "酷我",
                  "kg": "酷狗", "mg": "咪咕", "qsvip": "企鹅SVIP"}
# 顺序即下拉顺序，第一项是 Spinner 的默认值 —— 所以 320k 放最前
QUALITY_ORDER = ["320k", "128k", "192k", "flac", "flac24bit",
                 "hires", "master"]
# 搜索结果数量选项（None = 尽量多拿，上限见 searchers.MAX_RESULTS）
COUNT_ORDER = ["20 首", "50 首", "100 首", "200 首", "300 首", "全部"]
COUNT_VALUE = {"20 首": 20, "50 首": 50, "100 首": 100,
               "200 首": 200, "300 首": 300, "全部": None}

TIME_FMT = "%02d:%02d"


def fmt_time(sec):
    try:
        sec = int(max(0, sec))
    except Exception:
        return "00:00"
    return TIME_FMT % (sec // 60, sec % 60)


# ============================================================
#  控件
# ============================================================
class CNSpinnerOption(SpinnerOption):
    """下拉列表项。

    Spinner 的选项项默认是 Kivy 自带的灰底渐变按钮 —— 就是用户说的
    「像老安卓」。这里换成和界面一致的深色圆角行 + 中文字体 + 左对齐。

    两个坑：
      * 选项项**不会**继承 Spinner 的字体，不传就是方块
      * Spinner._update_dropdown_size 会把每项高度强制设成 Spinner 的高度，
        所以高度不用自己定，但 size_hint_y 必须是 None

    背景用 attach_bg() 而不是继承 FlatButton —— 后者定义在本类之后，
    写在基类位置上会在 import 时就 NameError。
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
        kw.setdefault("font_size", dp(14))
        kw.setdefault("color", C_TEXT)
        super().__init__(**kw)
        attach_bg(self, C_ITEM, radius=9)
        self.bind(size=lambda b, v: setattr(b, "text_size", (v[0] - dp(22), None)))


class CNDropdown(DropDown):
    """下拉框本体。

    默认 DropDown 是个裸 ScrollView + 裸 GridLayout：没有背景、没有留白、
    还带一条滚动条，拉开就是一列灰色方块。这里给它铺上深色圆角底、
    加内边距、隐藏滚动条，并限制最大高度（否则长列表会铺满整屏）。
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
                attach_bg(c, C_CARD, radius=12)
        except Exception:
            log_exc("CNDropdown 背景")


class CNSpinner(Spinner):
    """下拉框。做三件事：

    1) 下拉列表项不会继承 font_name，中文会显示成方块 —— 用 option_cls
       在创建时就带上字体。
    2) 默认的下拉外观是「老安卓」风格 —— 换成 dropdown_cls + 深色圆角。
    3) Kivy 的 Spinner 默认用灰底贴图，跟这套深色主题不搭，
       所以关掉贴图改用纯色（background_normal=""）。

    注意：不要重新声明 font_name！
    Label 自己就有 font_name（默认 'Roboto'），重新声明成
    StringProperty(None) 会把默认值覆盖成 None，
    于是 Kivy 的 resolve_font_name() 拿到 None 后崩：
      AttributeError: 'NoneType' object has no attribute 'endswith'
    这个崩只在「没找到中文字体」时触发（那时不会传 font_name），
    很容易漏测。
    """

    def __init__(self, **kw):
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", C_CTRL)
        kw.setdefault("color", C_TEXT)
        kw.setdefault("option_cls", CNSpinnerOption)   # 下拉项：深色圆角 + 中文字体
        kw.setdefault("dropdown_cls", CNDropdown)      # 下拉框：深色圆角 + 留白
        super().__init__(**kw)


def vibrate(ms=12):
    """极短的触感反馈。

    没有 VIBRATE 权限、或不在 Android 上，就静默跳过 ——
    触感只是锦上添花，绝不能因为它让点击失效。
    """
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        svc = act.getSystemService("vibrator")
        svc.vibrate(int(ms))
    except Exception:
        pass


def attach_bg(widget, color, radius=0):
    """给控件加背景（可选圆角）。

    Kivy 没有 canvas_before 这个属性（它是 widget.canvas.before 对象），
    当构造参数传会抛 TypeError 导致启动即崩 —— 必须建好后再加图元。
    """
    from kivy.graphics import Color, Rectangle, RoundedRectangle
    with widget.canvas.before:
        Color(*color)
        if radius:
            shape = RoundedRectangle(pos=widget.pos, size=widget.size,
                                     radius=[dp(radius)])
        else:
            shape = Rectangle(pos=widget.pos, size=widget.size)

    def _sync(w, *_):
        shape.pos = w.pos
        shape.size = w.size

    widget.bind(pos=_sync, size=_sync)
    return widget


def grad_buffer(c1, c2, size=64, horizontal=True):
    """按 colorfmt='rgb' 生成渐变像素缓冲。

    抽出来是为了能**在没有 GL 的机器上验证字节数** ——
    贴图本身要 Texture.create（要 GL），但缓冲区长度对不对是纯算术。

    ⚠ 这里踩过一个坑：colorfmt="rgb" 是**每像素 3 字节**，
    但最初写成 `bytes(px + (255,))` 往每像素塞了 4 字节 ——
    长度对不上，纹理读进去就是错位数据，整片渐变色乱掉
    （用户看到的就是「界面颜色乱了」）。所以断言里要卡死字节数。
    """
    per_px = 3
    out = bytearray()
    for i in range(size):
        t = i / float(size - 1)
        px = bytes(int(255 * (c1[k] + (c2[k] - c1[k]) * t)) for k in range(3))
        if len(px) != per_px:                     # 自检，防止再写回 4 字节
            raise AssertionError("每像素必须是 %d 字节，实际 %d" % (per_px, len(px)))
        # 横向贴图是 size×1（每行 1 像素，共 size 个像素）；
        # 竖向贴图是 1×size（每行也只有 1 个像素，共 size 行）——
        # 两种情况**都是** size 个像素，所以都是直接追加一个像素，不能乘 size。
        out += px
    expect = (size * 1 if horizontal else 1 * size) * per_px
    if len(out) != expect:
        raise AssertionError("缓冲长度 %d 不等于 w*h*3=%d" % (len(out), expect))
    return bytes(out)


def make_grad_texture(c1, c2, size=64, horizontal=True):
    """生成两端渐变的贴图（供胶囊按钮 / 进度条用）。

    为什么用贴图：Kivy 的 Color/Rectangle 只能画纯色，没有渐变。
    自己算一张小贴图拉伸，是唯一能做出渐变的路子。
    """
    from kivy.graphics.texture import Texture
    dims = (size, 1) if horizontal else (1, size)
    tex = Texture.create(size=dims, colorfmt="rgb")
    tex.blit_buffer(grad_buffer(c1, c2, size, horizontal),
                    colorfmt="rgb", bufferfmt="ubyte")
    tex.wrap = "clamp_to_edge"
    tex.mag_filter = "linear"
    tex.min_filter = "linear"
    return tex


def attach_gradient(widget, c1, c2, radius=R_LG, horizontal=True):
    """给控件加渐变圆角背景（描边由 attach_border 单独负责）"""
    from kivy.graphics import Color, RoundedRectangle
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
    plain = getattr(widget, "_bg_color", None)
    if plain is not None:
        plain.rgba = (0, 0, 0, 0)
    return widget


def attach_border(widget, radius=R_MD, color=None, width=1.0):
    """细微边框高光。

    暗色界面里卡片如果只有填充色，会「糊」在一起分不出层级 ——
    一圈极淡的描边是最省力的分层手段（border highlight）。
    """
    from kivy.graphics import Color, Line
    col = color or C_SEP
    with widget.canvas.after:
        c = Color(*col)
        line = Line(width=dp(width), rounded_rectangle=(0, 0, 1, 1, dp(radius)))

    def _sync(w, *_):
        line.rounded_rectangle = (w.x, w.y, w.width, w.height, dp(radius))

    widget.bind(pos=_sync, size=_sync)
    _sync(widget)
    # 保持引用，防止被 GC
    widget._border_line = line
    widget._border_color = c
    return widget


def attach_glow(widget, color=None, spread=None, alpha=0.30):
    """柔和的发光阴影（暗色界面里 shadow 不能用黑色，看不见 —— 得用彩色光）。

    用背景模块那张径向渐变贴图拉伸成椭圆铺在控件**后面**。
    必须在 attach_bg / attach_gradient **之前**调用，canvas.before 里
    按插入顺序绘制，后加的会盖在前面。

    注意 spread 的默认值写成 None、在函数体里算 dp()：默认参数是在
    **模块导入时**求值的，而 dp() 依赖 Metrics（要窗口），
    在窗口就绪前求值会直接把导入搞崩（本次实测踩到）。
    """
    from kivy.graphics import Color, Rectangle
    if spread is None:
        spread = dp(18)
    r, g, b = (color or C_ACCENT)[:3]
    tex = bgfx.make_glow_texture()
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


class FlatButton(Button):
    """扁平圆角按钮。

    背景用自绘圆角矩形，所以要把 Kivy 默认的灰色贴图关掉
    （background_normal="" + background_color 透明）。
    """
    bg_color = ListProperty([0.24, 0.27, 0.34, 1])
    radius = NumericProperty(8)

    def __init__(self, **kw):
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", (0, 0, 0, 0))   # 关掉默认灰底
        super().__init__(**kw)
        # 注意：bg_color 是通过 kwargs 设进来的，Kivy 会在 super().__init__()
        # 里就触发 on_bg_color，那时 canvas 图元还不存在 —— 所以那里必须判空。
        from kivy.graphics import Color, RoundedRectangle
        with self.canvas.before:
            self._bg_color = Color(*self.bg_color)
            self._bg_shape = RoundedRectangle(
                pos=self.pos, size=self.size, radius=[dp(self.radius)])
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_):
        self._bg_shape.pos = self.pos
        self._bg_shape.size = self.size

    def _apply_bg(self):
        c = getattr(self, "_bg_color", None)
        if c is None:
            return
        # 渐变按钮：纯色底已经被 attach_gradient 压成全透明，
        # 这里**不能**再按 bg_color 画回来 —— 否则一按下去就会冒出一块
        # 灰蓝纯色盖住渐变（用户反馈「颜色乱了」的原因之一）。
        if getattr(self, "_grad_shape", None) is not None:
            return
        r, g, b, a = self.bg_color
        # 按下时压暗一点。原来 background_normal/down 都设成空串，
        # 于是按钮**完全没有触摸反馈**，点下去像没反应。
        k = 0.78 if self.state == "down" else 1.0
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

    松手用 out_back 缓动，比线性更有「弹」的感觉。
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
        Animation(
            _press=1.0 if down else 0.0,
            duration=0.07 if down else 0.24,
            t="out_quad" if down else "out_back",
        ).start(self)

    def _sync(self, *_):
        shape = getattr(self, "_grad_shape", None) or getattr(self, "_bg_shape", None)
        if shape is None:
            return
        grow = 1.0 - self._press * (1.0 - self.press_scale)
        w, h = self.width * grow, self.height * grow
        shape.pos = (self.x + (self.width - w) / 2.0,
                     self.y + (self.height - h) / 2.0)
        shape.size = (w, h)


def icon_parts(kind, w, h, ox=0.0, oy=0.0, unit=1.0):
    """把图标拆成**父坐标系**下的图元描述（纯函数，不碰 Kivy，便于无 GL 断言）。

    背景：往 widget.canvas 加的图元用的是**父坐标系**（和 widget.pos 同一空间），
    而早期版本的 draw_icon 用本地坐标 `cx, cy = w/2, h/2` 算几何 ——
    图标全被画到父容器原点附近去了（用户反馈「叉不在应该在的地方」）。
    把几何抽在这里，就能在没有 OpenGL 的机器上直接断言坐标对不对。

    ox/oy = 控件在父坐标系里的原点（widget.x / widget.y）；
    unit  = 一个 dp 对应的像素数（线宽用它换算 —— 纯函数里不能调 dp()）。
    返回 [(op, kwargs), ...]，op ∈ {"line", "mesh"}。
    """
    s = min(w, h) * 0.5
    cx, cy = ox + w / 2.0, oy + h / 2.0
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
        out.append(("mesh", {"vertices": [cx - s * 0.34, cy - s * 0.5, 0, 0,
                                          cx - s * 0.34, cy + s * 0.5, 0, 0,
                                          cx + s * 0.52, cy, 0, 0],
                             "indices": [0, 1, 2], "mode": "triangles"}))
    elif kind == "pause":
        bw = s * 0.26
        for dx in (-0.34, 0.08):
            out.append(("line", {"rectangle": (cx + s * dx, cy - s * 0.48,
                                               bw, s * 0.96),
                                 "width": 1.2 * unit}))
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
    return out


def draw_icon(widget, kind, color=None, size=None):
    """把图标**画**在控件上，而不是用字符。

    为什么不直接写 ⚙ / 🔍 这类字符：自带的中文字体是从 Noto Sans SC
    裁出来的，emoji 和大部分几何符号根本不在里面，写上去就是方块。
    自己用 canvas 画最稳，而且线条更锐利、更贴合极简风格。

    ⚠ 图元必须用**父坐标系**（widget.x / widget.y 当原点）：
    往 widget.canvas 加的东西和 widget.pos 在同一个坐标空间，
    用本地坐标（0..w）会把图标画到父容器原点上去 ——
    这就是「×」跑到屏幕左下角、按钮上只剩一个空圆的原因。
    """
    from kivy.graphics import Color, InstructionGroup, Line, Mesh
    col = color or C_TEXT
    _DRAW = {"line": Line, "mesh": Mesh}

    def _redraw(*_):
        w, h = max(1.0, widget.width), max(1.0, widget.height)
        # 图标画进自己的 InstructionGroup：不能用 canvas.after.clear()，
        # 那会把 attach_border 加在同一 canvas.after 上的边框一起清掉。
        grp = getattr(widget, "_icon_group", None)
        if grp is None or grp not in widget.canvas.after.children:
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


class IconButton(SpringButton):
    """只有图标的圆形按钮"""

    def __init__(self, kind, dia=None, color=None, bg=(1, 1, 1, 0.06),
                 icon_color=None, **kw):
        # dp() 不能在默认参数里求值（导入期就要窗口），所以在这里算
        if dia is None:
            dia = dp(40)
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dia, dia))
        kw.setdefault("bg_color", bg)
        kw.setdefault("color", (0, 0, 0, 0))   # 不显示文字
        super().__init__(**kw)
        self._bg_shape.radius = [dia / 2.0]    # 正圆
        draw_icon(self, kind, color=icon_color)


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
#  主界面
# ============================================================
class LxApp(App):
    title = "落雪音源下载器"
    # 抽屉遮罩的不透明度（动画驱动）
    _scrim_a = NumericProperty(0.0)

    def on__scrim_a(self, *_):
        c = getattr(self, "_scrim_c", None)
        if c is not None:
            c.a = self._scrim_a

    # ---------- 生命周期 ----------
    def build(self):
        self.bridge = LxBridge()
        self.player = player_mod.Player()
        self.songs = []           # 搜索结果
        self.platforms = []       # [{source,name,qualitys}]
        self.busy = False
        self._asked_permission = False
        self._cur_duration = 0.0  # 当前播放时长（秒）
        self._dragging = False    # 用户正在拖进度条
        self._cur_song = None
        self._cur_url = None
        self._cur_ext = None
        self._cur_dur = 0.0
        self._popup = None
        self.F = fonts.font_kwargs()
        self.P = fonts.popup_kwargs()      # Popup 标题的字体参数名是 title_font

        root = BoxLayout(orientation="vertical")
        attach_bg(root, C_BG)
        # 动态背景必须在 attach_bg 之后建：canvas.before 里按插入顺序绘制，
        # 光斑要压在底色之上、卡片之下（卡片半透明，光斑会透出来）。
        # 兜底：背景只是装饰，建不起来也绝不能拦住 App 启动。
        try:
            self.bgfx = bgfx.MusicBackground(root)
        except Exception:
            log_exc("创建动态背景")
            self.bgfx = None

        # 主内容区（顶部栏 / 搜索条 / 列表 / 悬浮播放栏）
        self.main = BoxLayout(orientation="vertical")
        self.main.add_widget(self._build_header())
        self.main.add_widget(self._build_search())
        self.main.add_widget(self._build_results())
        self.main.add_widget(self._build_footer())
        root.add_widget(self.main)

        # 设置抽屉打开时压暗主内容 —— 用 canvas.after 盖一层黑，
        # 它在子控件**之后**绘制，所以能盖住列表（Kivy 的 canvas 不裁剪，
        # 但这一层正好只需要盖住主内容区）。
        from kivy.graphics import Color as _C, Rectangle as _R
        with self.main.canvas.after:
            self._scrim_c = _C(0, 0, 0, 0.0)
            self._scrim_r = _R(pos=self.main.pos, size=self.main.size)
        self.main.bind(pos=self._sync_scrim, size=self._sync_scrim)

        # 底部抽屉：初始高度 0（收在屏幕外）。
        # 为什么动画高度而不是位置：在 BoxLayout/FloatLayout 里做位置动画
        # 会和布局的排布互相覆盖，高度是唯一不会被抢回去的自由度。
        self.sheet = BoxLayout(orientation="vertical", size_hint_y=None,
                               height=0, padding=(dp(16), dp(8)),
                               spacing=dp(6))
        attach_bg(self.sheet, C_CARD, radius=R_LG)
        attach_border(self.sheet, radius=R_LG)
        self.sheet.add_widget(self._sheet_head())
        # 套一层滚动：配置项不少，小屏上必须能滚，否则底部几项会被切掉
        sv = ScrollView(bar_width=dp(2), do_scroll_x=False)
        inner = self._build_panel()
        sv.add_widget(inner)
        self.sheet.add_widget(sv)
        # 抽屉目标高度：屏高的 72% 与 560dp 取小 —— 不遮住整个屏幕，
        # 留一点主内容能看见（并露出遮罩变暗的效果）
        try:
            from kivy.core.window import Window as _W
            self._sheet_h = min(dp(560), max(dp(300), _W.height * 0.72))
        except Exception:
            self._sheet_h = dp(460)
        root.add_widget(self.sheet)
        self._sheet_open = False

        Clock.schedule_once(self._guard(self._boot), 0.2)
        Clock.schedule_interval(self._guard(self._tick), 0.5)

        # 启动淡入：界面从透明浮出来，比「啪」一下出现柔和得多。
        # 保险丝：1.5s 后无条件把 opacity 拉回 1 —— 万一动画没跑起来，
        # 整个界面会是全透明的，那比没有动效糟得多（而且我看不到真机）。
        root.opacity = 0.0
        Clock.schedule_once(
            lambda *_: Animation(opacity=1.0, d=0.28, t="out_quad").start(root), 0.05)
        Clock.schedule_once(lambda *_: setattr(root, "opacity", 1.0), 1.5)
        return root

    def _guard(self, fn):
        """Clock 回调异常没人接管会直接终止 App，统一包一层"""
        def _w(*a, **k):
            try:
                return fn(*a, **k)
            except Exception:
                log_exc(getattr(fn, "__name__", "callback"))
                self.set_status("出错了，详见 crash.log", C_ERR)
        return _w

    # ---------- UI 组装 ----------
    def _build_header(self):
        """顶部只留标题 + 设置图标 —— 配置项全部收进底部抽屉。

        原来这里挂着一整块控制面板（音源/平台/品质/格式/数量），
        小屏上几乎把歌曲列表挤没了。现在只在右上角放一个图标。
        """
        box = BoxLayout(size_hint_y=None, height=dp(62),
                        padding=(dp(20), dp(14)), spacing=dp(10))
        t = Label(text="落雪音源下载器", bold=True, font_size=dp(20),
                  halign="left", valign="middle", color=C_TEXT, **self.F)
        t.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(t)
        self.lbl_sub = Label(text="", size_hint_x=None, width=dp(96),
                             font_size=dp(11), color=C_FAINT,
                             halign="right", valign="middle", **self.F)
        self.lbl_sub.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(self.lbl_sub)
        self.btn_set = IconButton("settings", dia=dp(42), icon_color=C_DIM)
        self.btn_set.bind(on_release=lambda *_: self.open_settings())
        box.add_widget(self.btn_set)
        return box

    def _build_search(self):
        """搜索条：长条形 + 内嵌放大镜 + 渐变胶囊按钮"""
        wrap = BoxLayout(size_hint_y=None, height=dp(58),
                         padding=(dp(16), dp(6)), spacing=dp(10))

        # 输入框做成圆角胶囊，放大镜画在它左内侧
        self.ti_box = BoxLayout(size_hint_y=None, height=dp(46))
        attach_bg(self.ti_box, C_CTRL, radius=R_LG)
        attach_border(self.ti_box, radius=R_LG, color=C_SEP)

        ico = Widget(size_hint=(None, None), size=(dp(34), dp(46)),
                     pos_hint={"center_y": 0.5})
        draw_icon(ico, "search", color=C_FAINT)
        self.ti_box.add_widget(ico)

        self.ti_search = SearchInput(hint_text="搜索歌曲或歌手", multiline=False,
                                     font_size=dp(15), padding=(dp(2), dp(12)),
                                     background_color=(0, 0, 0, 0),
                                     foreground_color=C_TEXT,
                                     hint_text_color=C_FAINT,
                                     cursor_color=C_ACCENT, **self.F)
        self.ti_search.bind(on_text_validate=self.do_search)
        self.ti_box.add_widget(self.ti_search)
        wrap.add_widget(self.ti_box)

        # 搜索按钮：渐变胶囊，与输入框等高
        self.btn_search = SpringButton(text="搜索", bold=True, font_size=dp(14),
                                       size_hint=(None, None),
                                       size=(dp(76), dp(46)),
                                       color=(1, 1, 1, 1), **self.F)
        attach_gradient(self.btn_search, GRAD_A, GRAD_B, radius=R_LG)
        self.btn_search.bind(on_release=self.do_search)
        wrap.add_widget(self.btn_search)
        return wrap

    def _build_panel(self):
        """设置抽屉的内容（全部配置项都收在这里）。

        注意：原来的 sp_source / sp_platform / sp_quality / sp_format /
        sp_count 仍然叫这些名字 —— 别的地方（_apply_source_info /
        _refresh_qualities / _resolve_song 等）都靠这些属性取控件，
        换名字会连环崩。
        """
        panel = BoxLayout(orientation="vertical", size_hint_y=None,
                          padding=(dp(18), dp(6)), spacing=dp(12))
        panel.bind(minimum_height=panel.setter("height"))

        # ---- 音源文件（内置多个，可切换）----
        panel.add_widget(self._section("音源"))
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.sp_source = CNSpinner(text="加载中…", values=[], font_size=dp(13),
                                   **self.F)
        self.sp_source.bind(on_text=self._on_source_picked)
        row.add_widget(self.sp_source)
        btn = self._btn("更换", color=C_CTRL, w=dp(72))
        btn.bind(on_release=self.pick_source)
        row.add_widget(btn)
        panel.add_widget(row)

        # ---- 平台 / 品质 ----
        panel.add_widget(self._section("平台与品质"))
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.sp_platform = CNSpinner(text="—", values=[], font_size=dp(14),
                                     **self.F)
        self.sp_platform.bind(text=lambda *_: self._refresh_qualities())
        row.add_widget(self.sp_platform)
        self.sp_quality = CNSpinner(
            text=songinfo.quality_label("320k"),
            values=[songinfo.quality_label(q) for q in QUALITY_ORDER],
            font_size=dp(14), **self.F)
        row.add_widget(self.sp_quality)
        panel.add_widget(row)

        # ---- 格式 + 数量 ----
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        row.add_widget(self._field("格式", w=dp(40)))
        self.sp_format = CNSpinner(text="自动", values=songinfo.FORMAT_ORDER,
                                   font_size=dp(14), size_hint_x=None,
                                   width=dp(104), **self.F)
        self.sp_format.bind(text=lambda *_: self._refresh_qualities())
        row.add_widget(self.sp_format)
        row.add_widget(self._field("数量", w=dp(40)))
        self.sp_count = CNSpinner(text="100 首", values=COUNT_ORDER,
                                  font_size=dp(14), size_hint_x=None,
                                  width=dp(104), **self.F)
        row.add_widget(self.sp_count)
        panel.add_widget(row)

        # ---- 下载位置 + QQ 代理（原来在独立的设置弹窗里）----
        panel.add_widget(self._section("下载保存位置"))
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.sp_dir = CNSpinner(text="—", values=[], font_size=dp(13), **self.F)
        self.sp_dir.bind(on_text=self._on_dir_preset)
        row.add_widget(self.sp_dir)
        btn_dir = self._btn("选目录", color=C_CTRL, w=dp(72))
        btn_dir.bind(on_release=self.pick_dir)
        row.add_widget(btn_dir)
        panel.add_widget(row)

        panel.add_widget(self._section("QQ 代理（失效时可自己换）"))
        self.btn_proxy = self._btn("选择代理文件(.txt)", color=C_CTRL, fs=dp(13))
        self.btn_proxy.bind(on_release=self.pick_proxies)
        panel.add_widget(self.btn_proxy)
        return panel

    def _sheet_head(self):
        """抽屉顶部的标题行 + 关闭按钮"""
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        t = Label(text="设置", bold=True, font_size=dp(16), color=C_TEXT,
                  halign="left", valign="middle", **self.F)
        t.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        row.add_widget(t)
        btn = IconButton("close", dia=dp(34), icon_color=C_DIM)
        btn.bind(on_release=lambda *_: self.close_settings())
        row.add_widget(btn)
        return row

    def _sync_scrim(self, *_):
        try:
            self._scrim_r.pos = self.main.pos
            self._scrim_r.size = self.main.size
        except Exception:
            pass

    def _section(self, text):
        lb = Label(text=text, size_hint_y=None, height=dp(20),
                   font_size=dp(11), color=C_FAINT, bold=True,
                   halign="left", valign="middle", **self.F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        return lb

    def _btn(self, text, color=None, w=None, bold=False, fs=None):
        b = FlatButton(text=text, size_hint_x=None if w else 1,
                       width=w or 0, font_size=fs or dp(14), bold=bold,
                       bg_color=color or C_CTRL, color=C_TEXT, **self.F)
        return b

    def _field(self, text, w=None):
        lb = Label(text=text, size_hint_x=None, width=w or dp(42),
                   font_size=dp(14),
                   color=C_DIM, halign="left", valign="middle", **self.F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        return lb

    def _build_results(self):
        wrap = BoxLayout(orientation="vertical", padding=(dp(12), dp(4)),
                         spacing=dp(4))
        self.hint = Label(text="搜索后点结果即可播放或下载", size_hint_y=None,
                          height=dp(26), font_size=dp(12), color=C_FAINT,
                          halign="left", valign="middle", **self.F)
        self.hint.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        wrap.add_widget(self.hint)
        self.sv = ScrollView(bar_width=dp(2), bar_color=(0.35, 0.4, 0.5, 1),
                             bar_inactive_color=(0.22, 0.25, 0.31, 1))
        self.results = BoxLayout(orientation="vertical", size_hint_y=None,
                                 spacing=dp(8), padding=(0, dp(2)))
        self.results.bind(minimum_height=self.results.setter("height"))
        self.sv.add_widget(self.results)
        wrap.add_widget(self.sv)
        return wrap

    def _build_footer(self):
        outer = BoxLayout(orientation="vertical", size_hint_y=None,
                          padding=(dp(10), dp(6)))
        outer.bind(minimum_height=outer.setter("height"))
        box = BoxLayout(orientation="vertical", size_hint_y=None,
                        padding=(dp(14), dp(12)), spacing=dp(10))
        attach_bg(box, C_CARD, radius=14)
        box.bind(minimum_height=box.setter("height"))
        outer.add_widget(box)

        # ---- 播放条 ----
        prow = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(10))
        self.btn_play = self._btn("播放", color=C_ACCENT, w=dp(64), fs=dp(14))
        self.btn_play.bind(on_release=lambda *_: self.toggle_play())
        prow.add_widget(self.btn_play)

        self.slider = Slider(min=0, max=1000, value=0, step=1,
                             cursor_size=(dp(18), dp(18)),
                             background_width=dp(3))
        self.slider.bind(on_touch_down=self._seek_down,
                         on_touch_up=self._seek_up)
        prow.add_widget(self.slider)

        self.lbl_time = Label(text="00:00 / 00:00", size_hint_x=None,
                              width=dp(96), font_size=dp(12), color=C_DIM,
                              halign="right", valign="middle", **self.F)
        self.lbl_time.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        prow.add_widget(self.lbl_time)
        box.add_widget(prow)

        # ---- 下载进度 ----
        self.pb = ProgressBar(max=100, size_hint_y=None, height=dp(4))
        box.add_widget(self.pb)
        # 名字必须是 self.status —— set_status() 写的就是它
        self.status = Label(text="正在启动…", size_hint_y=None, height=dp(34),
                            font_size=dp(12), color=C_DIM,
                            halign="left", valign="middle", **self.F)
        self.status.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(self.status)
        return outer

    # ---------- 线程工具 ----------
    def ui(self, fn):
        """把 fn 排到主线程执行（后台线程改界面的唯一入口）。

        Clock 回调会收到 dt 参数，这里统一吞掉，
        这样传进来的 fn 可以是无参的 lambda。
        """
        Clock.schedule_once(self._guard(lambda *_: fn()), 0)

    def bg(self, fn, name="worker"):
        def _w():
            try:
                fn()
            except Exception:
                log_exc(name)
                self.ui(lambda: self.set_status("出错了，详见 crash.log", C_ERR))
        threading.Thread(target=_w, name=name, daemon=True).start()

    def set_status(self, text, color=None):
        text = str(text)

        def _set(*_):
            self.status.text = text
            if color:
                self.status.color = color

        if threading.current_thread() is threading.main_thread():
            _set()
        else:
            self.ui(_set)

    # ---------- 启动 ----------
    def _boot(self, *_):
        diag("=== 启动 === dir=%s" % appenv.APP_DIR)
        try:
            self.builtin = extract_bundled_sources()
        except Exception:
            log_exc("释放内置音源")
            self.builtin = []
        # 下拉显示友好名，内部记「显示名 -> 路径」
        self.source_paths = {}
        labels = []
        for fname, path in self.builtin:
            title = source_title(fname)
            labels.append(title)
            self.source_paths[title] = path
        if labels:
            self.sp_source.values = labels
            self.sp_source.text = labels[0]
            diag("内置音源 %d 个: %s" % (len(labels), labels[:3]))
        else:
            self.sp_source.text = "无内置音源"
        try:
            self._fill_dir_presets()
        except Exception:
            log_exc("_fill_dir_presets")
        try:
            self._refresh_proxy_label()
        except Exception:
            log_exc("_refresh_proxy_label")
        self.set_status("正在启动 JS 引擎…")
        if IS_ANDROID:
            self.bridge.start(on_ready=self._on_engine_ready)
        else:
            # 桌面调试：没有 WebView；用假引擎跑界面（见 test_ui.py）
            self.set_status("桌面模式：仅界面可用", C_DIM)

    def _on_engine_ready(self, ok, detail=""):
        """由 WebView 流程在后台线程回调"""
        if not ok:
            msg = "WebView 引擎不可用"
            if detail:
                msg += ": %s" % detail
            msg += "\n（详细日志: Android/data/com.lxdl.lxdownloader/files/diag.log）"
            self.set_status(msg, C_ERR)
            log("引擎不可用:", detail)
            return
        self.ui(lambda: self.set_status("引擎就绪，加载音源…"))
        self.load_source(self._selected_source_path())

    def _selected_source_path(self):
        """当前下拉里选中的内置音源路径"""
        name = (self.sp_source.text or "").strip()
        path = getattr(self, "source_paths", {}).get(name)
        if path and os.path.exists(path):
            return path
        try:
            p = default_source_path()
            if os.path.exists(p):
                return p
        except Exception:
            log_exc("default_source_path")
        return SOURCE_FILE

    def _on_source_picked(self, spinner, text):
        """用户在内置音源下拉里换了源"""
        try:
            if not getattr(self, "source_paths", None):
                return
            path = self.source_paths.get((text or "").strip())
            if not path or not os.path.exists(path):
                return
            if getattr(self, "_loading_source", None) == path:
                return
            self._loading_source = path
            self.set_status("切换音源: %s …" % os.path.basename(path))
            self.load_source(path)
        except Exception:
            log_exc("_on_source_picked")

    def load_source(self, path):
        """加载音源（后台线程）。LxBridge 的 JS 调用不能在主线程做。"""
        self._loading_source = path      # 供状态栏显示是哪个音源

        def _work():
            self.ui(lambda: self.set_status("正在读取音源…"))
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    code = f.read()
            except Exception as e:
                # 注意：先把消息取出来再进 lambda。
                # `except ... as e` 的 e 在 except 块结束时会被删除，
                # 而 lambda 是稍后由 Clock 执行的，那时访问 e 会 NameError。
                msg = str(e)
                self.ui(lambda: self.set_status("读取音源失败: %s" % msg, C_ERR))
                return

            ok, info = self.bridge.load_source(code)
            if not ok:
                self.ui(lambda: self.set_status("音源加载失败: %s" % info, C_ERR))
                return
            self.ui(lambda: self._apply_source_info(info))

        self.bg(_work, "load-source")

    def _apply_source_info(self, info):
        self.platforms = []
        labels = []
        for key, v in (info.get("sources") or {}).items():
            v = v or {}
            self.platforms.append({
                "source": key,
                "name": v.get("name") or key,
                "qualitys": v.get("qualitys") or [],
            })
            labels.append("%s (%s)" % (PLATFORM_LABEL.get(key, key), key))

        # 注意：sp_source 是「音源文件」下拉，不要覆盖成平台列表
        self.sp_platform.values = labels
        if labels:
            self.sp_platform.text = labels[0]
        self._refresh_qualities()

        meta = info.get("meta") or {}
        # 有些音源 meta 里没有 name/version，用文件名兜底，别显示 "?"
        src_name = source_title(
            os.path.basename(getattr(self, "_loading_source", "") or ""))
        ctx = {
            "name": meta.get("name") or src_name or "音源",
            "version": meta.get("version") or "",
        }
        ver = (" v%s" % ctx["version"]) if ctx["version"] else ""
        self.lbl_sub.text = "%d 个平台" % len(labels)
        self.set_status("音源: %s%s · %d 个平台"
                        % (ctx["name"], ver, len(labels)), C_OK)
        if self.hint.text.startswith("搜索后"):
            self.hint.text = "搜索后点结果即可下载"

    # ---------- 设置 ----------
    def open_settings(self, *_):
        """从底部滑出设置面板，同时背景逐渐变暗。

        为什么用高度动画：面板挂在竖排 BoxLayout 里，Kivy 的布局每一帧都会
        按 size_hint/pos_hint 重新摆放子控件，动画位置会被覆盖回去 ——
        height 是唯一不会被抢走的自由度。
        """
        try:
            if getattr(self, "_sheet_open", False):
                return
            self._sheet_open = True
            self._fill_dir_presets()
            self._refresh_proxy_label()
            Animation(height=self._sheet_h, d=0.34,
                      t="out_cubic").start(self.sheet)
            Animation(_scrim_a=0.55, d=0.34).start(self)
            # 保险丝：动画没跑起来也要落到最终状态，否则面板卡在半开。
            Clock.schedule_once(
                lambda *_: setattr(self.sheet, "height", self._sheet_h), 0.7)
        except Exception:
            log_exc("open_settings")

    def close_settings(self, *_):
        try:
            if not getattr(self, "_sheet_open", False):
                return
            self._sheet_open = False
            Animation(height=0, d=0.26, t="out_quad").start(self.sheet)
            Animation(_scrim_a=0.0, d=0.26).start(self)
            Clock.schedule_once(
                lambda *_: setattr(self.sheet, "height", 0), 0.6)
        except Exception:
            log_exc("close_settings")

    # ---------- 下载目录 ----------
    def _fill_dir_presets(self):
        """填「保存位置」下拉：预设目录 + 当前生效项"""
        sp = getattr(self, "sp_dir", None)
        if sp is None:
            return                      # 设置弹窗还没打开过
        try:
            items = preset_dirs()
        except Exception:
            log_exc("preset_dirs")
            items = []
        self._dir_paths = {label: path for label, path in items}
        labels = [label for label, _ in items]
        cur = download_dir()
        hit = None
        for label, path in items:
            if os.path.normpath(path) == os.path.normpath(cur):
                hit = label
                break
        if hit is None:
            # 用户自己选的目录不在预设里 —— 补一条显示出来，
            # 否则下拉会显示成另一个目录，等于骗用户
            labels.append("自定义")
            self._dir_paths["自定义"] = cur
            hit = "自定义"
        self.sp_dir.values = labels
        self.sp_dir.text = hit
        diag("下载目录 = %s（预设 %d 个）" % (cur, len(items)))

    def _on_dir_preset(self, spinner, text):
        path = getattr(self, "_dir_paths", {}).get((text or "").strip())
        if not path:
            return
        if os.path.normpath(path) == os.path.normpath(download_dir()):
            return
        set_download_dir(path)
        self.set_status("下载目录改为：%s" % path, C_OK)

    def pick_dir(self, *_):
        """系统目录选择器（任意目录）。

        本 App 已经拿了「所有文件访问」，所以选完直接用真实路径写文件即可，
        不必走 SAF 的 ContentResolver 那一套。
        """
        if not IS_ANDROID:
            self.set_status("桌面端请直接用预设目录")
            return
        try:
            from jnius import autoclass
            from android import activity as android_activity

            Intent = autoclass("android.content.Intent")
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            if not getattr(self, "_picker_bound", False):
                android_activity.bind(on_activity_result=self._on_picked)
                self._picker_bound = True
            intent = Intent("android.intent.action.OPEN_DOCUMENT_TREE")
            intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                            | Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            act.startActivityForResult(intent, 0x1235)
        except Exception as e:
            log_exc("pick_dir")
            self.set_status("打开目录选择器失败: %s" % e, C_ERR)

    def _on_dir_picked(self, result_code, intent):
        try:
            if result_code != -1 or intent is None or intent.getData() is None:
                self.set_status("已取消选择目录")
                return
            path = tree_uri_to_path(intent.getData())
            if not path:
                self.set_status(
                    "这个目录用不了（目前只支持手机内置存储），换一个试试",
                    C_ERR)
                return
            set_download_dir(path)
            self._fill_dir_presets()
            self.set_status("下载目录改为：%s" % path, C_OK)
        except Exception as e:
            log_exc("_on_dir_picked")
            self.set_status("设置目录失败: %s" % e, C_ERR)

    def pick_proxies(self, *_):
        """选一个 QQ 代理列表 .txt（一行一条，含 {id}）

        这样第三方代理失效时用户能自己换 —— 不用等重新编译发版。
        """
        self._open_file_picker(0x1236)

    def _on_proxies_picked(self, result_code, intent):
        try:
            if result_code != -1 or intent is None or intent.getData() is None:
                self.set_status("已取消选择代理文件")
                return
            text = self._read_uri(intent.getData())
            n = qqresolve.save_proxies(text)
            self._refresh_proxy_label()
            self.set_status("已保存 %d 条 QQ 代理" % n, C_OK)
        except Exception as e:
            log_exc("_on_proxies_picked")
            self.set_status("代理文件不可用: %s" % e, C_ERR)

    def _refresh_proxy_label(self):
        try:
            btn = getattr(self, "btn_proxy", None)
            if btn is None:
                return                  # 设置弹窗还没打开过
            n_builtin = len(qqresolve.QQ_PROXIES)
            has_custom = os.path.exists(qqresolve.QQ_PROXY_FILE)
            extra = "，已加自定义" if has_custom else ""
            btn.text = "选代理文件(.txt)  ·  内置 %d 条%s" % (n_builtin, extra)
        except Exception:
            log_exc("_refresh_proxy_label")

    def _open_file_picker(self, code):
        """打开系统文件选择器（音源 .js / 代理 .txt 共用）

        两个不能踩的坑：

        1) 只用 p4a 自带的 activity 回调机制：它内部已经注册好了 Java 侧的
           listener，这里传一个普通 Python 函数即可。
           千万不要自己写 PythonJavaClass + __javainterfaces__ =
           ["org/kivy/android/activity/ActivityResultListener"] ——
           那个类名在 APK 的 dex 里根本不存在，会抛
             ClassNotFoundException: Didn't find class
             "org.kivy.android.activity.ActivityResultListener"

        2) createChooser 的第二个参数签名是 CharSequence，
           直接传 Python str 会抛
             No static methods called createChooser ...
             requested: (Intent, 'str')
           必须包成 java.lang.String 再 cast 成 CharSequence。
        """
        if not IS_ANDROID:
            self.set_status("桌面端请直接替换文件")
            return
        try:
            from jnius import autoclass
            from android import activity as android_activity

            Intent = autoclass("android.content.Intent")
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            if not getattr(self, "_picker_bound", False):
                android_activity.bind(on_activity_result=self._on_picked)
                self._picker_bound = True

            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("*/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            launcher = intent
            try:
                from jnius import cast
                JString = autoclass("java.lang.String")
                title = cast("java.lang.CharSequence", JString("选择文件"))
                launcher = Intent.createChooser(intent, title)
            except Exception:
                log_exc("createChooser")
            act.startActivityForResult(launcher, code)
        except Exception as e:
            log_exc("_open_file_picker")
            self.set_status("打开文件选择器失败: %s" % e, C_ERR)

    # ---------- 换音源 ----------
    def pick_source(self, *_):
        """从手机里选一个新的音源 .js 文件

        具体实现统一在 _open_file_picker（p4a activity 回调、
        createChooser 的那两个坑都记在那里了），这里只管语义。
        """
        self._open_file_picker(0x1234)

    def _on_picked(self, request_code, result_code, intent):
        """选完文件/目录回调（p4a 的 activity 在 UI 线程派发）"""
        try:
            if request_code == 0x1235:
                return self._on_dir_picked(result_code, intent)
            if request_code == 0x1236:
                return self._on_proxies_picked(result_code, intent)
            if request_code != 0x1234:
                return
            if result_code != -1 or intent is None:
                self.set_status("已取消选择")
                return
            uri = intent.getData()
            if uri is None:
                self.set_status("没有拿到文件")
                return
            text = self._read_uri(uri)
            if not text or len(text) < 200:
                self.set_status("文件内容过短，可能不是音源", C_ERR)
                return
            save_source(text)
            self.set_status("音源已保存，正在加载…")
            self.load_source(SOURCE_FILE)
        except Exception as e:
            log_exc("_on_picked")
            msg = str(e)
            self.set_status("读取音源失败: %s" % msg, C_ERR)

    def _read_uri(self, uri):
        """Android 文件选择器返回的是 content:// URI，要用 ContentResolver 读"""
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        stream = act.getContentResolver().openInputStream(uri)
        reader = autoclass("java.io.BufferedReader")(
            autoclass("java.io.InputStreamReader")(stream))
        sb = autoclass("java.lang.StringBuilder")()
        line = reader.readLine()
        while line is not None:
            sb.append(line).append("\n")
            line = reader.readLine()
        reader.close()
        return str(sb.toString())

    # ---------- 搜索 ----------
    def _focus_search(self):
        """把搜索框重新聚焦。

        Android 上用户收起软键盘后 Kivy 的 focus 仍是 True，
        直接再点不会弹键盘 —— 必须显式「失焦 → 延时聚焦」。
        """
        try:
            ti = self.ti_search
            ti.focus = False
            Clock.schedule_once(lambda *_: setattr(ti, "focus", True), 0.05)
        except Exception:
            log_exc("_focus_search")

    def do_search(self, *_):
        try:
            keyword = self.ti_search.text.strip()
            if not keyword:
                # 空输入时把键盘重新拉起来。用户反馈「关了键盘后点搜索
                # 没反应」多半就卡在这：只报一句「请先输入歌名」，
                # 键盘却不回来，看着像按钮坏了。
                self._focus_search()
                self.set_status("请先输入歌名")
                return
            if self.busy:
                self.set_status("正在处理中，请稍候…")
                return
            self.busy = True
            self.set_status("搜索中: %s" % keyword)
            self._clear_results()
            want = COUNT_VALUE.get(self.sp_count.text, 100)
            plat = self._current_source()
            self.bg(lambda: self._search_work(keyword, want, plat), "search")
        except Exception as e:
            self.busy = False
            log_exc("do_search")
            self.set_status("搜索出错: %s" % e, C_ERR)

    def _search_work(self, keyword, want, platform):
        songs, err = [], None
        try:
            def prog(got, total):
                self.ui(lambda: self.set_status(
                    "在 %s 搜索: %s… 已获取 %d%s"
                    % (PLATFORM_LABEL.get(platform, platform), keyword, got,
                       ("/%d" % total) if total else "")))
            songs = searchers.search(platform, keyword, want, on_progress=prog)
            if not songs:
                err = "该平台没有找到结果"
        except KeyError:
            err = "该平台暂不支持搜索（可手动输入歌曲 ID）"
        except Exception as e:
            err = str(e)
            log_exc("search")
        self.ui(lambda: self._after_search(songs, err, platform))

    def _after_search(self, songs, err, platform="wy"):
        self.busy = False
        if err:
            self.set_status("搜索失败: %s" % err, C_ERR)
            return
        self._show_results(songs)

    def _clear_results(self):
        self.results.clear_widgets()

    def _show_results(self, songs):
        self.songs = list(songs or [])
        self._clear_results()
        if not self.songs:
            self.hint.text = "没有找到结果，换个关键词试试"
            self.set_status("没有找到结果")
            return

        self.hint.text = "点歌名直接播放，点右侧「下载」保存（共 %d 首）" % len(self.songs)
        # 只给前几首做入场动效：几百首全做会明显卡，而且看不到那么远
        STAGGER = 10
        for i, s in enumerate(self.songs):
            row = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(6))
            # 歌名这块本身就是播放键 —— 点一下直接放，不再弹详情框
            song_btn = FlatButton(
                text="%d. %s\n%s    %s"
                     % (i + 1, s["name"], s["singer"],
                        s.get("interval") or "--:--"),
                halign="left", valign="middle", font_size=dp(13),
                color=C_TEXT, bg_color=C_ITEM, radius=10, **self.F)
            song_btn.bind(size=lambda b, v: setattr(b, "text_size",
                                                    (v[0] - dp(20), None)))
            song_btn.bind(on_release=lambda b, idx=i: self.play_song(idx))
            row.add_widget(song_btn)

            dl = FlatButton(text="下载", font_size=dp(13), color=C_TEXT,
                            bg_color=C_CTRL, radius=10, size_hint_x=None,
                            width=dp(58), **self.F)
            dl.bind(on_release=lambda b, idx=i: self.download_song(idx))
            row.add_widget(dl)
            self.results.add_widget(row)

            if i < STAGGER:
                # 错峰进场：淡入 + 高度展开，列表像「长」出来而不是一次砸下来
                row.opacity = 0.0
                row.height = 0
                anim = Animation(opacity=1.0, height=dp(58), duration=0.22,
                                 t="out_quad")
                Clock.schedule_once(
                    lambda *_, r=row, a=anim: a.start(r), 0.03 * i)

        # 保险丝：万一入场动画没跑起来，列表会停在全透明/零高度，
        # 那比没有动效糟得多 —— 到点无条件把最终状态写回去。
        if self.songs:
            def _settle(*_):
                try:
                    for w in self.results.children:
                        w.opacity = 1.0
                        if w.height < dp(58):
                            w.height = dp(58)
                except Exception:
                    log_exc("列表入场收尾")
            Clock.schedule_once(_settle, 0.03 * min(len(self.songs), STAGGER) + 0.6)

        self.set_status("找到 %d 首：点歌名播放，点「下载」保存" % len(self.songs))

    # ---------- 列表上的直接操作 ----------
    def play_song(self, idx):
        """点歌名：解析直链后直接播放"""
        self._act_on_song(idx, "play")

    def download_song(self, idx):
        """点下载：解析直链后直接下载"""
        self._act_on_song(idx, "download")

    def _act_on_song(self, idx, action):
        try:
            if idx >= len(self.songs):
                return
            if self.busy:
                self.set_status("正在处理中，请稍候…")
                return
            song = self.songs[idx]
            self.busy = True
            self._cur_song = song
            self._cur_url = None
            self._pre_meta = None
            self._pending_action = action
            self.set_status(("正在准备播放: " if action == "play"
                             else "正在准备下载: ") + song["name"])
            self.bg(lambda: self._resolve_song(song), "resolve")
        except Exception as e:
            self.busy = False
            log_exc("_act_on_song")
            self.set_status("操作失败: %s" % e, C_ERR)

    # ---------- 品质 / 格式 ----------
    def _current_source(self):
        """从「平台」下拉里取出平台代号"""
        text = self.sp_platform.text or ""
        for p in self.platforms:
            if "(%s)" % p["source"] in text:
                return p["source"]
        return "wy"

    def _platform_qualities(self):
        """当前平台声明的可用品质"""
        src = self._current_source()
        for p in self.platforms:
            if p["source"] == src:
                return list(p.get("qualitys") or QUALITY_ORDER)
        return list(QUALITY_ORDER)

    def _refresh_qualities(self):
        """按平台 + 格式偏好刷新品质下拉。

        下拉里显示**中文标签**（用户看不懂 flac/hires 这些代号），
        内部一律用代号，靠 _quality_code() 换回去。
        """
        try:
            quals = songinfo.filter_qualities(self._platform_qualities(),
                                              self.sp_format.text)
            self._qual_labels = {songinfo.quality_label(q): q for q in quals}
            self.sp_quality.values = list(self._qual_labels.keys())
            code = self._quality_code()
            if code not in quals and quals:
                code = quals[0]
            self.sp_quality.text = songinfo.quality_label(code)
        except Exception:
            log_exc("_refresh_qualities")

    def _quality_code(self):
        """把下拉里的中文标签换回品质代号（换不回来就退回高音质）"""
        text = (self.sp_quality.text or "").strip()
        code = getattr(self, "_qual_labels", {}).get(text)
        if code:
            return code
        if text in songinfo.QUALITY_LABEL:      # 兼容直接给代号的情况
            return text
        return "320k"

    # ---------- 歌曲详情 ----------
    def open_song(self, idx):
        """点搜索结果：解析直链 + 探测元信息，然后弹详情"""
        try:
            if idx >= len(self.songs):
                return
            song = self.songs[idx]
            self._cur_song = song
            self._cur_url = None
            self._show_song_popup(song)
            self.set_status("正在解析: %s …" % song["name"])
            self.bg(lambda: self._resolve_song(song), "resolve")
        except Exception as e:
            log_exc("open_song")
            self.set_status("打开歌曲失败: %s" % e, C_ERR)

    def _try_one(self, source, song, qualities):
        """在某个平台上按音质依次尝试，返回 (url, 音质, extra, 最后一次错误)

        关键：解析出地址不算成功，还要 verify() 确认真能取到音频。
        很多第三方接口挂了会返回 403，只看到「解析成功」会误判，
        最后在播放/下载时才莫名其妙地失败。
        """
        info = {"id": song["id"], "name": song["name"],
                "singer": song["singer"], "source": source,
                "interval": song.get("interval", ""),
                "meta": {"songId": song["id"],
                         "albumName": song.get("album", "")}}
        for k, v in (song.get("extra") or {}).items():
            if v:
                info[k] = v
                info["meta"].setdefault(k, v)

        last = ""
        for q in qualities:
            got, err = self.bridge.music_url(source, q, info)
            if not got:
                last = err or "未返回地址"
                continue
            if downloader.is_restricted(got):
                last = "该音质直链受限"
                continue

            ok, why = songinfo.verify(got, source)
            log("平台 %s 音质 %s: %s" % (source, q, "可用" if ok else why))
            if ok:
                return got, q, info, ""
            last = why

        return None, None, info, last or "所有音质都不可用"

    def _resolve_song(self, song):
        """后台：解析直链（本平台内依次尝试各音质，不换平台）"""
        want = self._quality_code()
        order = [want] + [q for q in ("320k", "flac", "128k") if q != want]
        order = order[:3]

        source = self._current_source()
        self.ui(lambda: self.set_status(
            "正在解析 (%s)…" % PLATFORM_LABEL.get(source, source)))

        url, used, info, err = None, None, None, ""

        # QQ 优先走内置解析器：QQ 官方接口要 VIP，
        # 而各音源自带的第三方代理有的已失效（实测聚合音源那条 403）。
        # 内置这条路实测稳定（standard=128k / exhigh=320k / lossless=FLAC）。
        # 注意：这一步仍然是 **QQ 平台**，不会偷偷换成别的平台。
        if source == "tx":
            mid = (song.get("extra") or {}).get("songmid") or song.get("id")
            self.ui(lambda: self.set_status("正在解析 QQ 直链…"))
            u, lv, qmeta = qqresolve.resolve(mid, order[0])
            if u:
                url, used = u, order[0]
                self._pre_meta = qmeta      # 已经探测过，省一次请求
                log("QQ 内置解析成功:", lv)
            else:
                err = lv
                log("QQ 内置解析失败，改试音源自带接口:", lv)

        # 再试音源自带的接口
        if not url:
            url, used, info, err2 = self._try_one(source, song, order)
            err = err2 or err

        if url:
            meta = getattr(self, "_pre_meta", None) or songinfo.probe(url)
            self._pre_meta = None
            self.ui(lambda: self._song_ready(song, url, used, meta, source))
            return

        # 注意：这里**不再**自动换成别的平台。
        # 用户选了 QQ 音乐就是要 QQ 音乐 —— 偷偷换成网易云/酷狗
        # 等于给了一首「来源不对」的歌，比直接说失败更糟。
        # 失败就如实报错，是否换平台由用户自己决定（见 _song_failed）。
        self.ui(lambda: self._song_failed(err, source))

        self.ui(lambda: self._song_failed(err))

    def _song_failed(self, err, platform=None):
        name = PLATFORM_LABEL.get(platform or "", platform or "")
        self.busy = False
        self._pending_action = None
        # 失败时**才**弹详情框 —— 那里有「改用 XX 平台」的入口。
        # 成功路径不弹：点歌名就是直接播放，点「下载」就是直接下载。
        #
        # 注意判据是「是否真的在显示」，不是「对象存不存在」：
        # _popup 弹过一次再关掉之后仍然不是 None（只是没挂在窗口上），
        # 只看 None 会把错误写进一个已经关掉的框里，用户什么都看不到。
        if getattr(self._popup, "parent", None) is None:
            try:
                self._show_song_popup(self._cur_song
                                      or {"name": "未知歌曲", "singer": ""})
            except Exception:
                log_exc("_show_song_popup(失败兜底)")
        if getattr(self, "_pop_info", None) is not None:
            self._pop_info.text = ("%s 取不到直链\n原因: %s\n\n"
                                   "（不会自动换成别的平台 —— 你可以自己选）"
                                   % (name or "该平台", err))
        self.set_status("%s 取不到直链: %s" % (name or "该平台", err), C_ERR)
        # 按钮改成「换个平台试试」，但必须用户点了才换
        others = [p for p in ("wy", "kg", "kw", "mg", "tx")
                  if p != platform and p in {x["source"] for x in self.platforms}]
        if others and getattr(self, "_pop_dl", None) is not None:
            nxt = others[0]
            self._pop_dl.text = "改用%s" % PLATFORM_LABEL.get(nxt, nxt)
            self._pop_dl.disabled = False
            self._alt_platform = nxt
            self._pop_dl.bind(on_release=lambda *_: self._switch_platform())

    def _song_ready(self, song, url, quality, meta, platform=None, note=""):
        self._cur_url = url
        self._cur_ext = meta.get("format") or songinfo.format_of(quality)
        self._cur_dur = 0.0
        try:
            parts = song.get("interval", "").split(":")
            if len(parts) == 2:
                self._cur_dur = int(parts[0]) * 60 + int(parts[1])
        except Exception:
            pass

        src = PLATFORM_LABEL.get(platform or "", "") if platform else ""
        if getattr(self, "_pop_info", None) is not None:
            self._pop_info.text = (
                "歌手: %s\n时长: %s\n品质: %s\n格式: %s\n大小: %s%s"
                % (song["singer"], song.get("interval") or "--:--",
                   songinfo.quality_label(quality),
                   (self._cur_ext or "?").upper(),
                   songinfo.human_size(meta.get("size")),
                   ("\n来源: %s %s" % (src, note)).rstrip() if src else ""))
        if getattr(self, "_pop_play", None) is not None:
            self._pop_play.disabled = False
            self._pop_dl.disabled = False
        self.set_status("已解析: %s（%s%s / %s / %s）"
                        % (song["name"],
                           ("%s " % src) if src else "",
                           songinfo.quality_label(quality),
                           (self._cur_ext or "?").upper(),
                           songinfo.human_size(meta.get("size"))), C_OK)

        # 列表上的直接动作（点歌名播放 / 点下载）在这里接着走完。
        # 走详情弹窗那条路时不带 _pending_action，所以不会重复触发。
        act = getattr(self, "_pending_action", None)
        self._pending_action = None
        self.busy = False
        if act == "play":
            self.play_current()
        elif act == "download":
            self.download_current()

    def _switch_platform(self):
        """用户主动点了「改用 XX」才换平台"""
        try:
            alt = getattr(self, "_alt_platform", None)
            if not alt:
                return
            if getattr(self, "_popup", None):
                self._popup.dismiss()
            song = self._cur_song
            if not song:
                return
            # 按新平台重新搜一次，拿该平台的 id/extra
            self.set_status("改用 %s 搜索同一首…"
                            % PLATFORM_LABEL.get(alt, alt))
            self.bg(lambda: self._research_on(alt, song), "alt-search")
        except Exception:
            log_exc("_switch_platform")

    def _research_on(self, platform, song):
        try:
            key = "%s %s" % (song["name"], song["singer"])
            cands = searchers.search(platform, key, 5)
            best = None
            for c in cands:
                if (song["name"][:6] in c["name"]
                        or c["name"][:6] in song["name"]):
                    best = c
                    break
            if best is None:
                self.ui(lambda: self.set_status(
                    "%s 上没找到这首歌" % PLATFORM_LABEL.get(platform, platform),
                    C_ERR))
                return
            self.ui(lambda: (setattr(self, "_cur_song", best),
                             self.set_status("已切到 %s: %s"
                                             % (PLATFORM_LABEL.get(platform, platform),
                                                best["name"]))))
            # 切到对应平台后再解析
            self.ui(lambda: self._set_platform_and_load(platform, best))
        except Exception as e:
            log_exc("_research_on")
            msg = str(e)      # 同上：别在 lambda 里直接用 e
            self.ui(lambda: self.set_status("换平台失败: %s" % msg, C_ERR))

    def _set_platform_and_load(self, platform, song):
        for p in self.platforms:
            if p["source"] == platform:
                self.sp_platform.text = "%s (%s)" % (
                    PLATFORM_LABEL.get(platform, platform), platform)
                break
        self._show_song_popup(song)
        self.bg(lambda: self._resolve_song(song), "resolve")

    def _show_song_popup(self, song):
        kw = dict(self.F)
        content = BoxLayout(orientation="vertical", spacing=dp(8),
                            padding=dp(12))

        name = Label(text="%s — %s" % (song["name"], song["singer"]),
                     size_hint_y=None, height=dp(44), font_size=dp(15),
                     bold=True, color=C_TEXT, halign="left", valign="middle",
                     **kw)
        name.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        content.add_widget(name)

        self._pop_info = Label(text="解析中…", font_size=dp(13), color=C_DIM,
                               halign="left", valign="top", **kw)
        self._pop_info.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        content.add_widget(self._pop_info)

        btns = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self._pop_play = FlatButton(text="播放", font_size=dp(15),
                                    bg_color=C_ACCENT, color=(1, 1, 1, 1),
                                    radius=10, disabled=True, **kw)
        self._pop_play.bind(on_release=lambda *_: self.play_current())
        btns.add_widget(self._pop_play)

        self._pop_dl = FlatButton(text="下载", font_size=dp(15),
                                  bg_color=C_CTRL, color=C_TEXT,
                                  radius=10, disabled=True, **kw)
        self._pop_dl.bind(on_release=lambda *_: self.download_current())
        btns.add_widget(self._pop_dl)
        content.add_widget(btns)

        self._popup = Popup(title="歌曲信息", content=content,
                            size_hint=(0.92, None), height=dp(310),
                            title_size=dp(15), separator_color=C_ACCENT,
                            **self.P)
        # 淡入。刻意从 0.86 而不是 0 起 —— 万一动画没跑起来，
        # 弹窗至少是「几乎全可见」，不会变成一个看不见却挡住点击的遮罩。
        self._popup.opacity = 0.86
        self._popup.open()
        Animation(opacity=1.0, duration=0.18, t="out_quad").start(self._popup)

    # ---------- 播放 ----------
    def play_current(self):
        if not getattr(self, "_cur_url", None):
            self.set_status("还没有解析出可播放的直链")
            return
        if self._popup:
            self._popup.dismiss()
        self.player.play(self._cur_url, on_event=self._on_player_event)
        self.btn_play.text = "暂停"
        self._bg_playing(True)

    def _bg_playing(self, on):
        """把播放状态同步给动态背景（没建背景时静默跳过）"""
        fx = getattr(self, "bgfx", None)
        if fx is None:
            return
        try:
            fx.set_playing(on)
        except Exception:
            log_exc("bgfx.set_playing")

    def _on_player_event(self, kind, payload):
        """播放器回调（后台线程）"""
        if kind == "buffering":
            self.ui(lambda: self._player_buffering(payload))
        elif kind == "ready":
            self.ui(lambda: self._player_ready(payload))
        elif kind == "error":
            self.ui(lambda: self._player_error(payload))

    def _player_buffering(self, pct):
        self.pb.value = pct * 100
        self.set_status("缓冲中… %.0f%%" % (pct * 100))

    def _player_ready(self, duration):
        self.pb.value = 0
        self._cur_duration = float(duration or 0) or float(self._cur_dur or 0)
        self.btn_play.text = "暂停"
        self.set_status("正在播放: %s" % (self._cur_song or {}).get("name", ""),
                        C_OK)

    def _player_error(self, msg):
        self.btn_play.text = "播放"
        self._bg_playing(False)
        self.set_status("播放失败: %s" % msg, C_ERR)

    def toggle_play(self):
        try:
            if not self.player.is_active():
                self.play_current()
                return
            self.player.toggle()
            playing = self.player.state == player_mod.Player.PLAYING
            self.btn_play.text = "暂停" if playing else "播放"
            self._bg_playing(playing)
        except Exception as e:
            log_exc("toggle_play")
            self.set_status("播放控制失败: %s" % e, C_ERR)

    def _seek_down(self, slider, touch):
        if slider.collide_point(*touch.pos):
            self._dragging = True
        return False

    def _seek_up(self, slider, touch):
        if self._dragging:
            self._dragging = False
            total = self._cur_duration or self.player.duration()
            if total > 0:
                self.player.seek(slider.value / 1000.0 * total)
        return False

    def _tick(self, dt):
        """定时刷新播放进度（0.5s 一次）"""
        try:
            if not self.player.is_active():
                return
            pos = self.player.position()
            total = self._cur_duration or self.player.duration()
            if not self._dragging and total > 0:
                self.slider.value = max(0, min(1000, pos / total * 1000))
            self.lbl_time.text = "%s / %s" % (fmt_time(pos), fmt_time(total))
            if (self.player.state == player_mod.Player.PLAYING
                    and total > 0 and pos >= total - 0.7):
                self.player.stop()
                self._player_finished()
        except Exception:
            log_exc("_tick")

    def _player_finished(self):
        self.btn_play.text = "播放"
        self._bg_playing(False)
        self.slider.value = 0
        self.lbl_time.text = "00:00 / 00:00"
        self.set_status("播放结束")

    # ---------- 下载 ----------
    def download_current(self):
        if not getattr(self, "_cur_url", None):
            self.set_status("还没有解析出可下载的直链")
            return
        if self._popup:
            self._popup.dismiss()
        song = self._cur_song or {}
        url = self._cur_url
        ext = self._cur_ext or "mp3"
        self.pb.value = 0
        self.set_status("准备下载: %s" % song.get("name", ""))
        plat = (self._cur_song or {}).get("platform") or self._current_source()
        self.bg(lambda: self._download_url(song, url, ext, plat), "download")

    def _download_url(self, song, url, ext, platform=None):
        try:
            out_dir = download_dir()
            # 往公共目录（Download/…）写需要「所有文件访问」；Android 11+
            # 不授权就只能写 App 私有目录。原判断是「不打算用公共目录才提示」，
            # 恰好写反了：真正要写公共目录时反而从不提示，用户一直没授权，
            # 下载就卡在最后一步（真机实测 EPERM，进度条到 100% 然后作废）。
            if IS_ANDROID and not self._asked_permission \
                    and not appenv.has_all_files_access():
                self._asked_permission = True
                self.ui(lambda: self.set_status(
                    "提示：授权「所有文件访问」后文件会存到 "
                    "Download/落雪音源；不授权也能下，只是存到 App 私有目录"))
                request_all_files_access()
            os.makedirs(out_dir, exist_ok=True)

            name = downloader.safe_name("%s - %s" % (song.get("name", "unknown"),
                                                     song.get("singer", "")))
            # 同名文件自动改名，不覆盖 —— Android 上覆盖会 EPERM
            dest = downloader.unique_path(
                os.path.join(out_dir, "%s.%s" % (name, ext)))

            def on_progress(got, total):
                self.ui(lambda: self._progress(got, total, song.get("name", "")))

            size = downloader.download(url, dest, on_progress=on_progress,
                                       platform=platform)
            self.ui(lambda: self._download_done(dest, size))
        except Exception as e:
            msg = str(e)
            log_exc("download")
            self.ui(lambda: self.set_status("下载失败: %s" % msg, C_ERR))

    def _progress(self, got, total, name):
        self.pb.value = got * 100.0 / max(total, 1)
        self.set_status("下载中 %s: %.1f%% (%.1f/%.1f MB)"
                        % (name, self.pb.value, got / 1048576,
                           total / 1048576))

    def _download_done(self, path, size):
        self.pb.value = 100
        self.set_status("完成: %s (%.1f MB)\n保存于 %s"
                        % (os.path.basename(path), size / 1048576,
                           os.path.dirname(path)), C_OK)


if __name__ == "__main__":
    appenv.install_crash_guard()
    fonts.register()
    try:
        LxApp().run()
    except Exception:
        log_exc("App.run")
        raise
