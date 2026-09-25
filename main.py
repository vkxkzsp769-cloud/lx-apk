"""落雪音源下载器 —— App 入口（界面 + 编排）

模块划分
--------
  appenv      运行环境：日志、路径、存储权限、内置音源
  fonts       中文字体注册
  lxbridge    WebView JS 引擎（跑落雪音源 .js，取音频直链）
  netease     按歌名搜索（音源本身不含搜索）
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
import netease
import player as player_mod
import songinfo
from appenv import (IS_ANDROID, SOURCE_FILE, diag, download_dir,
                    ensure_source, log, log_exc, request_all_files_access,
                    save_source)
from lxbridge import LxBridge

# ============================================================
#  主题
# ============================================================
C_BG = (0.10, 0.12, 0.16, 1)
C_HEADER = (0.13, 0.15, 0.20, 1)
C_CARD = (0.17, 0.19, 0.25, 1)
C_CTRL = (0.24, 0.27, 0.34, 1)
C_ACCENT = (0.26, 0.55, 0.96, 1)
C_TEXT = (0.93, 0.95, 0.98, 1)
C_DIM = (0.62, 0.67, 0.75, 1)
C_OK = (0.33, 0.80, 0.47, 1)
C_ERR = (0.96, 0.45, 0.45, 1)

PLATFORM_LABEL = {"wy": "网易云", "tx": "QQ音乐", "kw": "酷我",
                  "kg": "酷狗", "mg": "咪咕"}
# 顺序即下拉顺序，第一项是 Spinner 的默认值 —— 所以 320k 放最前
QUALITY_ORDER = ["320k", "128k", "192k", "flac", "flac24bit",
                 "hires", "master"]
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
    """Spinner 的下拉列表项不会继承 font_name，中文会显示成方块。
    这里在创建下拉后统一下发字体。

    注意：不要重新声明 font_name！
    Label 自己就有 font_name（默认 'Roboto'），重新声明成
    StringProperty(None) 会把默认值覆盖成 None，
    于是 Kivy 的 resolve_font_name() 拿到 None 后崩：
      AttributeError: 'NoneType' object has no attribute 'endswith'
    这个崩只在「没找到中文字体」时触发（那时不会传 font_name），
    很容易漏测。
    """

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


def attach_bg(widget, color):
    """给控件加纯色背景。

    Kivy 没有 canvas_before 这个属性（它是 widget.canvas.before 对象），
    当构造参数传会抛 TypeError 导致启动即崩 —— 必须建好后再加图元。
    """
    from kivy.graphics import Color, Rectangle
    with widget.canvas.before:
        Color(*color)
        rect = Rectangle(pos=widget.pos, size=widget.size)

    def _sync(w, *_):
        rect.pos = w.pos
        rect.size = w.size

    widget.bind(pos=_sync, size=_sync)
    return widget


# ============================================================
#  主界面
# ============================================================
class LxApp(App):
    title = "落雪音源下载器"

    # ---------- 生命周期 ----------
    def build(self):
        self.bridge = LxBridge()
        self.player = player_mod.Player(
            os.path.join(appenv.APP_DIR, "cache"))
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
        box = BoxLayout(size_hint_y=None, height=dp(52),
                        padding=(dp(14), dp(8)))
        attach_bg(box, C_HEADER)
        t = Label(text="落雪音源下载器", bold=True, font_size=dp(18),
                  halign="left", valign="middle", color=C_TEXT, **self.F)
        t.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(t)
        return box

    def _build_panel(self):
        panel = BoxLayout(orientation="vertical", size_hint_y=None,
                          padding=(dp(14), dp(12)), spacing=dp(10))
        attach_bg(panel, C_CARD)
        panel.bind(minimum_height=panel.setter("height"))

        # 音源
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        row.add_widget(self._field("音源"))
        self.sp_source = CNSpinner(text="加载中…", values=[], size_hint_x=None,
                                   width=dp(148), font_size=dp(14), **self.F)
        row.add_widget(self.sp_source)
        btn = Button(text="更换", size_hint_x=None, width=dp(74),
                     font_size=dp(14), background_normal="",
                     background_color=C_ACCENT, color=(1, 1, 1, 1), **self.F)
        btn.bind(on_release=self.pick_source)
        row.add_widget(btn)
        panel.add_widget(row)

        # 平台 + 音质
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        row.add_widget(self._field("平台"))
        self.sp_platform = CNSpinner(text="—", values=[], size_hint_x=None,
                                     width=dp(100), font_size=dp(14), **self.F)
        self.sp_platform.bind(text=lambda *_: self._refresh_qualities())
        row.add_widget(self.sp_platform)
        row.add_widget(self._field("品质"))
        self.sp_quality = CNSpinner(text="320k", values=QUALITY_ORDER,
                                    size_hint_x=None, width=dp(100),
                                    font_size=dp(14), **self.F)
        row.add_widget(self.sp_quality)
        panel.add_widget(row)

        # 格式 + 搜索
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        row.add_widget(self._field("格式", w=dp(40)))
        self.sp_format = CNSpinner(text="自动", values=songinfo.FORMAT_ORDER,
                                   size_hint_x=None, width=dp(92),
                                   font_size=dp(14), **self.F)
        self.sp_format.bind(text=lambda *_: self._refresh_qualities())
        row.add_widget(self.sp_format)
        self.ti_search = TextInput(hint_text="输入歌名或歌手", multiline=False,
                                   font_size=dp(15), padding=(dp(10), dp(12)),
                                   background_color=C_CTRL,
                                   foreground_color=C_TEXT,
                                   hint_text_color=(0.55, 0.6, 0.68, 1),
                                   **self.F)
        self.ti_search.bind(on_text_validate=self.do_search)
        row.add_widget(self.ti_search)
        self.btn_search = Button(text="搜索", size_hint_x=None, width=dp(78),
                                 font_size=dp(15), bold=True,
                                 background_normal="", background_color=C_ACCENT,
                                 color=(1, 1, 1, 1), **self.F)
        self.btn_search.bind(on_release=self.do_search)
        row.add_widget(self.btn_search)
        panel.add_widget(row)
        return panel

    def _field(self, text, w=None):
        lb = Label(text=text, size_hint_x=None, width=w or dp(42),
                   font_size=dp(14),
                   color=C_DIM, halign="left", valign="middle", **self.F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        return lb

    def _build_results(self):
        wrap = BoxLayout(orientation="vertical", padding=(dp(8), dp(4)))
        self.hint = Label(text="搜索后点结果即可下载", size_hint_y=None,
                          height=dp(28), font_size=dp(13), color=C_DIM,
                          **self.F)
        wrap.add_widget(self.hint)
        self.sv = ScrollView(bar_width=dp(3))
        self.results = BoxLayout(orientation="vertical", size_hint_y=None,
                                 spacing=dp(6))
        self.results.bind(minimum_height=self.results.setter("height"))
        self.sv.add_widget(self.results)
        wrap.add_widget(self.sv)
        return wrap

    def _build_footer(self):
        box = BoxLayout(orientation="vertical", size_hint_y=None,
                        padding=(dp(14), dp(8)), spacing=dp(6))
        attach_bg(box, C_HEADER)

        # ---- 播放条 ----
        prow = BoxLayout(size_hint_y=None, height=dp(38), spacing=dp(8))
        self.btn_play = Button(text="▶", size_hint_x=None, width=dp(42),
                               font_size=dp(16), background_normal="",
                               background_color=C_CARD, color=C_TEXT, **self.F)
        self.btn_play.bind(on_release=lambda *_: self.toggle_play())
        prow.add_widget(self.btn_play)

        self.slider = Slider(min=0, max=1000, value=0, step=1,
                             cursor_size=(dp(16), dp(16)))
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
        self.pb = ProgressBar(max=100, size_hint_y=None, height=dp(6))
        box.add_widget(self.pb)
        # 名字必须是 self.status —— set_status() 写的就是它
        self.status = Label(text="正在启动…", size_hint_y=None, height=dp(34),
                            font_size=dp(12), color=C_DIM,
                            halign="left", valign="middle", **self.F)
        self.status.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        box.add_widget(self.status)
        return box

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
        ensure_source()
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
        self.load_source(SOURCE_FILE)

    def load_source(self, path):
        """加载音源（后台线程）。LxBridge 的 JS 调用不能在主线程做。"""
        def _work():
            self.ui(lambda: self.set_status("正在读取音源…"))
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    code = f.read()
            except Exception as e:
                self.ui(lambda: self.set_status("读取音源失败: %s" % e, C_ERR))
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

        self.sp_source.values = labels
        if labels:
            self.sp_source.text = labels[0]
        self.sp_platform.values = labels
        if labels:
            self.sp_platform.text = labels[0]
        self._refresh_qualities()

        meta = info.get("meta") or {}
        self.set_status("✓ 音源: %s v%s · %d 个平台"
                        % (meta.get("name", "?"), meta.get("version", "?"),
                           len(labels)), C_OK)
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
            self.bg(lambda: self._search_work(keyword), "search")
        except Exception as e:
            self.busy = False
            log_exc("do_search")
            self.set_status("搜索出错: %s" % e, C_ERR)

    def _search_work(self, keyword):
        songs, err = [], None
        try:
            songs = netease.search(keyword, 15)
        except Exception as e:
            err = str(e)
            log_exc("search")
        self.ui(lambda: self._after_search(songs, err))

    def _after_search(self, songs, err):
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
            btn = Button(text="%d. %s\n     %s   [%s]"
                              % (i + 1, s["name"], s["singer"],
                                 s.get("interval") or "--:--"),
                         size_hint_y=None, height=dp(56), halign="left",
                         valign="middle", font_size=dp(12),
                         background_normal="", background_color=C_CARD,
                         color=C_TEXT, **kw)
            btn.bind(size=lambda b, v: setattr(b, "text_size",
                                               (v[0] - dp(12), None)))
            btn.bind(on_release=lambda b, idx=i: self.open_song(idx))
            self.results.add_widget(btn)
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

    def _resolve_song(self, song):
        """后台：取直链（受限自动降品质）+ 探测格式/大小"""
        source = self._current_source()
        want = self.sp_quality.text or "320k"
        info = {"id": song["id"], "name": song["name"],
                "singer": song["singer"], "source": source,
                "interval": song.get("interval", ""),
                "meta": {"songId": song["id"],
                         "albumName": song.get("album", "")}}

        order = [want] + [q for q in ("320k", "flac", "128k") if q != want]
        url, used, err = None, want, ""
        for q in order[:3]:
            got, e = self.bridge.music_url(source, q, info)
            if got and not downloader.is_restricted(got):
                url, used = got, q
                break
            err = e or "该品质直链受限"
            log("品质 %s 解析失败: %s" % (q, err))

        if not url:
            msg = err
            self.ui(lambda: self._song_failed(msg))
            return

        meta = songinfo.probe(url)
        self.ui(lambda: self._song_ready(song, url, used, meta))

    def _song_failed(self, err):
        if getattr(self, "_pop_info", None) is not None:
            self._pop_info.text = "解析失败: %s\n（可能版权受限，换一首试试）" % err
        self.set_status("解析失败: %s" % err, C_ERR)

    def _song_ready(self, song, url, quality, meta):
        self._cur_url = url
        self._cur_ext = meta.get("format") or songinfo.format_of(quality)
        self._cur_dur = 0.0
        try:
            parts = song.get("interval", "").split(":")
            if len(parts) == 2:
                self._cur_dur = int(parts[0]) * 60 + int(parts[1])
        except Exception:
            pass

        if getattr(self, "_pop_info", None) is not None:
            self._pop_info.text = (
                "歌手: %s\n时长: %s\n品质: %s\n格式: %s\n大小: %s"
                % (song["singer"], song.get("interval") or "--:--",
                   songinfo.quality_label(quality),
                   (self._cur_ext or "?").upper(),
                   songinfo.human_size(meta.get("size"))))
        if getattr(self, "_pop_play", None) is not None:
            self._pop_play.disabled = False
            self._pop_dl.disabled = False
        self.set_status("✓ 已解析: %s（%s / %s / %s）"
                        % (song["name"], songinfo.quality_label(quality),
                           (self._cur_ext or "?").upper(),
                           songinfo.human_size(meta.get("size"))), C_OK)

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
        self._pop_play = Button(text="▶ 播放", font_size=dp(15),
                                background_normal="", background_color=C_ACCENT,
                                color=(1, 1, 1, 1), disabled=True, **kw)
        self._pop_play.bind(on_release=lambda *_: self.play_current())
        btns.add_widget(self._pop_play)

        self._pop_dl = Button(text="⬇ 下载", font_size=dp(15),
                              background_normal="", background_color=C_CARD,
                              color=C_TEXT, disabled=True, **kw)
        self._pop_dl.bind(on_release=lambda *_: self.download_current())
        btns.add_widget(self._pop_dl)
        content.add_widget(btns)

        self._popup = Popup(title="歌曲信息", content=content,
                            size_hint=(0.92, None), height=dp(300),
                            title_size=dp(15))
        self._popup.open()

    # ---------- 播放 ----------
    def play_current(self):
        if not getattr(self, "_cur_url", None):
            self.set_status("还没有解析出可播放的直链")
            return
        if self._popup:
            self._popup.dismiss()
        self.player.play(self._cur_url, on_event=self._on_player_event,
                         ext=self._cur_ext or "mp3",
                         fallback_len=self._cur_dur)
        self.btn_play.text = "⏸"

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
        self.btn_play.text = "⏸"
        self.set_status("▶ 正在播放: %s" % (self._cur_song or {}).get("name", ""),
                        C_OK)

    def _player_error(self, msg):
        self.btn_play.text = "▶"
        self.set_status("播放失败: %s" % msg, C_ERR)

    def toggle_play(self):
        try:
            if not self.player.is_active():
                self.play_current()
                return
            self.player.toggle()
            self.btn_play.text = ("⏸" if self.player.state
                                  == player_mod.Player.PLAYING else "▶")
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
        self.btn_play.text = "▶"
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
        self.bg(lambda: self._download_url(song, url, ext), "download")

    def _download_url(self, song, url, ext):
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

            size = downloader.download(url, dest, on_progress=on_progress)
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
        self.set_status("✓ 完成: %s (%.1f MB)\n保存于 %s"
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
