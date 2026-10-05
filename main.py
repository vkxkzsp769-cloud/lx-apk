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
import math
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
#  主题 —— iOS 浅色（system colors）
# ============================================================
# 设计令牌：全部取苹果 HIG / UIColor 系统色的浅色模式值。
# 页面底 = systemGroupedBackground；卡片/列表 = white；
# 输入胶囊 = secondarySystemFill；主色 = systemBlue #007AFF。
C_BG = (0.949, 0.949, 0.961, 1)       # #F2F2F6 分组页底色
C_CARD = (1, 1, 1, 1)                 # 卡片 / 分组列表
C_CTRL = (0.914, 0.914, 0.925, 1)     # 胶囊输入（secondarySystemFill）
C_ITEM = (1, 1, 1, 1)                 # 列表行（白）
C_SEP = (0.235, 0.235, 0.263, 0.16)   # 发丝分隔线（separator）
C_ACCENT = (0.0, 0.478, 1.0, 1)       # systemBlue #007AFF
C_ACCENT_D = (0.0, 0.40, 0.855, 1)    # 按下态
# 渐变两端（系统蓝的深浅两档，用于进度/高光；苹果几乎不用大幅渐变）
GRAD_A = (0.039, 0.518, 1.0)          # #0A84FF
GRAD_B = (0.0, 0.478, 1.0)            # #007AFF
C_TEXT = (0.110, 0.110, 0.118, 1)     # label #1C1C1E
C_DIM = (0.557, 0.557, 0.576, 1)      # secondaryLabel #8E8E93
C_FAINT = (0.682, 0.682, 0.698, 1)    # tertiaryLabel #AEAEB2
C_OK = (0.133, 0.529, 0.224, 1)       # 浅底上可读的绿
C_ERR = (1.0, 0.231, 0.188, 1)        # systemRed #FF3B30
C_WHITE = (1, 1, 1, 1)
SHADOW_C = (0.27, 0.30, 0.38)         # 卡片投影色（偏冷的灰）

# 圆角：卡片 20 / 控件 14 / 小元素 11；胶囊用 "pill"（高度的一半）
R_LG = 20
R_MD = 14
R_SM = 11

# 「毛玻璃」近似参数（浅色版）：白 + 高不透明度 + 发丝描边。
# Kivy 拿不到背后像素，做不了真 backdrop-blur，观感靠半透明白 + 分层。
GLASS_FILL = (1, 1, 1, 0.82)
GLASS_HI = (1, 1, 1, 0.35)

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


# ⚠ padding 语义（**实测** Kivy 2.3，文档写的 (t,r,b,l) 是错的）：
#   四元组 = (left, top, right, bottom)
#   二元组 = (horizontal, vertical)
# 探针：BoxLayout(size=300).padding=(10,20,30,40) → 子控件 x=10 y=40 w=260 h=240
# 即 左=10 上=20 右=30 下=40。按 (t,r,b,l) 写过一次，左距/右距全错位（用户：
# 「下载按钮还是对不齐，要整齐」）。别再改了，先跑那个探针再动。


def spring_t(p):
    """苹果式阻尼弹簧缓动：先冲过终点一点，再回落定住。

    UIKit 的 present/dismiss、列表进场都是这种「有生命的」曲线，
    线性或 ease-in-out 一看就不像 iOS。直接喂给 Kivy 的
    `Animation(..., t=spring_t)`：它按归一化时间取进度值。
    f(0)=0、f(1)≈0.999（收尾由各处「保险丝」补成精确值）。
    """
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    # 欠阻尼弹簧：冲过终点约 8~10% 再摆回来（衰减 6.2 / 频率 10.4，
    # 约一个半周期收敛；f(1)≈0.999 的收尾差由各处「保险丝」补成精确值）。
    return 1.0 - math.exp(-6.2 * p) * math.cos(10.4 * p)


# ============================================================
#  控件
# ============================================================
def attach_bg(widget, color, radius=0, pill=False):
    """给控件加背景（可选圆角；radius 可为 int 或 [上右,上左,下左,下右]）。

    Kivy 没有 canvas_before 这个属性（它是 widget.canvas.before 对象），
    当构造参数传会抛 TypeError 导致启动即崩 —— 必须建好后再加图元。
    pill=True 时圆角恒等于高度的一半（胶囊）。
    """
    from kivy.graphics import Color, Rectangle, RoundedRectangle
    with widget.canvas.before:
        c = Color(*color)
        if isinstance(radius, (list, tuple)):
            shape = RoundedRectangle(pos=widget.pos, size=widget.size,
                                     radius=[dp(r) for r in radius])
        elif radius == "pill" or pill:
            shape = RoundedRectangle(pos=widget.pos, size=widget.size,
                                     radius=[dp(8)])
        elif radius:
            shape = RoundedRectangle(pos=widget.pos, size=widget.size,
                                     radius=[dp(radius)])
        else:
            shape = Rectangle(pos=widget.pos, size=widget.size)

    def _sync(w, *_):
        shape.pos = w.pos
        shape.size = w.size
        if pill or radius == "pill":
            shape.radius = [max(w.height / 2.0, 1.0)]

    widget.bind(pos=_sync, size=_sync)
    # 存引用：按下高亮之类的交互要改颜色/形状时用（FlatButton 等自带一套）
    widget._att_bg_c = c
    widget._att_bg_shape = shape
    return widget


def attach_shadow(widget, spread=None, alpha=0.13, squash=0.55):
    """iOS 卡片投影：径向渐变贴图横向铺开、纵向压扁，垫在控件后面。

    浅色界面里阴影是分层的核心手段（深色版才用发光）。
    必须在 attach_bg **之前**调用 —— canvas.before 按插入顺序绘制，
    先画的投影会被后画的白色卡片盖住中心，只露出四周的柔和边。

    参数默认值在函数体内算 dp()：默认参数在**模块导入时**求值，
    而 dp() 依赖 Metrics（要窗口），窗口就绪前求值会导入即崩。
    """
    from kivy.graphics import Color, Rectangle
    if spread is None:
        spread = dp(14)
    tex = bgfx.make_glow_texture()
    with widget.canvas.before:
        c = Color(*SHADOW_C, alpha)
        rect = Rectangle(pos=widget.pos, size=widget.size, texture=tex)

    def _sync(w, *_):
        v = spread * squash
        rect.pos = (w.x - spread, w.y - v - dp(2))
        rect.size = (w.width + spread * 2, w.height + v * 2 + dp(4))

    widget.bind(pos=_sync, size=_sync)
    _sync(widget)
    Clock.schedule_once(lambda *_: _sync(widget), 0)  # 首帧布局后再校准
    widget._shadow_rect = rect
    widget._shadow_color = c
    return widget


def attach_sep(widget, inset=None, color=None):
    """iOS 列表发丝分隔线：底部 1dp 横线，左右各缩进 16dp。"""
    from kivy.graphics import Color, Rectangle
    if inset is None:
        inset = dp(16)
    col = color or (0.235, 0.235, 0.263, 0.13)
    with widget.canvas.after:
        c = Color(*col)
        r = Rectangle(pos=widget.pos, size=(1, 1))
    widget._sep_color = c    # 保持引用，防止被 GC

    def _sync(w, *_):
        if getattr(w, "_sep_hidden", False):
            r.size = (0, 0)              # 分组卡最后一行：发丝线让位给卡边缘
            return
        r.pos = (w.x + inset, w.y)
        r.size = (max(0.0, w.width - inset * 2), max(1.0, dp(0.7)))

    widget.bind(pos=_sync, size=_sync)
    _sync(widget)
    widget._sep_rect = r
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
    """生成两端渐变的贴图（供胶囊按钮 / 进度条用）"""
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
    """发丝描边。

    浅色界面里纯白卡片和近白背景之间要有极淡的一圈描边，
    分层才「立」得起来（hairline border）。
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
    Clock.schedule_once(lambda *_: _sync(widget), 0)   # 首帧布局后校准
    # 保持引用，防止被 GC
    widget._border_line = line
    widget._border_color = c
    return widget


def attach_glow(widget, color=None, spread=None, alpha=0.30):
    """彩色柔光（旧暗色主题遗留；浅色主题里就是另一种投影）。"""
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
    Clock.schedule_once(lambda *_: _sync(widget), 0)  # 首帧布局后再校准
    widget._glow_rect = rect
    widget._glow_color = c
    return widget


class CNSpinnerOption(SpinnerOption):
    """下拉列表项 —— iOS picker 的白底行：左对齐 + 发丝分隔线 + 按下变灰。

    两个坑（别改回去）：
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
        attach_bg(self, C_ITEM)                     # 白底行
        attach_sep(self)                            # 发丝线
        self.bind(state=self._hl)

    def _hl(self, *_):
        c = getattr(self, "_att_bg_c", None)
        if c is not None:
            c.rgba = (0.905, 0.905, 0.918, 1) if self.state == "down" else C_ITEM


class CNDropdown(DropDown):
    """下拉框本体：白色圆角卡 + 投影 + 内边距，隐藏滚动条。

    默认 DropDown 是裸 ScrollView：没背景没留白，拉开就是一列灰方块
    （「像老安卓」的观感来源）。类名保持 CNDropdown —— 测试在守它。
    """

    def __init__(self, **kw):
        kw.setdefault("max_height", dp(320))
        kw.setdefault("bar_width", 0)          # 隐藏滚动条，靠留白区分
        super().__init__(**kw)
        # 淡入：DropDown 挂上窗口即出现，从 0.6 起（万一动画没跑也基本可见）
        try:
            self.opacity = 0.6
            Animation(opacity=1.0, d=0.16, t="out_quad").start(self)
            Clock.schedule_once(lambda *_: setattr(self, "opacity", 1.0), 0.6)
        except Exception:
            pass
        try:
            c = self.container
            if c is not None:
                c.padding = (dp(6), dp(6))
                c.spacing = 0
                attach_shadow(c, spread=dp(16), alpha=0.20, squash=0.5)
                attach_bg(c, C_CARD, radius=R_MD)
                attach_border(c, radius=R_MD, color=(0.235, 0.235, 0.263, 0.08))
        except Exception:
            log_exc("CNDropdown 背景")


class CNSpinner(Spinner):
    """下拉框。做三件事：

    1) 下拉列表项不会继承 font_name，中文会显示成方块 —— 用 option_cls
       在创建时就带上字体。
    2) 默认的下拉外观是「老安卓」风格 —— 换成 dropdown_cls + 白色圆角卡。
    3) Kivy 的 Spinner 默认用灰底贴图，浅色 iOS 主题不要贴图，
       背景一律关掉（background_normal=""），由外层单元格提供底色。

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
        kw.setdefault("background_color", (0, 0, 0, 0))
        kw.setdefault("color", C_DIM)                     # iOS：单元格值用次级灰
        kw.setdefault("halign", "right")
        kw.setdefault("option_cls", CNSpinnerOption)   # 下拉项：白底 + 中文字体
        kw.setdefault("dropdown_cls", CNDropdown)      # 下拉框：白卡 + 留白
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


class FlatButton(Button):
    """扁平圆角按钮（iOS 质感）。

    背景用自绘圆角矩形，所以要把 Kivy 默认的灰色贴图关掉
    （background_normal="" + background_color 透明）。
    扩展：
      bg_color  ListProperty  底色
      radius    NumericProperty  圆角 dp；胶囊传 pill=True
      shadow    bool          构造参数：是否带投影（必须在底色之前画）
    """
    bg_color = ListProperty([1, 1, 1, 1])
    radius = NumericProperty(R_MD)

    def __init__(self, **kw):
        self._pill = bool(kw.pop("pill", False))
        want_shadow = bool(kw.pop("shadow", False))
        self._radius_shape = None          # 非数字圆角（4 角列表）时用
        kw.setdefault("background_normal", "")
        kw.setdefault("background_down", "")
        kw.setdefault("background_color", (0, 0, 0, 0))   # 关掉默认灰底
        super().__init__(**kw)
        # 注意：bg_color 是通过 kwargs 设进来的，Kivy 会在 super().__init__()
        # 里就触发 on_bg_color，那时 canvas 图元还不存在 —— 所以那里必须判空。
        from kivy.graphics import Color, RoundedRectangle
        if want_shadow:
            attach_shadow(self, spread=dp(10), alpha=0.16, squash=0.45)
        with self.canvas.before:
            self._bg_color = Color(*self.bg_color)
            self._bg_shape = RoundedRectangle(
                pos=self.pos, size=self.size, radius=self._sync_radius())
        self.bind(pos=self._sync, size=self._sync)

    def set_radius(self, shape):
        """shape 可为数字 dp、'pill'、或 [上右, 上左, 下左, 下右] 四元列表"""
        self._radius_shape = shape
        self._sync()

    def _sync_radius(self):
        s = self._radius_shape if self._radius_shape is not None else self.radius
        if isinstance(s, str) or (self._pill and not isinstance(s, list)):
            return [max(self.height / 2.0, 1.0)]
        if isinstance(s, (list, tuple)):
            return [dp(r) for r in s]
        return [dp(s)]

    def _sync(self, *_):
        shape = getattr(self, "_bg_shape", None)
        if shape is None:
            return
        shape.pos = self.pos
        shape.size = self.size
        shape.radius = self._sync_radius()

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
        # 按下反馈：iOS 的 touch-down 是把底色**压深一档**
        #（白 → #E5E5E5 的触感），0.90 在浅底上刚刚好。
        k = 0.90 if self.state == "down" else 1.0
        c.rgba = (r * k, g * k, b * k, a)

    def on_bg_color(self, *_):
        self._apply_bg()

    def on_state(self, *_):
        self._apply_bg()


class SpringButton(FlatButton):
    """真·iOS Q弹：**整个控件**（底、文字、图标）一起缩小再弹簧回位。

    旧版只缩背景形状 —— 字和图标不动，弹起来像「壳在抖」，发僵。
    矩阵做法：canvas.before 最前插 PushMatrix+Translate+Scale，
    canvas.after 末尾 PopMatrix；布局尺寸全程不动（Scale 只改绘制矩阵，
    不和 BoxLayout 打架）。围绕中心缩放 = T(c(1-s))·S(s)：
    p' = s·p + c(1-s) = c + s·(p−c) ✓
    """
    press_scale = NumericProperty(0.93)
    _press = NumericProperty(0.0)      # 0=常态 1=完全按下

    def __init__(self, **kw):
        self._haptic = kw.pop("haptic", True)
        super().__init__(**kw)         # FlatButton 先把背景画进 canvas.before
        from kivy.graphics import (PopMatrix, PushMatrix, Scale, Translate)
        self._mt_push = PushMatrix()
        self._mt_tr = Translate(0, 0, 0)
        self._mt_scale = Scale(1, 1, 1)
        self._mt_pop = PopMatrix()
        cb = self.canvas.before        # insert(0)：矩阵要排在背景图元**之前**
        cb.insert(0, self._mt_scale)
        cb.insert(0, self._mt_tr)
        cb.insert(0, self._mt_push)
        self.canvas.after.add(self._mt_pop)
        self.bind(state=self._on_state, _press=self._sync,
                  pos=self._sync, size=self._sync)
        # ⚠ 保险丝（和全局动画同一方法论）：KV 会在构造期间就画第一帧，
        # 那时 pos/size 还是布局前默认值（100x100@0,0）—— 矩阵按错的中心
        # 缩放过一次，静态场景之后不重画，按钮就永久错位缩小（实测丢在角落）。
        # 布局完成后强制把矩阵写回正确值。
        Clock.schedule_once(self._sync, 0)

    def _on_state(self, *_):
        down = self.state == "down"
        if down and self._haptic:
            vibrate(8)
        Animation(
            _press=1.0 if down else 0.0,
            duration=0.09 if down else 0.52,
            t="out_quad" if down else spring_t,
        ).start(self)

    def _sync(self, *_):
        FlatButton._sync(self)          # ⚠ 先让背景形状跟随 pos/size ——
        # 这里覆盖了 FlatButton._sync，忘了转调用就会把「形状跟随布局」
        # 的责任吞掉：按钮永远画在布局前的默认 100x100 处（实测缩在角落）。
        sc = getattr(self, "_mt_scale", None)
        tr = getattr(self, "_mt_tr", None)
        if sc is None or tr is None:
            return
        s = 1.0 - self._press * (1.0 - self.press_scale)
        sc.x = sc.y = sc.z = s
        cx = self.x + self.width / 2.0
        cy = self.y + self.height / 2.0
        tr.xyz = (cx * (1.0 - s), cy * (1.0 - s), 0)
        try:
            self.canvas.ask_update()
        except Exception:
            pass


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
    elif kind == "chevron":
        # iOS 表格行尾的「›」
        out.append(("line", {"points": [cx - s * 0.22, cy + s * 0.50,
                                        cx + s * 0.28, cy,
                                        cx - s * 0.22, cy - s * 0.50],
                             "width": 1.6 * unit}))
    return out


def draw_icon(widget, kind, color=None, size=None):
    """把图标**画**在控件上，而不是用字符。

    为什么不直接写 ⚙ / 🔍 这类字符：自带的中文字体是从 Noto Sans SC
    裁出来的，emoji 和大部分几何符号根本不在里面，写上去就是方块。
    自己用 canvas 画最稳，而且线条更锐利、更贴合极简风格。

    ⚠ 图元必须用**父坐标系**（widget.x / widget.y 当原点）：
    往 widget.canvas 加的东西和 widget.pos 在同一个坐标空间，
    用本地坐标（0..w）会把图标画到父容器原点上去 ——
    这就是「×」跑到屏幕左下角、按钮上只剩一个空圆底的原因。

    重复调用同一个控件时不会叠加图元：几何/颜色改走控件属性
    （_icon_kind / _icon_color），再调一次只刷新状态并即时重画
    —— 播放键那种「按文本切换 play/pause 图形」靠这个实现。
    """
    from kivy.graphics import (Color, InstructionGroup, Line, Mesh, PushMatrix,
                               PopMatrix, Translate)
    _DRAW = {"line": Line, "mesh": Mesh}
    widget._icon_kind = kind
    widget._icon_color = list(color or C_TEXT)

    def _geom_key():
        return (round(max(1.0, widget.width), 1),
                round(max(1.0, widget.height), 1),
                widget._icon_kind, tuple(widget._icon_color))

    def _sync(*_):
        # 结构：grp = [Translate, body]。
        #   位置变化（滚动就是一直变）→ 只改 Translate 的 xyz，零重建；
        #   尺寸/图形/颜色变化 → 重建 body 里的几何（少见）。
        # 图标画进自己的 InstructionGroup：不能用 canvas.after.clear()，
        # 那会把 attach_border/attach_sep 加在同一 canvas.after 上的线一起清掉。
        grp = getattr(widget, "_icon_group", None)
        if grp is None or grp not in widget.canvas.after.children:
            grp = InstructionGroup()
            widget._icon_group = grp
            # ⚠ Translate 直接改 GL 上下文矩阵，Kivy 的 InstructionGroup
            # **不会**自动 save/restore —— 不加 Push/Pop 会把偏移泄漏给
            # 之后画的一切（实测整屏错乱、读回全黑）。必须成对包起来。
            grp.add(PushMatrix())
            widget._icon_tr = Translate(widget.x, widget.y, 0)
            grp.add(widget._icon_tr)
            widget._icon_body = InstructionGroup()
            grp.add(widget._icon_body)
            grp.add(PopMatrix())
            widget.canvas.after.add(grp)
            # SpringButton 的「大弹」pop 挂在 canvas.after 末尾；图标组比它晚加，
            # 必须把 pop 重新挪到最后 —— 否则图标落在 pop 之后不参与缩放，
            # 弹起来壳动图标不动，就是「乱」。
            big_pop = getattr(widget, "_mt_pop", None)
            if big_pop is not None and big_pop in widget.canvas.after.children:
                widget.canvas.after.remove(big_pop)
                widget.canvas.after.add(big_pop)
        else:
            widget._icon_tr.xyz = (widget.x, widget.y, 0)
        key = _geom_key()
        if getattr(widget, "_icon_key", None) == key:
            return
        widget._icon_key = key
        w = max(1.0, widget.width)
        h = max(1.0, widget.height)
        body = widget._icon_body
        body.clear()
        body.add(Color(*widget._icon_color))
        for op, kw in icon_parts(widget._icon_kind, w, h, 0.0, 0.0, dp(1.0)):
            body.add(_DRAW[op](**kw))

    if not getattr(widget, "_icon_bound", False):
        widget._icon_bound = True
        widget.bind(pos=_sync, size=_sync)
    widget._icon_redraw = _sync
    _sync()
    return widget


class IconButton(SpringButton):
    """只有图标的圆形按钮（默认白底 + 投影，iOS 悬浮钮质感）"""

    def __init__(self, kind, dia=None, color=None, bg=None,
                 icon_color=None, shadow=False, **kw):
        # dp() 不能在默认参数里求值（导入期就要窗口），所以在这里算
        if dia is None:
            dia = dp(40)
        if bg is None:
            bg = (1, 1, 1, 1)
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dia, dia))
        # 图标必须自己声明垂直居中：Kivy 的 BoxLayout 在交叉轴（竖直方向）
        # **不会**居中固定尺寸的子控件，而是把它贴到内容区底部 ——
        # 于是图标高/矮于同行文字时就错位（头部/抽屉都栽过，别删 pos_hint）
        kw.setdefault("pos_hint", {"center_y": 0.5})
        kw.setdefault("bg_color", bg)
        kw.setdefault("color", (0, 0, 0, 0))   # 不显示文字
        kw["pill"] = True                      # 正圆 = 胶囊（高的一半）
        kw["shadow"] = shadow
        super().__init__(**kw)
        draw_icon(self, kind, color=icon_color or C_TEXT)


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


class VCenter(FloatLayout):
    """把一个固定高度的控件**垂直居中**在给定行高里。

    Kivy 的 BoxLayout 交叉轴不会居中固定尺寸的子控件（贴内容底边），
    且 BoxLayout 里 pos_hint 被**无视** —— 图标钮比行矮一点点就会
    「偏一点点」（用户反复说的强迫症问题，头部/播放条/列表行都中招）。
    套一层 FloatLayout 让 pos_hint 生效即可。
    """

    def __init__(self, inner, w=None, h=None, **kw):
        if h is None:
            h = inner.height or dp(44)
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", h)
        if w is not None:
            kw.setdefault("size_hint_x", None)
            kw.setdefault("width", w)
        super().__init__(**kw)
        inner.pos_hint = {"center_x": 0.5, "center_y": 0.5}
        self.add_widget(inner)


class Scrim(Widget):
    """模态遮罩：抽屉打开时压暗整屏并**吞掉**触摸（真 iOS 就是这样）。

    旧版只是在主内容 canvas.after 画了块半透明黑 —— 看得见但拦不住点，
    「面板开着还能点到底下的歌」就是这么来的。
    """

    def __init__(self, app, **kw):
        kw.setdefault("size_hint", (1, 1))
        super().__init__(**kw)
        self._app = app
        from kivy.graphics import Color, Rectangle
        with self.canvas:
            self._c = Color(0, 0, 0, 0)
            self._r = Rectangle(pos=self.pos, size=self.size)

        def _sync(*_):
            self._r.pos = self.pos
            self._r.size = self.size
        self.bind(pos=_sync, size=_sync)
        _sync()

    def on_touch_down(self, touch):
        a = getattr(self._app, "_scrim_a", 0.0)
        if a < 0.02:
            return False                     # 关闭态完全不拦截
        if self.collide_point(*touch.pos):
            if a > 0.15:
                self._app.close_settings()   # 点空白处收起抽屉
            return True
        return False


class SeekBar(Slider):
    """iOS 播放进度条：自己画轨道/已播段/游标，不用 Kivy 默认贴图。

    Slider 的贴图（灰轨道 + 蓝游标）在浅色界面上非常「安卓 4.4」。
    这里把 background/cursor 贴图清空，几何画在 canvas.after 上，
    坐标同样走父坐标系（和 draw_icon 一个道理）。
    值域仍是 0..1000（编排代码 _tick/_seek_up 依赖），只是画得好看。
    """

    def __init__(self, **kw):
        # Kivy 2.3 的 Slider 样式属性名（别写 1.x 的 background_normal/cursor，
        # 那些属性不存在，传进 __init__ 直接 TypeError）。
        # ⚠ 不要把 cursor_image / background_horizontal 设成空串 —— 真机上
        # 空贴图会污染整个 GL 帧。但默认 cursor 是 22px 的安卓灰球，
        # 直接盖不住（用户：「阴影还没去除」）—— 把它缩成 1px 就消失了；
        # 拖动热区随之改 sensitivity='all'：整条滑轨都能拖，比原来跟手，
        # 也更 iOS。自绘白圆 + 1px 描边照常画在 canvas.after。
        kw.setdefault("cursor_size", (dp(1), dp(1)))
        kw.setdefault("sensitivity", "all")
        kw.setdefault("value_track", False)
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", dp(44))      # 触摸目标 ≥44dp（HIG）
        super().__init__(**kw)
        from kivy.graphics import Color, RoundedRectangle
        self._kn = dp(18)
        # 播放游标：**系统蓝实心 + 白描边**（白圆在浅灰未播轨上看不出，
        # 用户点名改蓝）。蓝球骑在蓝段端点时靠 2dp 白圈分界；
        # Kivy 默认 22px 灰球已被 cursor_size=1px 消除，无任何投影。
        with self.canvas.after:
            self._tr_c = Color(0.878, 0.878, 0.894, 1)     # 未播段浅灰
            self._tr = RoundedRectangle(radius=[dp(3)])
            self._fl_c = Color(*C_ACCENT)                   # 已播段系统蓝
            self._fl = RoundedRectangle(radius=[dp(3)])
            self._kb_c = Color(1, 1, 1, 1)                  # 游标白圈底
            self._kb = RoundedRectangle(radius=[(self._kn + dp(4)) / 2.0])
            self._kn_c = Color(*C_ACCENT)
            self._kn_s = RoundedRectangle(radius=[self._kn / 2.0])
        self.bind(pos=self._redraw, size=self._redraw,
                  value=self._redraw, min=self._redraw, max=self._redraw)
        self._redraw()

    def _frac(self):
        try:
            span = float(self.max - self.min) or 1.0
            return max(0.0, min(1.0, (float(self.value) - self.min) / span))
        except Exception:
            return 0.0

    def _redraw(self, *_):
        cy = self.y + self.height / 2.0
        th = max(1.0, dp(6))
        x0 = self.x
        w = max(0.0, self.width)
        f = self._frac()
        self._tr.pos = (x0, cy - th / 2.0)
        self._tr.size = (w, th)
        self._fl.pos = (x0, cy - th / 2.0)
        self._fl.size = (max(th, w * f), th)
        kx = x0 + w * f
        d1 = self._kn
        d2 = self._kn + dp(4)
        self._kb.pos = (kx - d2 / 2.0, cy - d2 / 2.0)
        self._kb.size = (d2, d2)
        self._kn_s.pos = (kx - d1 / 2.0, cy - d1 / 2.0)
        self._kn_s.size = (d1, d1)


class ProgressCapsule(ProgressBar):
    """下载进度：4dp 细胶囊（浅灰轨道 + 系统蓝填充），替换默认粗贴图。"""

    def __init__(self, **kw):
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", dp(4))
        super().__init__(**kw)
        # Kivy 2.3 的 <ProgressBar> kv 把默认贴图**写死**在 canvas 上
        # （24px 高的灰条会漏在细胶囊上下），属性关不掉 —— 直接清空
        # 主 canvas 的图元，再由 canvas.after 画我们的细胶囊。
        try:
            self.canvas.clear()
        except Exception:
            log_exc("ProgressCapsule 清默认图元")
        from kivy.graphics import Color, RoundedRectangle
        with self.canvas.after:
            self._tr_c = Color(0.878, 0.878, 0.894, 1)
            self._tr = RoundedRectangle(radius=[dp(2)])
            self._fl_c = Color(*C_ACCENT)
            self._fl = RoundedRectangle(radius=[dp(2)])
        # Kivy 2.3 的 ProgressBar **没有** min 属性（只有 value/max）——
        # bind(min=...) 会 KeyError，别按老文档写。
        self.bind(pos=self._redraw, size=self._redraw, value=self._redraw,
                  max=self._redraw)
        self._redraw()

    def _redraw(self, *_):
        self._tr.pos = self.pos
        self._tr.size = self.size
        try:
            span = float(self.max) or 1.0
            f = max(0.0, min(1.0, float(self.value) / span))
        except Exception:
            f = 0.0
        h = max(1.0, self.height)
        self._fl.pos = self.pos
        self._fl.size = (max(h, self.width * f), h)


class PlayButton(SpringButton):
    """圆形播放键：只显示图标，图标跟随按钮文字（播放/暂停）自动切换。

    编排代码在播放/暂停/出错/结束时一律用 `btn_play.text = "暂停"/"播放"`
    同步状态 —— 这是它的「逻辑接口」，不能动；这个控件把文本翻译成图形，
    文字本身压成透明，观感就是 Apple Music 底部那个蓝色圆钮。
    图标切换的瞬间补一发弹簧脉冲（morph 感：状态一变，钮「活」一下）。
    """

    def __init__(self, dia=None, **kw):
        if dia is None:
            dia = dp(50)
        for k, v in fonts.font_kwargs().items():
            kw.setdefault(k, v)        # text 是状态载体，字体照样要带（防方块）
        kw.setdefault("size_hint", (None, None))
        kw.setdefault("size", (dia, dia))
        kw.setdefault("bg_color", C_ACCENT)
        kw.setdefault("color", (0, 0, 0, 0))
        kw.setdefault("halign", "center")
        kw["pill"] = True
        super().__init__(**kw)
        self.bind(text=self._sync_icon)
        draw_icon(self, "play", color=C_WHITE)
        self._sync_icon()

    def _sync_icon(self, *_):
        want = "pause" if (self.text or "") == "暂停" else "play"
        if want != getattr(self, "_icon_kind", "play"):
            self._icon_kind = want
            self._icon_redraw()
            self._press = 0.5          # 小弹一下：图标形变瞬间的「活着」感
            Animation(_press=0.0, d=0.52, t=spring_t).start(self)


# ============================================================
#  主界面
# ============================================================
class LxApp(App):
    title = "落雪音源下载器"
    # 模态遮罩的不透明度（动画驱动，Scrim 图元的 alpha 跟着它走）
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

        root = FloatLayout()
        attach_bg(root, C_BG)
        # 动态背景必须在 attach_bg 之后建：canvas.before 里按插入顺序绘制，
        # 彩色晕团要压在底色之上、卡片之下。
        # 兜底：背景只是装饰，建不起来也绝不能拦住 App 启动。
        try:
            self.bgfx = bgfx.MusicBackground(root)
        except Exception:
            log_exc("创建动态背景")
            self.bgfx = None

        # 主内容（大标题 / 搜索胶囊 / 分组列表 / 迷你播放器）
        self.main = BoxLayout(orientation="vertical", size_hint=(1, 1))
        # 抽屉打开时主内容**缩到后方**（iOS：presentation 背后的视图
        # scale 0.965 + 变暗）。矩阵挂在 main 自己的 canvas 两端，
        # before/after 目前没人用，整组独占、顺序安全。
        from kivy.graphics import (PopMatrix as _PoP, PushMatrix as _PuS,
                                   Scale as _Sca, Translate as _Tra)
        self._main_push = _PuS()
        self._main_tr = _Tra(0, 0, 0)
        self._main_scale = _Sca(1, 1, 1)
        self._main_pop = _PoP()
        self.main.canvas.before.add(self._main_push)
        self.main.canvas.before.add(self._main_tr)
        self.main.canvas.before.add(self._main_scale)
        self.main.canvas.after.add(self._main_pop)
        self.main.add_widget(self._build_header())
        self.main.add_widget(self._build_search())
        self.main.add_widget(self._build_hist())
        self.main.add_widget(self._build_results())
        self.main.add_widget(self._build_footer())
        root.add_widget(self.main)

        # 全屏遮罩：抽屉打开时压暗整屏并吞掉点击（真 iOS 的 modal 层级）
        self.scrim = Scrim(self)
        root.add_widget(self.scrim)
        self._scrim_c = self.scrim._c
        self._scrim_r = self.scrim._r

        # 底部抽屉：FloatLayout 的**覆盖层**子控件，初始藏在屏幕下沿外。
        # 为什么动画 y 而不是高度：覆盖层不进布局排布；pos_hint 里**不钉 y**，
        # 布局就不会把动画位置抢回去 —— 旧版挂在 BoxLayout 里只能"顶内容"，
        # 不是"浮起来盖住内容"，层级感完全不对。
        self.sheet = BoxLayout(orientation="vertical", size_hint=(1, None),
                               padding=(dp(18), dp(6), dp(18), dp(12)),
                               spacing=dp(14))
        attach_shadow(self.sheet, spread=dp(26), alpha=0.24, squash=0.4)
        attach_bg(self.sheet, C_CARD, radius=[28, 28, 0, 0])   # 顶部两角圆
        self.sheet.add_widget(self._sheet_head())
        # 套一层滚动：配置项不少，小屏上必须能滚，否则底部几项会被切掉
        sv = ScrollView(bar_width=0, do_scroll_x=False,
                        effect_cls="ScrollEffect")   # iOS 没有橡皮筋发光
        inner = self._build_panel()
        sv.add_widget(inner)
        self.sheet.add_widget(sv)
        # 抽屉目标高度：屏高的 78% 与 620dp 取小，底下留一点主内容透出遮罩
        try:
            from kivy.core.window import Window as _W
            self._sheet_h = min(dp(620), max(dp(320), _W.height * 0.78))
        except Exception:
            self._sheet_h = dp(480)
        self.sheet.height = self._sheet_h
        self.sheet.pos_hint = {"center_x": 0.5}      # 只钉横向，y 归动画
        self.sheet.y = -self._sheet_h
        root.add_widget(self.sheet)
        self._sheet_open = False

        Clock.schedule_once(self._guard(self._boot), 0.2)
        Clock.schedule_interval(self._guard(self._tick), 0.5)

        # 启动淡入：界面从透明浮出来，比「啪」一下出现柔和得多。
        # 保险丝：1.5s 后无条件把 opacity 拉回 1 —— 万一动画没跑起来，
        # 整个界面会是全透明的，那比没有动效糟得多（而且我看不到真机）。
        root.opacity = 0.0
        Clock.schedule_once(
            lambda *_: Animation(opacity=1.0, d=0.28, t="out_quad").start(root),
            0.05)
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
        """iOS 大标题（Large Title）+ 右上角悬浮设置圆钮。

        配置项全部收进底部抽屉 —— 原来顶部挂一整块控制面板，
        小屏上几乎把歌曲列表挤没了。
        """
        box = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(94),
                        padding=(dp(20), dp(10), dp(20), dp(2)), spacing=dp(0))
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(10))
        t = Label(text="落雪音源", bold=True, font_size=dp(27),
                  halign="left", valign="middle", color=C_TEXT, **self.F)
        t.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        row.add_widget(t)
        row.add_widget(Widget())
        self.btn_set = IconButton("settings", dia=dp(42), icon_color=C_DIM,
                                  shadow=True)
        self.btn_set.bind(on_release=lambda *_: self.open_settings())
        row.add_widget(VCenter(self.btn_set, w=dp(46), h=dp(46)))
        box.add_widget(row)
        self.lbl_sub = Label(text="", size_hint_y=None, height=dp(22),
                             font_size=dp(12), color=C_FAINT,
                             halign="left", valign="middle", **self.F)
        self.lbl_sub.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(self.lbl_sub)
        return box

    def _build_search(self):
        """搜索条：iOS 胶囊输入（聚焦染蓝）+ 系统蓝胶囊按钮"""
        wrap = BoxLayout(size_hint_y=None, height=dp(56),
                         padding=(dp(16), dp(6), dp(16), dp(6)), spacing=dp(10))

        # 输入框是 secondarySystemFill 胶囊，放大镜画在它左内侧
        self.ti_box = BoxLayout(size_hint_y=None, height=dp(44))
        attach_bg(self.ti_box, C_CTRL, radius="pill")

        ico = Widget(size_hint=(None, None), size=(dp(30), dp(44)),
                     pos_hint={"center_y": 0.5})
        draw_icon(ico, "search", color=C_DIM)
        self.ti_box.add_widget(ico)

        self.ti_search = SearchInput(hint_text="歌曲、歌手", multiline=False,
                                     font_size=dp(16), padding=(dp(4), dp(11)),
                                     background_color=(0, 0, 0, 0),
                                     foreground_color=C_TEXT,
                                     hint_text_color=C_DIM,
                                     cursor_color=C_ACCENT,
                                     cursor_width=dp(2), **self.F)
        self.ti_search.bind(on_text_validate=self._go_search)
        self.ti_box.add_widget(self.ti_search)
        wrap.add_widget(self.ti_box)

        # 搜索按钮：系统蓝胶囊，与输入框等高
        self.btn_search = SpringButton(text="搜索", bold=True, font_size=dp(16),
                                       size_hint=(None, None),
                                       size=(dp(78), dp(44)),
                                       bg_color=C_ACCENT, pill=True, shadow=True,
                                       color=C_WHITE, **self.F)
        self.btn_search.bind(on_release=self._go_search)
        wrap.add_widget(self.btn_search)

        # 聚焦反馈：胶囊底色淡染成蓝（iOS search field 的高亮方式）+
        # 聚焦时展开搜索历史（App Store 搜索页的做法）
        def _focus(ti, focused):
            c = getattr(self.ti_box, "_att_bg_c", None)
            if c is not None:
                c.rgba = (0.855, 0.905, 1.0, 1) if focused else C_CTRL
            if focused:
                self._show_hist()
            else:
                # 延迟收起：点胶囊会先让输入框失焦，立刻收就点不到胶囊了
                Clock.schedule_once(lambda *_: (
                    self._hide_hist() if not ti.focus else None), 0.18)
        self.ti_search.bind(focus=_focus)
        return wrap

    # ---------- 搜索历史 ----------
    # 只存关键词、只走 UI 入口：do_search / _search_work 一字未动
    HIST_MAX = 20

    def _hist_path(self):
        return os.path.join(appenv.APP_DIR, "search_history.json")

    def _load_history(self):
        if getattr(self, "_history", None) is not None:
            return self._history
        self._history = []
        try:
            with open(self._hist_path(), encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                self._history = [str(x) for x in d][:self.HIST_MAX]
        except Exception:
            pass
        return self._history

    def _save_history(self):
        try:
            with open(self._hist_path(), "w", encoding="utf-8") as f:
                json.dump(self._history[:self.HIST_MAX], f, ensure_ascii=False)
        except Exception:
            log_exc("保存搜索历史")

    def _add_history(self, kw):
        kw = (kw or "").strip()
        if not kw:
            return
        h = self._load_history()
        if kw in h:
            h.remove(kw)          # 搜过的挪到最前（iOS 最近搜索的排序）
        h.insert(0, kw)
        del h[self.HIST_MAX:]
        self._save_history()
        self._rebuild_chips()

    def _clear_history(self, *_):
        try:
            vibrate(8)
            self._history = []
            self._save_history()
            self._rebuild_chips()
        except Exception:
            log_exc("_clear_history")

    def _go_search(self, *_):
        """搜索按钮 / 回车的新入口：先记历史，再转原始逻辑 do_search"""
        kw = (self.ti_search.text or "").strip()
        if kw:
            self._add_history(kw)
            self._hide_hist()
        self.do_search()

    def _build_hist(self):
        """历史面板：组标题行（左「搜索历史」右「清除」）+ 胶囊横滑行。
        默认高度 0 收起；聚焦搜索框时弹簧展开（iOS 搜索页的下推动作）。"""
        box = BoxLayout(orientation="vertical", size_hint_y=None, height=0,
                        padding=(dp(16), dp(2), dp(16), dp(4)), spacing=dp(6))
        head = BoxLayout(size_hint_y=None, height=dp(24))
        lb = Label(text="搜索历史", font_size=dp(13), color=C_DIM, bold=True,
                   halign="left", valign="middle", **self.F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        head.add_widget(lb)
        head.add_widget(Widget())
        clr = FlatButton(text="清除", size_hint=(None, None), size=(dp(56), dp(24)),
                         font_size=dp(13), color=C_ERR, bg_color=(0, 0, 0, 0),
                         pos_hint={"center_y": 0.5}, **self.F)
        clr.bind(on_release=self._clear_history)
        head.add_widget(clr)
        box.add_widget(head)
        self._hist_sv = ScrollView(do_scroll_y=False, size_hint_y=None, height=dp(34))
        self._hist_row = BoxLayout(size_hint_y=None, height=dp(34), spacing=dp(8),
                                   padding=(0, 0))
        self._hist_row.bind(minimum_width=self._hist_row.setter("width"))
        self._hist_sv.add_widget(self._hist_row)
        box.add_widget(self._hist_sv)
        self._hist_box = box
        self._hist_open = False
        self._rebuild_chips()
        return box

    def _rebuild_chips(self):
        if getattr(self, "_hist_row", None) is None:
            return                       # build() 早期被调时容错
        self._hist_row.clear_widgets()
        h = self._load_history()
        if not h:
            tip = Label(text="还没有搜索记录，搜过的歌名会出现在这里",
                        size_hint_x=None, width=dp(300), font_size=dp(12),
                        color=C_FAINT, halign="left", valign="middle", **self.F)
            tip.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
            self._hist_row.add_widget(tip)
            return
        from kivy.core.text import Label as CoreLabel
        for i, kw in enumerate(h[:10]):   # 一屏胶囊最多 10 个，横向可滑
            try:
                lb = CoreLabel(text=kw, font_size=dp(13), **self.F)
                lb.refresh()
                w = min(dp(220), lb.texture_size[0] + dp(26))
            except Exception:
                w = dp(26) + dp(13) * len(kw)
            chip = SpringButton(text=kw, font_size=dp(13),
                                size_hint=(None, None), size=(w, dp(34)),
                                bg_color=C_CTRL, color=C_TEXT, pill=True,
                                pos_hint={"center_y": 0.5}, **self.F)
            chip.bind(on_release=lambda b, k=kw: self._chip_tap(k))
            self._hist_row.add_widget(chip)
            if getattr(self, "_hist_open", False):
                # 逐个弹入：从 0.93 缩放回位，每个晚 30ms ——
                # iOS「最近搜索」展开时的那种涟漪感
                chip._press = 1.0
                Animation(_press=0.0, d=0.5, t=spring_t,
                          delay=0.03 * i).start(chip)

    def _chip_tap(self, kw):
        """点历史胶囊：填回搜索框并直接重搜"""
        try:
            vibrate(8)
            self.ti_search.text = kw
            self._add_history(kw)
            self._hide_hist()
            self._go_search()
        except Exception:
            log_exc("_chip_tap")

    def _show_hist(self, *_):
        try:
            if getattr(self, "_hist_open", False):
                return
            self._hist_open = True
            self._rebuild_chips()
            self._hist_box.opacity = 0.0
            Animation(height=dp(96), opacity=1.0, d=0.30,
                      t=spring_t).start(self._hist_box)
            # 保险丝：动画没跑起来也落到展开态
            Clock.schedule_once(
                lambda *_: setattr(self._hist_box, "height", dp(96)), 0.8)
        except Exception:
            log_exc("_show_hist")

    def _hide_hist(self):
        try:
            if not getattr(self, "_hist_open", False):
                return
            self._hist_open = False
            Animation(height=0, opacity=0.0, d=0.22,
                      t="out_cubic").start(self._hist_box)
            Clock.schedule_once(
                lambda *_: setattr(self._hist_box, "height", 0), 0.6)
        except Exception:
            log_exc("_hide_hist")

    # ---- 分组列表小件 ----
    def _group(self):
        """一张白色分组卡（iOS Settings 的 group）：圆角 + 投影 + 单元格列表"""
        card = BoxLayout(orientation="vertical", size_hint_y=None, spacing=0)
        card.bind(minimum_height=card.setter("height"))
        attach_shadow(card, spread=dp(12), alpha=0.09, squash=0.5)
        attach_bg(card, C_CARD, radius=R_LG)
        return card

    def _cell(self, label, ctrl, chevron=True):
        """分组卡里的一行单元格：左标题 + 右控件（+ 行尾 ›），底部发丝线"""
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8),
                        padding=(dp(14), 0))
        lb = Label(text=label, font_size=dp(15), color=C_TEXT,
                   halign="left", valign="middle", size_hint_x=None,
                   width=dp(84), **self.F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        row.add_widget(lb)
        row.add_widget(ctrl)
        if chevron:
            ch = Widget(size_hint=(None, None), size=(dp(13), dp(48)),
                        pos_hint={"center_y": 0.5})
            draw_icon(ch, "chevron", color=C_FAINT)
            row.add_widget(ch)
        attach_sep(row, inset=dp(14))
        return row

    def _action_cell(self, text, color=None):
        """分组卡里的「操作行」：整行居中的彩色文字（iOS 的 destructive/link 行）"""
        b = FlatButton(text=text, size_hint_y=None, height=dp(48),
                       font_size=dp(15), bold=True, bg_color=C_ITEM,
                       color=color or C_ACCENT, halign="center", **self.F)
        attach_bg(b, (1, 1, 1, 0))            # 按下发灰由 FlatButton 自己管
        return b

    def _fix_group(self, *cards):
        """每张卡的最后一行不要发丝线（否则白卡底边挂一条悬空横线）"""
        for card in cards:
            kids = list(card.children)
            if kids:
                kids[0]._sep_hidden = True    # children 逆序：[0] 是最底行
                try:
                    kids[0]._sep_rect.size = (0, 0)
                except Exception:
                    pass

    def _build_panel(self):
        """设置抽屉的内容 —— iOS 设置页的「分组 + 单元格」结构。

        注意：sp_source / sp_platform / sp_quality / sp_format / sp_count /
        sp_dir / btn_proxy 这些名字**一个都不能改** —— 编排代码
        （_apply_source_info / _refresh_qualities / _resolve_song /
        _fill_dir_presets / _refresh_proxy_label）和测试都靠属性取控件。
        """
        panel = BoxLayout(orientation="vertical", size_hint_y=None,
                          padding=(dp(2), dp(2), dp(2), dp(6)), spacing=dp(16))
        panel.bind(minimum_height=panel.setter("height"))

        # ---- 音源 ----
        panel.add_widget(self._section("音源"))
        g1 = self._group()
        self.sp_source = CNSpinner(text="加载中…", values=[], font_size=dp(15),
                                   **self.F)
        self.sp_source.bind(on_text=self._on_source_picked)
        g1.add_widget(self._cell("音频源", self.sp_source))
        btn = self._action_cell("从手机导入新音源")
        btn.bind(on_release=self.pick_source)
        g1.add_widget(btn)
        panel.add_widget(g1)

        # ---- 平台与音质 ----
        panel.add_widget(self._section("平台与音质"))
        g2 = self._group()
        self.sp_platform = CNSpinner(text="—", values=[], font_size=dp(15),
                                     **self.F)
        self.sp_platform.bind(text=lambda *_: self._refresh_qualities())
        g2.add_widget(self._cell("平台", self.sp_platform))
        self.sp_quality = CNSpinner(
            text=songinfo.quality_label("320k"),
            values=[songinfo.quality_label(q) for q in QUALITY_ORDER],
            font_size=dp(15), **self.F)
        g2.add_widget(self._cell("音质", self.sp_quality))
        self.sp_format = CNSpinner(text="自动", values=songinfo.FORMAT_ORDER,
                                   font_size=dp(15), **self.F)
        self.sp_format.bind(text=lambda *_: self._refresh_qualities())
        g2.add_widget(self._cell("格式", self.sp_format))
        self.sp_count = CNSpinner(text="100 首", values=COUNT_ORDER,
                                  font_size=dp(15), **self.F)
        g2.add_widget(self._cell("数量", self.sp_count))
        panel.add_widget(g2)

        # ---- 下载 ----
        panel.add_widget(self._section("下载"))
        g3 = self._group()
        self.sp_dir = CNSpinner(text="—", values=[], font_size=dp(15), **self.F)
        self.sp_dir.bind(on_text=self._on_dir_preset)
        g3.add_widget(self._cell("保存位置", self.sp_dir))
        btn_dir = self._action_cell("选择其他目录")
        btn_dir.bind(on_release=self.pick_dir)
        g3.add_widget(btn_dir)
        panel.add_widget(g3)

        # ---- QQ 代理 ----
        panel.add_widget(self._section("QQ 代理（失效时可自己换）"))
        g4 = self._group()
        self.btn_proxy = self._action_cell("选代理文件(.txt)", color=C_ACCENT)
        self.btn_proxy.bind(on_release=self.pick_proxies)
        g4.add_widget(self.btn_proxy)
        panel.add_widget(g4)

        self._fix_group(g1, g2, g3, g4)
        return panel

    def _sheet_head(self):
        """抽屉顶部：居中抓取条（iOS detents 的小胶囊）+ 右侧关闭圆钮。

        用 FloatLayout：BoxLayout 里抓取条(5px)和关闭钮(34px)都贴底，
        中心线差 14px —— 「偏一点点」的重灾区。FloatLayout 才认 pos_hint。
        """
        row = FloatLayout(size_hint_y=None, height=dp(44))
        g = Widget(size_hint=(None, None), size=(dp(36), dp(5)),
                   pos_hint={"center_x": 0.5, "center_y": 0.5})
        attach_bg(g, (0.78, 0.78, 0.80, 1), radius="pill")
        row.add_widget(g)
        btn = IconButton("close", dia=dp(34), icon_color=C_DIM,
                         bg=(0.914, 0.914, 0.925, 1))
        btn.pos_hint = {"right": 1.0, "center_y": 0.5}
        btn.bind(on_release=lambda *_: self.close_settings())
        row.add_widget(btn)
        return row

    def _section(self, text):
        """分组页眉：小字灰色，左缩进 16（iOS grouped header）"""
        lb = Label(text=text, size_hint_y=None, height=dp(24),
                   font_size=dp(13), color=C_DIM, bold=True,
                   halign="left", valign="bottom",
                   padding=(dp(6), 0), **self.F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        return lb

    def _btn(self, text, color=None, w=None, bold=False, fs=None, h=None):
        b = FlatButton(text=text, size_hint_x=None if w else 1,
                       width=w or 0, font_size=fs or dp(14), bold=bold,
                       bg_color=color or C_CTRL, color=C_TEXT, **self.F)
        if h:
            # 直接加进竖向 BoxLayout 时必须给固定高度：那类面板的高度是
            # minimum_height（由子控件撑开），size_hint_y=1 的控件在那里会被
            # 算成 0 ——「选择代理文件」按钮就是这样塌成 0 高、整条看不见的。
            b.size_hint_y = None
            b.height = h
        return b

    def _field(self, text, w=None):
        lb = Label(text=text, size_hint_x=None, width=w or dp(42),
                   font_size=dp(14),
                   color=C_DIM, halign="left", valign="middle", **self.F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        return lb

    def _build_results(self):
        """结果区：一整张白色分组卡（iOS 列表），行与行靠发丝线分隔"""
        wrap = BoxLayout(orientation="vertical",
                         padding=(dp(16), dp(2), dp(16), dp(6)), spacing=dp(6))
        self.hint = Label(text="搜索后点结果即可播放或下载", size_hint_y=None,
                          height=dp(24), font_size=dp(12), color=C_FAINT,
                          halign="left", valign="middle", **self.F)
        self.hint.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        wrap.add_widget(self.hint)
        self.sv = ScrollView(bar_width=dp(2),
                             bar_color=(0.557, 0.557, 0.576, 0.55),
                             bar_inactive_color=(0, 0, 0, 0),
                             effect_cls="ScrollEffect")
        self.results = BoxLayout(orientation="vertical", size_hint_y=None,
                                 spacing=0, padding=(0, 0))
        self.results.bind(minimum_height=self.results.setter("height"))
        # 长列表容器**不要**投影：投影贴图会被拉伸到整卡高度
        # （300 首 ≈ 十几屏），滚动时每帧巨幅半透明 overdraw，就是「小卡」。
        # 用发丝描边分层，观感干净、成本近乎零。
        attach_bg(self.results, C_CARD, radius=R_LG)
        attach_border(self.results, radius=R_LG, color=(0.235, 0.235, 0.263, 0.10))
        self.sv.add_widget(self.results)
        wrap.add_widget(self.sv)
        return wrap

    def _build_footer(self):
        """底部迷你播放器卡（Apple Music 播放条的画法）：
        圆钮 + 自绘进度 + 时间，下面接细胶囊下载进度与状态行。"""
        outer = BoxLayout(orientation="vertical", size_hint_y=None,
                          padding=(dp(12), dp(4), dp(12), dp(10)))
        outer.bind(minimum_height=outer.setter("height"))
        box = BoxLayout(orientation="vertical", size_hint_y=None,
                        padding=(dp(14), dp(12)), spacing=dp(8))
        attach_shadow(box, spread=dp(18), alpha=0.15, squash=0.45)
        attach_bg(box, C_CARD, radius=22)
        box.bind(minimum_height=box.setter("height"))
        outer.add_widget(box)

        # ---- 播放条 ----
        prow = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(10))
        self.btn_play = PlayButton(text="播放", dia=dp(50))
        self.btn_play.bind(on_release=lambda *_: self.toggle_play())
        prow.add_widget(VCenter(self.btn_play, w=dp(52), h=dp(52)))

        self.slider = SeekBar(min=0, max=1000, value=0, step=1)
        self.slider.bind(on_touch_down=self._seek_down,
                         on_touch_up=self._seek_up)
        prow.add_widget(VCenter(self.slider, h=dp(52)))

        self.lbl_time = Label(text="00:00 / 00:00", size_hint_x=None,
                              width=dp(92), font_size=dp(12), color=C_DIM,
                              halign="right", valign="middle", **self.F)
        self.lbl_time.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        prow.add_widget(self.lbl_time)
        box.add_widget(prow)

        # ---- 下载进度 ----
        self.pb = ProgressCapsule(max=100)
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
            if self.status.text != text:      # 文本变了才淡入（进度高频刷新不闪）
                self.status.opacity = 0.4
                Animation(opacity=1.0, d=0.18, t="out_quad").start(self.status)
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
        """底部抽屉以**弹簧曲线**浮起盖住内容，同时全屏遮罩渐暗。

        动画走 `y`：抽屉是 FloatLayout 覆盖层、pos_hint 不钉 y，
        布局不会把动画位置抢回去（旧版在 BoxLayout 里只能动画 height，
        观感是「顶开内容」而不是「盖住内容」—— 层级完全不对）。
        弹簧参数照抄苹果 sheet：响应快、轻微过冲后定住。
        """
        try:
            if getattr(self, "_sheet_open", False):
                return
            self._sheet_open = True
            self._fill_dir_presets()
            self._refresh_proxy_label()
            vibrate(10)
            Animation(y=0, d=0.5, t=spring_t).start(self.sheet)
            Animation(_scrim_a=0.34, d=0.30, t="out_quad").start(self)
            self._main_zoom(0.965)
            # 保险丝：动画没跑起来也要落到最终位置，否则抽屉卡在半开
            Clock.schedule_once(
                lambda *_: setattr(self.sheet, "y", 0), 1.0)
        except Exception:
            log_exc("open_settings")

    def _main_zoom(self, s):
        """主内容围绕屏幕中心缩放到 s（配 translate 保持锚点在中心）"""
        try:
            cx = self.main.x + self.main.width / 2.0
            cy = self.main.y + self.main.height / 2.0
            d = 0.45 if s < 1.0 else 0.34
            t = spring_t if s < 1.0 else "out_cubic"
            Animation(x=s, y=s, z=s, d=d, t=t).start(self._main_scale)
            Animation(x=cx * (1 - s), y=cy * (1 - s), z=0, d=d, t=t).start(self._main_tr)
            Clock.schedule_once(lambda *_: self._main_settle(s), d + 0.4)
        except Exception:
            log_exc("main_zoom")

    def _main_settle(self, s):
        """保险丝：动画没跑起来也把矩阵写到最终值（矩阵停在半路 = 全局错位）"""
        try:
            self._main_scale.x = self._main_scale.y = self._main_scale.z = s
            cx = self.main.x + self.main.width / 2.0
            cy = self.main.y + self.main.height / 2.0
            self._main_tr.xyz = (cx * (1 - s), cy * (1 - s), 0)
        except Exception:
            pass

    def close_settings(self, *_):
        try:
            if not getattr(self, "_sheet_open", False):
                return
            self._sheet_open = False
            Animation(y=-self._sheet_h, d=0.34, t="out_cubic").start(self.sheet)
            Animation(_scrim_a=0.0, d=0.30, t="out_quad").start(self)
            self._main_zoom(1.0)
            Clock.schedule_once(
                lambda *_: setattr(self.sheet, "y", -self._sheet_h), 0.8)
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

        self.hint.text = "点歌名直接播放，点右侧下载（共 %d 首）" % len(self.songs)
        # 只给前几首做入场动效：几百首全做会明显卡，而且看不到那么远
        STAGGER = 10
        for i, s in enumerate(self.songs):
            row = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(4),
                            padding=(0, 0, dp(16), 0))
            # 歌名这块本身就是播放键 —— 点一下直接放，不再弹详情框
            song_btn = FlatButton(
                text="%s\n%s · %s" % (s["name"], s["singer"],
                                       s.get("interval") or "--:--"),
                halign="left", valign="middle", font_size=dp(15),
                color=C_TEXT, bg_color=C_ITEM, radius=0,
                padding=(dp(16), dp(6)), **self.F)
            song_btn.bind(size=lambda b, v: setattr(b, "text_size",
                                                    (v[0] - dp(32), None)))
            song_btn.bind(on_release=lambda b, idx=i: self.play_song(idx))
            row.add_widget(song_btn)
            row._song_btn = song_btn

            dl = IconButton("download", dia=dp(42),
                            icon_color=C_ACCENT,
                            bg=(0.926, 0.953, 1.0, 1))
            dl.bind(on_release=lambda b, idx=i: self.download_song(idx))
            row.add_widget(VCenter(dl, w=dp(58), h=dp(64)))
            # inset 16 = 和歌名文字起点对齐（song_btn 左内衬同为 16dp）
            attach_sep(row, inset=dp(16))
            self.results.add_widget(row)

            if i < STAGGER:
                # 错峰进场：淡入 + 高度弹簧展开，列表像「长」出来而不是一次砸下来
                row.opacity = 0.0
                row.height = dp(18)
                anim = Animation(opacity=1.0, height=dp(64), duration=0.42,
                                 t=spring_t)
                Clock.schedule_once(
                    lambda *_, r=row, a=anim: a.start(r), 0.036 * i)

        # 首/末行底色跟着分组卡做圆角 —— 直角白行会顶穿卡片的圆角
        kids = list(reversed(self.results.children))   # children 是逆序
        if kids:
            try:
                kids[0]._song_btn.set_radius([R_LG, R_LG, 0, 0])
                if len(kids) > 1:
                    kids[-1]._song_btn.set_radius([0, 0, R_LG, R_LG])
                kids[-1]._sep_hidden = True            # 最后一行不要发丝线
                kids[-1]._sep_rect.size = (0, 0)
            except Exception:
                log_exc("列表首末行圆角")

        # 保险丝：万一入场动画没跑起来，列表会停在全透明/零高度，
        # 那比没有动效糟得多 —— 到点无条件把最终状态写回去。
        if self.songs:
            def _settle(*_):
                try:
                    for w in self.results.children:
                        w.opacity = 1.0
                        if w.height < dp(64):
                            w.height = dp(64)
                except Exception:
                    log_exc("列表入场收尾")
            Clock.schedule_once(
                _settle, 0.036 * min(len(self.songs), STAGGER) + 0.7)

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
                   ("%s %s" % (songinfo.quality_label(quality), quality)),
                   (self._cur_ext or "?").upper(),
                   songinfo.human_size(meta.get("size")),
                   ("\n来源: %s %s" % (src, note)).rstrip() if src else ""))
        if getattr(self, "_pop_play", None) is not None:
            self._pop_play.disabled = False
            self._pop_dl.disabled = False
        self.set_status("已解析: %s（%s%s / %s / %s）"
                        % (song["name"],
                           ("%s " % src) if src else "",
                           ("%s %s" % (songinfo.quality_label(quality), quality)),
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
        """歌曲详情：iOS alert 式白卡（自绘圆角+投影，不用 Kivy 默认贴图边框）"""
        kw = dict(self.F)
        content = BoxLayout(orientation="vertical", spacing=dp(10),
                            padding=(dp(20), dp(4), dp(20), dp(16)))

        name = Label(text="%s\n%s" % (song["name"], song["singer"]),
                     size_hint_y=None, height=dp(56), font_size=dp(17),
                     bold=True, color=C_TEXT, halign="left", valign="middle",
                     **kw)
        name.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        content.add_widget(name)

        self._pop_info = Label(text="解析中…", font_size=dp(13), color=C_DIM,
                               halign="left", valign="top", **kw)
        self._pop_info.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        content.add_widget(self._pop_info)

        btns = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(10))
        self._pop_play = SpringButton(text="播放", font_size=dp(16), bold=True,
                                      bg_color=C_ACCENT, color=C_WHITE,
                                      pill=True, shadow=True,
                                      disabled=True, **kw)
        self._pop_play.bind(on_release=lambda *_: self.play_current())
        btns.add_widget(self._pop_play)

        self._pop_dl = SpringButton(text="下载", font_size=dp(16),
                                    bg_color=C_CTRL, color=C_TEXT,
                                    pill=True, disabled=True, **kw)
        self._pop_dl.bind(on_release=lambda *_: self.download_current())
        btns.add_widget(self._pop_dl)
        content.add_widget(btns)

        self._popup = Popup(title="歌曲信息", content=content,
                            size_hint=(0.88, None), height=dp(300),
                            title_size=dp(17), title_color=C_TEXT,
                            separator_color=(0, 0, 0, 0),
                            background_color=C_WHITE,
                            **self.P)
        # 换底：清掉 Popup 默认的贴图边框（那是「老安卓」观感来源），
        # 自绘「投影 + 白色圆角卡」。canvas.before 里按插入顺序绘制，
        # 投影必须在卡片之前 —— 顺序反了会把白卡糊黑。
        try:
            self._popup.canvas.before.clear()
            attach_shadow(self._popup, spread=dp(22), alpha=0.22, squash=0.5)
            attach_bg(self._popup, C_CARD, radius=R_LG)
        except Exception:
            log_exc("弹窗底样式")
        # 淡入。刻意从 0.86 而不是 0 起 —— 万一动画没跑起来，
        # 弹窗至少是「几乎全可见」，不会变成一个看不见却挡住点击的遮罩。
        self._popup.opacity = 0.86
        self._popup.open()
        Animation(opacity=1.0, duration=0.22, t=spring_t).start(self._popup)

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
