#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
落雪音源下载器 · Android APK
==========================================================
架构说明（为什么要用 WebView 跑 JS）：

落雪/LX 音源是 JavaScript 文件，而 p4a(python-for-android) 的 recipe 里
没有 dukpy/quickjs 等 JS 引擎（无法编译 C 扩展）。
但 Android 系统自带 WebView —— 它就是一个完整的 JS 引擎 + 网络栈。

所以本 App 的做法：
    Python(Kivy) 负责界面/下载
        └─ 通过 pyjnius 创建一个隐藏的 WebView
             └─ 在 WebView 里实现 lx 契约(xhr 版) 并执行音源 .js
                  └─ 音源返回音频直链 → 回传 Python → 下载

音源文件：
    内置一份默认音源在 assets 里，首次启动释放到 App 目录；
    用户也可以从手机文件管理器选新的 .js 音源覆盖，实现换源。
==========================================================
"""

import os
import sys
import json
import time
import threading
import traceback
import urllib.request
import urllib.error

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.utils import platform
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.spinner import Spinner
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar

IS_ANDROID = platform == "android"

# ============================================================
#  路径
# ============================================================
def app_dir():
    """可写目录（音源、下载）"""
    if IS_ANDROID:
        from jnius import autoclass
        ctx = autoclass("org.kivy.android.PythonActivity").mActivity
        base = ctx.getExternalFilesDir(None)
        p = base.getAbsolutePath() if base else ctx.getFilesDir().getAbsolutePath()
    else:
        p = os.path.dirname(os.path.abspath(__file__))
    return p

APP_DIR = app_dir()
SCRIPT_DIR = os.path.join(APP_DIR, "sources")
DOWNLOAD_DIR = os.path.join(APP_DIR, "downloads")
for d in (SCRIPT_DIR, DOWNLOAD_DIR):
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass

DEFAULT_SCRIPT = os.path.join(SCRIPT_DIR, "default.js")


def extract_default_source():
    """首次启动时把内置音源释放出来"""
    if os.path.exists(DEFAULT_SCRIPT) and os.path.getsize(DEFAULT_SCRIPT) > 1000:
        return
    try:
        # 从 APK assets 读取内置音源
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        InputStream = autoclass("java.io.InputStream")
        stream = act.getAssets().open("default_source.js")
        reader = autoclass("java.io.BufferedReader")(
            autoclass("java.io.InputStreamReader")(stream))
        lines = []
        line = reader.readLine()
        while line is not None:
            lines.append(line)
            line = reader.readLine()
        reader.close()
        with open(DEFAULT_SCRIPT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print("释放内置音源失败:", e)


# ============================================================
#  WebView JS 引擎（Android）
# ============================================================
# 注入到 WebView 的宿主代码：实现落雪的 lx 契约，用 XHR 发网络请求。
LX_HOST_JS = r"""
var __HANDLER = null, __INITED = null, __RESULT = null, __LOGS = [];

function __mkRequest() {
  return function lxRequest(url, options, callback) {
    if (typeof options === 'function') { callback = options; options = {}; }
    options = options || {};
    var xhr = new XMLHttpRequest();
    var method = (options.method || ((options.body || options.form) ? 'POST' : 'GET')).toUpperCase();
    var body = null;
    if (options.form) {
      var parts = [];
      for (var k in options.form) parts.push(encodeURIComponent(k)+'='+encodeURIComponent(options.form[k]));
      body = parts.join('&');
    } else if (options.json) { body = JSON.stringify(options.json); }
    else if (options.body != null) { body = String(options.body); }
    try { xhr.open(method, url, true); } catch (e) { return callback(new Error('bad url'), null); }
    var h = options.headers || {};
    for (var hk in h) { try { xhr.setRequestHeader(hk, h[hk]); } catch (e) {} }
    if (body && !h['Content-Type'] && !h['content-type'])
      try { xhr.setRequestHeader('Content-Type','application/x-www-form-urlencoded'); } catch (e) {}
    xhr.timeout = options.timeout || 8000;
    xhr.onreadystatechange = function () {
      if (xhr.readyState === 4)
        callback(null, { statusCode: xhr.status, headers: {}, body: xhr.responseText });
    };
    xhr.ontimeout = function () { callback(new Error('timeout'), null); };
    xhr.onerror = function () { callback(new Error('network'), null); };
    try { xhr.send(body); } catch (e) { callback(e, null); }
  };
}

var EVENT_NAMES = { request:'request', inited:'inited', updateAlert:'updateAlert' };
var lx = {
  EVENT_NAMES: EVENT_NAMES, env:'mobile', version:'2.11.0',
  request: __mkRequest(),
  on: function (n, h) { if (n === EVENT_NAMES.request) __HANDLER = h; },
  send: function (n, p) { if (n === EVENT_NAMES.inited) __INITED = p; },
  utils: {
    buffer: { from: function (s) { return s; } },
    crypto: { md5: function (s) { return s; } }
  }
};
window.lx = lx;

function lxInit(code) {
  __HANDLER = null; __INITED = null; __RESULT = null;
  window.console = { log: function(){}, error: function(){}, warn: function(){},
                     debug: function(){}, info: function(){} };
  try { (new Function(code))(); } catch (e) { return 'ERR:' + e.message; }
  if (!__INITED) return 'ERR:no-inited';
  try { return JSON.stringify(__INITED); } catch (e) { return 'ERR:serialize'; }
}

function lxGetUrl(source, quality, musicInfoJson) {
  __RESULT = { done: false, url: '', error: null };
  if (!__HANDLER) { __RESULT.error = 'no-handler'; __RESULT.done = true; return 'ERR'; }
  try {
    var mi = JSON.parse(musicInfoJson);
    var p = __HANDLER({ source: source, action: 'musicUrl',
                        info: { type: quality, musicInfo: mi } });
    if (p && typeof p.then === 'function') {
      p.then(function (u) {
        __RESULT.url = (typeof u === 'string') ? u : ((u && u.url) || '');
        __RESULT.done = true;
      }, function (e) {
        __RESULT.error = String((e && e.message) || e); __RESULT.done = true;
      });
    } else {
      __RESULT.url = (typeof p === 'string') ? p : ''; __RESULT.done = true;
    }
  } catch (e) { __RESULT.error = String(e.message || e); __RESULT.done = true; }
  return 'STARTED';
}

function lxPoll() { return JSON.stringify(__RESULT || {done:false,url:'',error:null}); }
"""


class WebViewEngine:
    """在 Android WebView 里跑音源 JS"""

    def __init__(self):
        self.wv = None
        self.ready = False
        self._result_box = {}
        self._lock = threading.Lock()

    def start(self, on_ready=None):
        """在 UI 线程创建隐藏 WebView"""
        if not IS_ANDROID:
            self.ready = False
            return
        try:
            from jnius import autoclass, PythonJavaClass, java_method
            from android.runnable import run_on_ui_thread

            WebView = autoclass("android.webkit.WebView")
            WebViewClient = autoclass("android.webkit.WebViewClient")
            activity = autoclass("org.kivy.android.PythonActivity").mActivity

            @run_on_ui_thread
            def _create():
                try:
                    wv = WebView(activity)
                    ws = wv.getSettings()
                    ws.setJavaScriptEnabled(True)
                    ws.setDomStorageEnabled(True)
                    # 允许 http 明文（很多音源 API 是 http）
                    try:
                        ws.setMixedContentMode(0)
                    except Exception:
                        pass
                    wv.setWebViewClient(WebViewClient())
                    wv.loadDataWithBaseURL("https://localhost/",
                                           "<html><body></body></html>",
                                           "text/html", "utf-8", None)

                    engine = self

                    class _Callback(PythonJavaClass):
                        __javainterfaces__ = ["android/webkit/ValueCallback"]
                        __javacontext__ = "app"

                        @java_method("(Ljava/lang/Object;)V")
                        def onReceiveValue(self, value):
                            pass

                    self._cb = _Callback()
                    self.wv = wv

                    # 等页面加载完再注入宿主
                    def _inject(dt):
                        try:
                            self.wv.evaluateJavascript(LX_HOST_JS, self._cb)
                            self.ready = True
                            if on_ready:
                                on_ready(True)
                        except Exception as e:
                            print("注入宿主失败:", e)
                            if on_ready:
                                on_ready(False)

                    Clock.schedule_once(_inject, 1.2)
                except Exception as e:
                    print("创建 WebView 失败:", e)
                    if on_ready:
                        on_ready(False)

            _create()
        except Exception as e:
            print("WebView 不可用:", e)
            self.ready = False

    def _eval_sync(self, js, timeout=3.0):
        """执行 JS 并把结果同步取回（evaluateJavascript 是异步的，用回调收集）"""
        if not self.wv:
            return None
        box = {}
        done = threading.Event()

        from jnius import autoclass, PythonJavaClass, java_method

        class _CB(PythonJavaClass):
            __javainterfaces__ = ["android/webkit/ValueCallback"]
            __javacontext__ = "app"

            @java_method("(Ljava/lang/Object;)V")
            def onReceiveValue(self, value):
                box["v"] = value
                done.set()

        cb = _CB()
        Clock.schedule_once(lambda dt: self.wv.evaluateJavascript(js, cb), 0)
        done.wait(timeout)
        return box.get("v")

    def load_source(self, code):
        """加载音源，返回 (ok, info_or_error)"""
        # 回退路径：WebView 不可用时用纯 Python 引擎（需 dukpy）
        if not self.ready:
            return self._load_via_python(code)
        escaped = json.dumps(code)
        raw = self._eval_sync("lxInit(%s)" % escaped, timeout=15)
        if raw is None:
            return False, "无响应"
        s = str(raw)
        # evaluateJavascript 返回的是 JSON 编码的字符串
        try:
            s = json.loads(s)
        except Exception:
            pass
        if s.startswith("ERR"):
            return False, s[4:]
        try:
            return True, json.loads(s)
        except Exception:
            return False, "解析失败"

    def get_url(self, source, quality, music_info, timeout=60):
        """取直链"""
        if not self.ready:
            return self._get_url_via_python(source, quality, music_info)

    # ---------- 纯 Python 引擎回退（桌面调试 / WebView 异常时） ----------
    def _load_via_python(self, code):
        try:
            import lx_engine, tempfile
            tmp = os.path.join(SCRIPT_DIR, "_active.js")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(code)
            self._py_engine = lx_engine.LxEngine(tmp)
            meta, srcs = self._py_engine.info()
            return True, {"meta": meta,
                          "sources": {x["source"]: {"name": x["name"],
                                                    "qualitys": x["qualitys"]}
                                      for x in srcs}}
        except Exception as e:
            return False, "无可用 JS 引擎: %s" % e

    def _get_url_via_python(self, source, quality, music_info):
        eng = getattr(self, "_py_engine", None)
        if eng is None:
            return None, "无可用 JS 引擎"
        try:
            return eng.music_url(source, quality, music_info), None
        except Exception as e:
            return None, str(e)
        args = "%s, %s, %s" % (json.dumps(source), json.dumps(quality),
                               json.dumps(json.dumps(music_info, ensure_ascii=False)))
        self._eval_sync("lxGetUrl(%s)" % args, timeout=5)

        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self._eval_sync("lxPoll()", timeout=3)
            if raw is None:
                time.sleep(0.25); continue
            try:
                s = json.loads(raw)
                if isinstance(s, str):
                    s = json.loads(s)
            except Exception:
                time.sleep(0.25); continue
            if s.get("done"):
                if s.get("error"):
                    return None, s["error"]
                return s.get("url") or None, None
            time.sleep(0.3)
        return None, "超时"


# ============================================================
#  搜索 / 下载（Python 侧）
# ============================================================
def http_json(url, headers=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def search_netease(kw, limit=15):
    import urllib.parse
    url = ("https://music.163.com/api/search/get/web?s=%s&type=1&offset=0&limit=%d"
           % (urllib.parse.quote(kw), limit))
    d = http_json(url, {"Referer": "https://music.163.com/", "Cookie": "appver=8.9.70;"})
    out = []
    for s in ((d.get("result") or {}).get("songs") or []):
        dur = int((s.get("duration") or 0) // 1000)
        out.append({
            "id": str(s.get("id")),
            "name": s.get("name", ""),
            "singer": "、".join(a.get("name", "") for a in (s.get("artists") or [])),
            "album": (s.get("album") or {}).get("name", ""),
            "interval": "%02d:%02d" % (dur // 60, dur % 60),
            "platform": "wy",
        })
    return out


def download_file(url, dest, referer=None, on_progress=None):
    h = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
         "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0)
        ctype = (r.headers.get("Content-Type") or "").lower()
        if total and total < 4096 and ("html" in ctype or "json" in ctype):
            raise RuntimeError("直链已失效（返回网页而非音频）")
        got = 0
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_progress and total:
                    on_progress(got, total)
    if got < 10240:
        os.remove(tmp)
        raise RuntimeError("文件过小，直链无效")
    with open(tmp, "rb") as f:
        magic = f.read(4)
    if magic[:4] == b"<htm" or magic[:1] == b"{":
        os.remove(tmp)
        raise RuntimeError("下载到的是网页，直链失效")
    if os.path.exists(dest):
        os.remove(dest)
    os.rename(tmp, dest)
    return got



# 受限直链特征（网易云版权受限时会返回这种跳转地址，下载得到的是网页）
RESTRICTED_PATTERNS = ("music.163.com/song/media/outer/url", "/404", "404.mp3")


def is_restricted(url):
    if not url:
        return True
    low = url.lower()
    return any(p.lower() in low for p in RESTRICTED_PATTERNS)

def guess_ext(url, quality):
    import re
    m = re.search(r"\.(mp3|flac|m4a|ape|wav)(?:\?|$)", url, re.I)
    if m:
        return m.group(1).lower()
    return {"flac": "flac", "flac24bit": "flac", "hires": "flac", "master": "flac"}.get(quality, "mp3")


def safe_name(s):
    import re
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s)
    return (re.sub(r"\s+", " ", s).strip(" .") or "unknown")[:120]


# ============================================================
#  界面
# ============================================================
PLATFORM_LABEL = {"wy": "网易云", "tx": "QQ音乐", "kw": "酷我", "kg": "酷狗", "mg": "咪咕"}
QUALITY_ORDER = ["128k", "192k", "320k", "flac", "flac24bit", "hires", "master"]


class DownloaderApp(App):
    title = "落雪音源下载器"

    def build(self):
        self.engine = WebViewEngine()
        self.sources = []
        self.song_list = []

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))

        # ---- 标题 ----
        self.status = Label(text="正在启动 JS 引擎...", size_hint_y=None, height=dp(30),
                            font_size=dp(13))
        root.add_widget(self.status)

        # ---- 音源行 ----
        row1 = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        self.sp_source = Spinner(text="音源: 加载中", values=[], size_hint_x=0.7)
        btn_pick = Button(text="换音源", size_hint_x=0.3)
        btn_pick.bind(on_release=self.pick_source_file)
        row1.add_widget(self.sp_source)
        row1.add_widget(btn_pick)
        root.add_widget(row1)

        # ---- 平台/音质 ----
        row2 = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        self.sp_platform = Spinner(text="网易云", values=["网易云"], size_hint_x=0.5)
        self.sp_quality = Spinner(text="320k", values=QUALITY_ORDER, size_hint_x=0.5)
        row2.add_widget(self.sp_platform)
        row2.add_widget(self.sp_quality)
        root.add_widget(row2)

        # ---- 搜索框 ----
        row3 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.ti_search = TextInput(hint_text="输入歌名或歌手，例如：海阔天空",
                                   multiline=False, size_hint_x=0.75)
        btn_search = Button(text="搜索", size_hint_x=0.25)
        btn_search.bind(on_release=self.do_search)
        self.ti_search.bind(on_text_validate=self.do_search)
        row3.add_widget(self.ti_search)
        row3.add_widget(btn_search)
        root.add_widget(row3)

        # ---- 结果列表 ----
        self.sv = ScrollView()
        self.results_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                     spacing=dp(4))
        self.results_box.bind(minimum_height=self.results_box.setter("height"))
        self.sv.add_widget(self.results_box)
        root.add_widget(self.sv)

        # ---- 进度 ----
        self.pb = ProgressBar(max=100, size_hint_y=None, height=dp(14))
        root.add_widget(self.pb)
        self.log = Label(text="", size_hint_y=None, height=dp(38), font_size=dp(11))
        root.add_widget(self.log)

        Clock.schedule_once(self._boot, 0.3)
        return root

    # ---------- 启动 ----------
    def _boot(self, dt):
        extract_default_source()
        if IS_ANDROID:
            self.engine.start(on_ready=self._on_engine_ready)
        else:
            # 桌面调试：用内置的 dukpy 引擎
            self._on_engine_ready(True)

    def _on_engine_ready(self, ok):
        if not ok:
            self.set_status("JS 引擎启动失败")
            return
        self.set_status("引擎就绪，加载音源...")
        self.load_source(DEFAULT_SCRIPT)

    def load_source(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                code = f.read()
        except Exception as e:
            self.set_status("读取音源失败: %s" % e)
            return
        ok, info = self.engine.load_source(code)
        if not ok:
            self.set_status("音源加载失败: %s" % info)
            return
        self.sources = []
        labels = []
        for key, v in (info.get("sources") or {}).items():
            self.sources.append({"source": key, "name": v.get("name", key),
                                 "qualitys": v.get("qualitys") or []})
            labels.append("%s (%s)" % (PLATFORM_LABEL.get(key, key), key))
        self.sp_source.values = labels
        if labels:
            self.sp_source.text = labels[0]
        meta = info.get("meta") or {}
        self.set_status("✓ 音源: %s v%s · %d 个平台"
                        % (meta.get("name", "?"), meta.get("version", "?"), len(labels)))

    def pick_source_file(self, *_):
        """从手机里选新的音源 .js 文件"""
        from kivy.utils import platform as pf
        if pf != "android":
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
                        code = app._read_uri(uri)
                        if not code or len(code) < 200:
                            raise RuntimeError("文件内容过短，可能不是音源")
                        with open(DEFAULT_SCRIPT, "w", encoding="utf-8") as f:
                            f.write(code)
                        Clock.schedule_once(
                            lambda dt: app.load_source(DEFAULT_SCRIPT), 0)
                    except Exception as e:
                        msg = str(e)
                        Clock.schedule_once(
                            lambda dt: app.set_status("读取音源失败: %s" % msg), 0)

            self._picker = _Picker()
            android_activity.bind(on_activity_result=self._picker.onActivityResult)

            intent = Intent(Intent.ACTION_GET_CONTENT)
            intent.setType("*/*")
            intent.addCategory(Intent.CATEGORY_OPENABLE)
            act.startActivityForResult(
                Intent.createChooser(intent, "选择音源 .js 文件"), 0x1234)
        except Exception as e:
            self.set_status("选择文件失败: %s" % e)

    def _read_uri(self, uri):
        """通过 ContentResolver 读取 content:// URI（Android 选文件返回的就是它）"""
        from jnius import autoclass, cast
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        resolver = act.getContentResolver()
        stream = resolver.openInputStream(uri)
        BufferedReader = autoclass("java.io.BufferedReader")
        InputStreamReader = autoclass("java.io.InputStreamReader")
        reader = BufferedReader(InputStreamReader(stream))
        sb = autoclass("java.lang.StringBuilder")()
        line = reader.readLine()
        while line is not None:
            sb.append(line).append("\n")
            line = reader.readLine()
        reader.close()
        return str(sb.toString())

    # ---------- 搜索 ----------
    def do_search(self, *_):
        kw = self.ti_search.text.strip()
        if not kw:
            return
        self.set_status("搜索中: %s" % kw)
        self.clear_results()
        threading.Thread(target=self._search_worker, args=(kw,), daemon=True).start()

    def _search_worker(self, kw):
        try:
            songs = search_netease(kw, 15)
        except Exception as e:
            Clock.schedule_once(lambda dt: self.set_status("搜索失败: %s" % e), 0)
            return
        Clock.schedule_once(lambda dt: self._show_results(songs), 0)

    def clear_results(self):
        self.results_box.clear_widgets()

    def _show_results(self, songs):
        self.song_list = songs
        self.clear_results()
        if not songs:
            self.set_status("没有找到结果")
            return
        for i, s in enumerate(songs):
            btn = Button(text="%d. %s — %s  [%s]" % (i + 1, s["name"], s["singer"], s["interval"]),
                         size_hint_y=None, height=dp(46), halign="left", valign="middle",
                         font_size=dp(12))
            btn.bind(on_release=lambda b, idx=i: self.start_download(idx))
            self.results_box.add_widget(btn)
        self.set_status("找到 %d 首，点一首开始下载" % len(songs))

    # ---------- 下载 ----------
    def start_download(self, idx):
        if idx >= len(self.song_list):
            return
        song = self.song_list[idx]
        quality = self.sp_quality.text
        plat = "wy"
        if self.sources:
            sel = self.sp_source.text
            for s in self.sources:
                if ("(%s)" % s["source"]) in sel:
                    plat = s["source"]
                    break
        self.set_status("请求直链: %s ..." % song["name"])
        self.pb.value = 0
        threading.Thread(target=self._dl_worker, args=(song, plat, quality),
                         daemon=True).start()

    def _dl_worker(self, song, plat, quality):
        try:
            info = {"id": song["id"], "name": song["name"], "singer": song["singer"],
                    "source": plat, "meta": {"songId": song["id"]}}
            # 音源有时返回受限直链，按音质降级重试
            qorder = [quality] + [q for q in ("320k", "flac", "128k")
                                  if q != quality]
            url, err = None, ""
            for qi, q in enumerate(qorder[:3]):
                Clock.schedule_once(lambda dt, qq=q: self.set_status(
                    "向音源请求直链 (%s)..." % qq), 0)
                u, e = self.engine.get_url(plat, q, info)
                if u and not is_restricted(u):
                    url, quality = u, q
                    break
                err = e or "直链受限"
                if qi < 2:
                    Clock.schedule_once(lambda dt: self.set_status(
                        "直链受限，降级重试..."), 0)
            if url is None:
                Clock.schedule_once(lambda dt: self.set_status(
                    "取直链失败: %s（该歌曲可能版权受限）" % err), 0)
                return
            ext = guess_ext(url, quality)
            base = safe_name("%s - %s" % (song["name"], song["singer"]))
            dest = os.path.join(DOWNLOAD_DIR, "%s.%s" % (base, ext))

            def prog(got, total):
                Clock.schedule_once(
                    lambda dt: self._update_prog(got, total, song["name"]), 0)

            Clock.schedule_once(lambda dt: self.set_status("下载中: %s" % song["name"]), 0)
            size = download_file(url, dest, on_progress=prog)
            Clock.schedule_once(
                lambda dt: self._done(dest, size), 0)
        except Exception as e:
            msg = str(e)
            Clock.schedule_once(lambda dt: self.set_status("下载失败: %s" % msg), 0)

    def _update_prog(self, got, total, name):
        self.pb.value = got * 100.0 / max(total, 1)
        self.set_status("下载中 %s: %.1f%% (%.1f/%.1f MB)"
                        % (name, self.pb.value, got / 1048576, total / 1048576))

    def _done(self, path, size):
        self.pb.value = 100
        self.set_status("✓ 完成: %s (%.1f MB)\n保存于 %s"
                        % (os.path.basename(path), size / 1048576, DOWNLOAD_DIR))

    def set_status(self, txt):
        self.status.text = txt


if __name__ == "__main__":
    DownloaderApp().run()
