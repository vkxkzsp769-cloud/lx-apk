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
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

import appenv
import downloader
import fonts
import searchers
import player as player_mod
import qqresolve
import songinfo
from appenv import (IS_ANDROID, SOURCE_FILE, default_source_path, diag,
                    download_dir, ensure_source, extract_bundled_sources, log,
                    log_exc, request_all_files_access, save_source,
                    source_title)
from lxbridge import LxBridge

# ============================================================
#  主题
# ============================================================
C_BG = (0.082, 0.094, 0.118, 1)      # 页面底色
C_CARD = (0.129, 0.145, 0.180, 1)    # 卡片
C_CTRL = (0.192, 0.212, 0.259, 1)    # 输入/下拉
C_ITEM = (0.153, 0.173, 0.212, 1)    # 列表项
C_ACCENT = (0.290, 0.560, 0.950, 1)  # 强调蓝
C_ACCENT_D = (0.212, 0.435, 0.760, 1)
C_TEXT = (0.937, 0.945, 0.965, 1)
C_DIM = (0.596, 0.639, 0.710, 1)
C_FAINT = (0.435, 0.475, 0.545, 1)
C_OK = (0.345, 0.800, 0.502, 1)
C_ERR = (0.949, 0.451, 0.451, 1)

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
class CNSpinner(Spinner):
    """下拉框。做两件事：

    1) 下拉列表项不会继承 font_name，中文会显示成方块 —— 创建下拉后统一下发。
    2) Kivy 的 Spinner 默认用灰底贴图，跟这套深色主题不搭，
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
        super().__init__(**kw)

    def _create_dropdown(self, *largs):
        super()._create_dropdown(*largs)
        self._apply_font()

    def _apply_font(self):
        dd = getattr(self, "_dropdown", None)
        if dd is None or not self.font_name:
            return
        try:
            for child in dd.container.children:
                if hasattr(child, "font_name"):
                    child.font_name = self.font_name
                    if hasattr(child, "halign"):
                        child.halign = "left"
        except Exception:
            log_exc("CNSpinner 下拉字体")


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

    def on_bg_color(self, *_):
        c = getattr(self, "_bg_color", None)
        if c is not None:
            c.rgba = self.bg_color


# ============================================================
#  主界面
# ============================================================
class LxApp(App):
    title = "落雪音源下载器"

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

        root = BoxLayout(orientation="vertical")
        attach_bg(root, C_BG)

        root.add_widget(self._build_header())
        root.add_widget(self._build_panel())
        root.add_widget(self._build_results())
        root.add_widget(self._build_footer())

        Clock.schedule_once(self._guard(self._boot), 0.2)
        Clock.schedule_interval(self._guard(self._tick), 0.5)
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
        box = BoxLayout(size_hint_y=None, height=dp(56),
                        padding=(dp(18), dp(10)))
        attach_bg(box, C_CARD)
        t = Label(text="落雪音源下载器", bold=True, font_size=dp(19),
                  halign="left", valign="middle", color=C_TEXT, **self.F)
        t.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(t)
        self.lbl_sub = Label(text="", size_hint_x=None, width=dp(150),
                             font_size=dp(11), color=C_FAINT,
                             halign="right", valign="middle", **self.F)
        self.lbl_sub.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(self.lbl_sub)
        return box

    def _build_panel(self):
        wrap = BoxLayout(size_hint_y=None, padding=(dp(10), dp(6)))
        wrap.bind(minimum_height=wrap.setter("height"))
        panel = BoxLayout(orientation="vertical", size_hint_y=None,
                          padding=(dp(14), dp(14)), spacing=dp(10))
        attach_bg(panel, C_CARD, radius=14)
        panel.bind(minimum_height=panel.setter("height"))
        wrap.add_widget(panel)

        # ---- 音源文件（内置多个，可切换）----
        panel.add_widget(self._section("音源"))
        row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
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
        row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
        self.sp_platform = CNSpinner(text="—", values=[], font_size=dp(14),
                                     **self.F)
        self.sp_platform.bind(text=lambda *_: self._refresh_qualities())
        row.add_widget(self.sp_platform)
        self.sp_quality = CNSpinner(text="320k", values=QUALITY_ORDER,
                                    font_size=dp(14), **self.F)
        row.add_widget(self.sp_quality)
        panel.add_widget(row)

        # ---- 格式 + 数量 ----
        row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(8))
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

        # ---- 搜索 ----
        panel.add_widget(self._section("搜索"))
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.ti_search = TextInput(hint_text="输入歌名或歌手", multiline=False,
                                   font_size=dp(15), padding=(dp(12), dp(12)),
                                   background_color=C_CTRL,
                                   foreground_color=C_TEXT,
                                   hint_text_color=C_FAINT, **self.F)
        self.ti_search.bind(on_text_validate=self.do_search)
        row.add_widget(self.ti_search)
        self.btn_search = self._btn("搜索", color=C_ACCENT, w=dp(76),
                                    bold=True, fs=dp(15))
        self.btn_search.bind(on_release=self.do_search)
        row.add_widget(self.btn_search)
        panel.add_widget(row)
        return wrap

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

    # ---------- 换音源 ----------
    def pick_source(self, *_):
        """从手机里选一个新的音源 .js 文件"""
        if not IS_ANDROID:
            self.set_status("桌面端请直接替换 sources/default.js")
            return
        try:
            from jnius import autoclass
            from android import activity as android_activity

            Intent = autoclass("android.content.Intent")
            act = autoclass("org.kivy.android.PythonActivity").mActivity

            # 只用 p4a 自带的 activity 回调机制：它内部已经注册好了 Java 侧的
            # listener，这里传一个普通 Python 函数即可。
            # 千万不要自己写 PythonJavaClass + __javainterfaces__ =
            # ["org/kivy/android/activity/ActivityResultListener"] ——
            # 那个类名在 APK 的 dex 里根本不存在，会抛
            #   ClassNotFoundException: Didn't find class
            #   "org.kivy.android.activity.ActivityResultListener"
            if not getattr(self, "_picker_bound", False):
                android_activity.bind(on_activity_result=self._on_picked)
                self._picker_bound = True

            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("*/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)

            # createChooser 的第二个参数签名是 CharSequence，
            # 直接传 Python str 会抛:
            #   No static methods called createChooser ...
            #   requested: (Intent, 'str')
            # 必须包成 java.lang.String 再 cast 成 CharSequence。
            launcher = intent
            try:
                from jnius import cast
                JString = autoclass("java.lang.String")
                title = cast("java.lang.CharSequence",
                             JString("选择音源 .js 文件"))
                launcher = Intent.createChooser(intent, title)
            except Exception as e:
                # 拿不到选择器也没关系：系统在没有默认应用时会自己弹选择框
                log_exc("createChooser")
                log("退回直接 startActivityForResult:", e)

            act.startActivityForResult(launcher, 0x1234)
        except Exception as e:
            log_exc("pick_source")
            self.set_status("选择文件失败: %s" % e, C_ERR)

    def _on_picked(self, request_code, result_code, intent):
        """选完文件回调（p4a 的 activity 在 UI 线程派发）"""
        try:
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
    def do_search(self, *_):
        try:
            keyword = self.ti_search.text.strip()
            if not keyword:
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

        self.hint.text = "点击任意一首查看详情 / 播放 / 下载（共 %d 首）" % len(self.songs)
        kw = dict(self.F)
        for i, s in enumerate(self.songs):
            item = FlatButton(
                text="%s\n%s    %s"
                     % (s["name"], s["singer"], s.get("interval") or "--:--"),
                size_hint_y=None, height=dp(58), halign="left",
                valign="middle", font_size=dp(13), color=C_TEXT,
                bg_color=C_ITEM, radius=10, **self.F)   # 少了 **self.F 中文就是方块
            item.bind(size=lambda b, v: setattr(b, "text_size",
                                                (v[0] - dp(24), None)))
            item.bind(on_release=lambda b, idx=i: self.open_song(idx))
            self.results.add_widget(item)
        self.set_status("找到 %d 首，点一首查看详情" % len(self.songs))

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
        """按平台 + 格式偏好刷新品质下拉"""
        try:
            quals = songinfo.filter_qualities(self._platform_qualities(),
                                              self.sp_format.text)
            self.sp_quality.values = quals
            if self.sp_quality.text not in quals and quals:
                self.sp_quality.text = quals[0]
        except Exception:
            log_exc("_refresh_qualities")

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
        want = self.sp_quality.text or "320k"
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
                            title_size=dp(15), separator_color=C_ACCENT)
        self._popup.open()

    # ---------- 播放 ----------
    def play_current(self):
        if not getattr(self, "_cur_url", None):
            self.set_status("还没有解析出可播放的直链")
            return
        if self._popup:
            self._popup.dismiss()
        self.player.play(self._cur_url, on_event=self._on_player_event)
        self.btn_play.text = "暂停"

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
        self.set_status("播放失败: %s" % msg, C_ERR)

    def toggle_play(self):
        try:
            if not self.player.is_active():
                self.play_current()
                return
            self.player.toggle()
            self.btn_play.text = ("暂停" if self.player.state
                              == player_mod.Player.PLAYING else "播放")
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
            if not appenv.using_public_dir() and IS_ANDROID \
                    and not self._asked_permission:
                self._asked_permission = True
                self.ui(lambda: self.set_status(
                    "提示：授权「所有文件访问」后文件会存到 "
                    "Download/落雪音源；不授权也能下，只是存到 App 私有目录"))
                request_all_files_access()
            os.makedirs(out_dir, exist_ok=True)

            name = downloader.safe_name("%s - %s" % (song.get("name", "unknown"),
                                                     song.get("singer", "")))
            dest = os.path.join(out_dir, "%s.%s" % (name, ext))

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
