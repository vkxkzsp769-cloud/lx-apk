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
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput

import appenv
import downloader
import fonts
import netease
from appenv import (IS_ANDROID, SOURCE_FILE, download_dir, ensure_source,
                    log, log_exc, request_all_files_access, save_source)
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
QUALITY_ORDER = ["128k", "192k", "320k", "flac", "flac24bit",
                 "hires", "master"]


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
        self.songs = []           # 搜索结果
        self.platforms = []       # [{source,name,qualitys}]
        self.busy = False
        self._asked_permission = False
        self.F = fonts.font_kwargs()

        root = BoxLayout(orientation="vertical")
        attach_bg(root, C_BG)

        root.add_widget(self._build_header())
        root.add_widget(self._build_panel())
        root.add_widget(self._build_results())
        root.add_widget(self._build_footer())

        Clock.schedule_once(self._guard(self._boot), 0.2)
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
                                     width=dp(104), font_size=dp(14), **self.F)
        row.add_widget(self.sp_platform)
        row.add_widget(self._field("音质"))
        self.sp_quality = CNSpinner(text="320k", values=QUALITY_ORDER,
                                    size_hint_x=None, width=dp(104),
                                    font_size=dp(14), **self.F)
        self.sp_quality.text = "320k"
        row.add_widget(self.sp_quality)
        panel.add_widget(row)

        # 搜索
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
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

    def _field(self, text):
        lb = Label(text=text, size_hint_x=None, width=dp(42), font_size=dp(14),
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
        ensure_source()
        self.set_status("正在启动 JS 引擎…")
        if IS_ANDROID:
            self.bridge.start(on_ready=self._on_engine_ready)
        else:
            # 桌面调试：没有 WebView；用假引擎跑界面（见 test_ui.py）
            self.set_status("桌面模式：仅界面可用", C_DIM)

    def _on_engine_ready(self, ok):
        """由 WebView 流程在后台线程回调"""
        if not ok:
            self.set_status("WebView 引擎不可用，无法加载音源", C_ERR)
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

        meta = info.get("meta") or {}
        self.set_status("✓ 音源: %s v%s · %d 个平台"
                        % (meta.get("name", "?"), meta.get("version", "?"),
                           len(labels)), C_OK)
        if self.hint.text.startswith("搜索后"):
            self.hint.text = "搜索后点结果即可下载"

    # ---------- 换音源 ----------
    def pick_source(self, *_):
        if not IS_ANDROID:
            self.set_status("桌面端请直接替换 sources/default.js")
            return
        try:
            from jnius import autoclass, PythonJavaClass, java_method
            from android import activity as android_activity

            Intent = autoclass("android.content.Intent")
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            app = self

            class _Picker(PythonJavaClass):
                __javainterfaces__ = [
                    "org/kivy/android/activity/ActivityResultListener"]
                __javacontext__ = "app"

                def onActivityResult(self, requestCode, resultCode, data):
                    if resultCode != -1 or data is None:
                        return
                    uri = data.getData()
                    if uri is None:
                        return
                    try:
                        text = app._read_uri(uri)
                        if not text or len(text) < 200:
                            raise RuntimeError("文件内容过短，可能不是音源")
                        save_source(text)
                        app.ui(lambda: app.set_status("音源已保存，正在加载…"))
                        app.load_source(SOURCE_FILE)
                    except Exception as e:
                        msg = str(e)
                        app.ui(lambda: app.set_status("读取音源失败: %s" % msg,
                                                      C_ERR))

            self._picker = _Picker()
            android_activity.bind(on_activity_result=self._picker.onActivityResult)

            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("*/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            act.startActivityForResult(
                Intent.createChooser(intent, "选择音源 .js 文件"), 0x1234)
        except Exception as e:
            log_exc("pick_source")
            self.set_status("选择文件失败: %s" % e, C_ERR)

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

        self.hint.text = "点击任意一首开始下载（共 %d 首）" % len(self.songs)
        kw = dict(self.F)
        for i, s in enumerate(self.songs):
            btn = Button(text="%d. %s — %s  [%s]"
                              % (i + 1, s["name"], s["singer"], s["interval"]),
                         size_hint_y=None, height=dp(46), halign="left",
                         valign="middle", font_size=dp(12),
                         background_normal="", background_color=C_CARD,
                         color=C_TEXT, **kw)
            btn.bind(size=lambda b, v: setattr(b, "text_size",
                                               (v[0] - dp(12), None)))
            btn.bind(on_release=lambda b, idx=i: self.start_download(idx))
            self.results.add_widget(btn)
        self.set_status("找到 %d 首，点一首开始下载" % len(self.songs))

    # ---------- 下载 ----------
    def start_download(self, idx):
        try:
            if idx >= len(self.songs):
                return
            song = self.songs[idx]
            source = self._current_source()
            quality = self.sp_quality.text or "320k"
            self.pb.value = 0
            self.set_status("准备下载: %s" % song["name"])
            self.bg(lambda: self._download_work(song, source, quality),
                    "download")
        except Exception as e:
            log_exc("start_download")
            self.set_status("无法开始下载: %s" % e, C_ERR)

    def _current_source(self):
        """从「平台」下拉里取出平台代号"""
        text = self.sp_platform.text or ""
        for p in self.platforms:
            if "(%s)" % p["source"] in text:
                return p["source"]
        return "wy"

    def _download_work(self, song, source, quality):
        try:
            # 目标目录：公共 Download 优先，不可写则回退应用私有目录
            out_dir = download_dir()
            if not appenv.using_public_dir() and IS_ANDROID \
                    and not self._asked_permission:
                self._asked_permission = True
                self.ui(lambda: self.set_status(
                    "提示：授权「所有文件访问」后文件会存到 "
                    "Download/落雪音源；不授权也能下，只是存到 App 私有目录"))
                request_all_files_access()
            os.makedirs(out_dir, exist_ok=True)

            info = {"id": song["id"], "name": song["name"],
                    "singer": song["singer"], "source": source,
                    "interval": song.get("interval", ""),
                    "meta": {"songId": song["id"],
                             "albumName": song.get("album", "")}}

            # 受限直链会自动降音质重试
            order = [quality] + [q for q in ("320k", "flac", "128k")
                                 if q != quality]
            url, last_err = None, ""
            for q in order[:3]:
                self.ui(lambda q=q: self.set_status("正在取直链 (%s)…" % q))
                got, err = self.bridge.music_url(source, q, info)
                if got and not downloader.is_restricted(got):
                    url, quality = got, q
                    break
                last_err = err or "该音质直链受限"
                log("音质 %s 取直链失败: %s" % (q, last_err))

            if not url:
                self.ui(lambda: self.set_status(
                    "取直链失败: %s（可能版权受限，换一首试试）" % last_err,
                    C_ERR))
                return

            ext = downloader.guess_ext(url, quality)
            name = downloader.safe_name("%s - %s" % (song["name"],
                                                     song["singer"]))
            dest = os.path.join(out_dir, "%s.%s" % (name, ext))

            self.ui(lambda: self.set_status("下载中: %s" % song["name"]))

            def on_progress(got, total):
                self.ui(lambda: self._progress(got, total, song["name"]))

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
