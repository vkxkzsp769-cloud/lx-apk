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
#  中文字体
# ============================================================
# Kivy 默认字体是 Roboto，不含中文字形 → 中文会显示成空方块（口口口）。
# 解决：注册 Android 系统自带的中文字体。
CN_FONT_NAME = "CNFont"
CN_FONT_PATH = []   # 实际选中的字体路径（排查用）


def register_cn_font():
    """注册中文字体，返回可用于 font_name 的名字；失败返回 None"""
    candidates = []
    if IS_ANDROID:
        # 顺序很重要：先 .otf/.ttf（纯简体字形），
        # .ttc 是字体集合，face0 通常是日文变体，中文会显示成日文字形
        candidates = [
            "/system/fonts/NotoSansSC-Regular.otf",
            "/system/fonts/NotoSansHans-Regular.otf",
            "/system/fonts/DroidSansFallbackFull.ttf",
            "/system/fonts/DroidSansFallback.ttf",
            "/system/fonts/MiSans-Regular.ttf",
            "/system/fonts/MiSans-Normal.ttf",
            "/system/fonts/HarmonyOS_Sans_SC_Regular.ttf",
            "/system/fonts/HarmonyOS_SansSC_Regular.ttf",
            "/system/fonts/NotoSansCJK-Regular.ttc",   # 最后才用 .ttc
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    # 再兜底：扫一遍字体目录找 CJK/中文相关的
    try:
        for d in ("/system/fonts", "/system/font"):
            if os.path.isdir(d):
                hits = []
                for fn in sorted(os.listdir(d)):
                    low = fn.lower()
                    if any(k in low for k in ("notosanssc", "notosanshans",
                                              "droidsansfallback", "misans",
                                              "harmony", "sourcehansans")):
                        hits.append(os.path.join(d, fn))
                # .otf/.ttf 排前面，.ttc 排后面
                hits.sort(key=lambda x: x.lower().endswith(".ttc"))
                candidates.extend(hits)
    except Exception:
        pass

    try:
        from kivy.core.text import LabelBase
    except Exception:
        return None

    for path in candidates:
        try:
            if path and os.path.exists(path) and os.path.getsize(path) > 10000:
                LabelBase.register(name=CN_FONT_NAME, fn_regular=path)
                print("已注册中文字体:", path)
                CN_FONT_PATH.append(path)
                return CN_FONT_NAME
        except Exception as e:
            print("注册字体失败", path, e)
    print("警告: 未找到中文字体，中文可能显示为方块")
    return None


# ============================================================
#  路径
# ============================================================
def app_dir():
    """应用私有可写目录（音源、崩溃日志放这里，卸载即清）"""
    if IS_ANDROID:
        from jnius import autoclass
        ctx = autoclass("org.kivy.android.PythonActivity").mActivity
        base = ctx.getExternalFilesDir(None)
        p = base.getAbsolutePath() if base else ctx.getFilesDir().getAbsolutePath()
    else:
        p = os.path.dirname(os.path.abspath(__file__))
    return p


def request_storage_permission():
    """Android 11+ 写公共 Downloads 需要 MANAGE_EXTERNAL_STORAGE（所有文件访问）。
    这里申请一下；用户拒绝也不影响下载（会退回私有目录）。"""
    if not IS_ANDROID:
        return
    try:
        from jnius import autoclass
        from android.permissions import request_permissions, Permission
        Build = autoclass("android.os.Build$VERSION")
        if Build.SDK_INT >= 30:
            # Android 11+：跳系统「所有文件访问」设置页
            try:
                Environment = autoclass("android.os.Environment")
                if Environment.isExternalStorageManager():
                    print("已有所有文件访问权限")
                    return
                Intent = autoclass("android.content.Intent")
                Settings = autoclass("android.provider.Settings")
                Uri = autoclass("android.net.Uri")
                act = autoclass("org.kivy.android.PythonActivity").mActivity
                intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
                intent.setData(Uri.parse("package:" + act.getPackageName()))
                act.startActivity(intent)
                print("已跳转申请所有文件访问权限")
            except Exception as e:
                print("申请所有文件访问失败:", e)
        else:
            request_permissions([Permission.WRITE_EXTERNAL_STORAGE,
                                 Permission.READ_EXTERNAL_STORAGE])
    except Exception as e:
        print("请求存储权限失败:", e)


def _public_download_dir():
    """公共同步下载目录，让文件在「文件管理 → Downloads」里能直接看到。

    优先用 Android 官方 API 取（Environment.getExternalStoragePublicDirectory
    或 MediaStore.Downloads），拿不到再退回硬编码路径。
    """
    if not IS_ANDROID:
        p = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(p, exist_ok=True)
        except OSError:
            pass
        return p

    # 1) 先试 MediaStore.Downloads（API 29+，最标准的做法）
    try:
        from jnius import autoclass
        Build = autoclass("android.os.Build$VERSION")
        if Build.SDK_INT >= 29:
            Environment = autoclass("android.os.Environment")
            # 这个 API 从 API 29 起废弃，但取 Downloads 仍然可用且免权限
            d = Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DOWNLOADS)
            if d is not None:
                p = d.getAbsolutePath()
                if p:
                    return p
    except Exception as e:
        print("取公共下载目录失败(API):", e)

    # 2) 退回标准硬编码路径
    for p in ("/storage/emulated/0/Download",
              "/storage/emulated/0/Downloads",
              "/sdcard/Download",
              "/sdcard/Downloads"):
        if os.path.isdir(p):
            return p
    return "/storage/emulated/0/Download"


APP_DIR = app_dir()
SCRIPT_DIR = os.path.join(APP_DIR, "sources")

# 公共目录（优先）/ 应用私有目录（兜底）
PUBLIC_DOWNLOAD_DIR = os.path.join(_public_download_dir(), "落雪音源")
PRIVATE_DOWNLOAD_DIR = os.path.join(APP_DIR, "downloads")


def _is_writable(path):
    """真正写一个临时文件来验证可写性（只看 isdir 会误判）"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".lx_write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def get_download_dir():
    """每次下载时都重新判定一次。

    这样用户在授权页给了「所有文件访问」之后，
    不用重启 App 就能直接下到公共 Download 目录。
    """
    if _is_writable(PUBLIC_DOWNLOAD_DIR):
        return PUBLIC_DOWNLOAD_DIR
    if _is_writable(PRIVATE_DOWNLOAD_DIR):
        return PRIVATE_DOWNLOAD_DIR
    return PRIVATE_DOWNLOAD_DIR


# 启动时先解析一次（界面上显示用；实际下载会再调一次 get_download_dir）
DOWNLOAD_DIR = get_download_dir()

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



# ============================================================
#  安全回调包装：Kivy 不会捕获 Clock 回调里的异常，
#  未捕获异常会直接终止 App（闪退）。全部包起来。
# ============================================================
def safe_cb(fn):
    def _wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            print("UI 回调异常:", traceback.format_exc())
    return _wrapper


# ============================================================
#  自定义控件：让下拉菜单也支持中文字体
# ============================================================
from kivy.properties import StringProperty, ListProperty  # noqa: E402


class CNButton(Button):
    """带字体的按钮（用于下拉菜单项）"""
    font_name = StringProperty(None)


class CNSpinner(Spinner):
    """修复：Kivy 原生 Spinner 的下拉列表项不会继承 font_name，
    导致下拉菜单里的中文显示成方块。这里在创建下拉时统一套上字体。"""

    font_name = StringProperty(None)

    def _create_dropdown(self, *largs):
        super()._create_dropdown(*largs)
        try:
            self._apply_font_to_dropdown()
        except Exception as e:
            print("下拉菜单字体修复失败:", e)

    def _apply_font_to_dropdown(self):
        dd = getattr(self, "_dropdown", None)
        if dd is None or not self.font_name:
            return
        try:
            container = dd.container
            for child in container.children:
                if hasattr(child, "font_name"):
                    child.font_name = self.font_name
                    if hasattr(child, "text_size"):
                        child.halign = "left"
            # 下拉项每次打开都会重建，绑定 values 变化时重新套用
            dd.bind(on_open=lambda *_: self._apply_font_to_dropdown())
        except Exception:
            pass


class WebViewEngine:
    """在 Android WebView 里跑音源 JS"""

    def __init__(self):
        self.wv = None
        self.ready = False
        self._result_box = {}
        self._lock = threading.Lock()
        self._keepalive = []   # 保持 Java 回调对象存活，防止 GC 导致 native 崩溃

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
                    # 关键：允许 file/http 页面发起跨域 XHR，
                    # 否则宿主页面调 music.163.com 等会被 CORS 拦掉
                    try:
                        ws.setAllowUniversalAccessFromFileURLs(True)
                    except Exception:
                        pass
                    try:
                        ws.setAllowFileAccessFromFileURLs(True)
                    except Exception:
                        pass
                    try:
                        ws.setAllowFileAccess(True)
                    except Exception:
                        pass
                    # 关掉缓存，避免音源 API 返回旧数据
                    try:
                        ws.setCacheMode(2)   # LOAD_NO_CACHE
                    except Exception:
                        pass
                    wv.setWebViewClient(WebViewClient())
                    # 用 http://localhost/ 作 base：
                    #  - 页面本身不是 https，就不触发 mixed-content 拦截
                    #  - 配合 AllowUniversalAccessFromFileURLs 让跨域 XHR 可用
                    wv.loadDataWithBaseURL("http://localhost/",
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

                    Clock.schedule_once(safe_cb(_inject), 1.2)
                except Exception as e:
                    print("创建 WebView 失败:", e)
                    if on_ready:
                        on_ready(False)

            _create()
        except Exception as e:
            print("WebView 不可用:", e)
            self.ready = False

    def _eval_sync(self, js, timeout=3.0):
        """执行 JS 并把结果同步取回。
        evaluateJavascript 是异步的，且必须在 UI 线程调用；
        用 threading.Event 等 Java 回调返回结果。

        注意：
        - 回调对象必须保持引用（否则被 GC 回收 → native 崩溃）
        - 加锁避免多线程同时调用导致回调串台
        - 主线程调用时不能用 done.wait() 阻塞（会死锁，UI 线程无法执行 JS）
        """
        if not self.wv:
            return None

        with self._lock:
            box = {}
            done = threading.Event()

            from jnius import autoclass, PythonJavaClass, java_method

            class _CB(PythonJavaClass):
                __javainterfaces__ = ["android/webkit/ValueCallback"]
                __javacontext__ = "app"

                @java_method("(Ljava/lang/Object;)V")
                def onReceiveValue(self, value):
                    try:
                        box["v"] = value
                    except Exception:
                        box["v"] = None
                    finally:
                        done.set()

            cb = _CB()
            # 保活，防止回调对象被 GC（否则 native 层回调野指针 → SIGSEGV 闪退）
            self._keepalive.append(cb)

            def _run(dt):
                try:
                    self.wv.evaluateJavascript(js, cb)
                except Exception as e:
                    print("evaluateJavascript 失败:", e)
                    done.set()

            # 关键：如果已经在 UI 线程，绝对不能 done.wait()！
            # 因为 evaluateJavascript 的 ValueCallback 是通过 UI 线程的
            # Looper 派发的，阻塞 UI 线程 = 回调永远送不到 = ANR/闪退。
            if threading.current_thread() is threading.main_thread():
                try:
                    self.wv.evaluateJavascript(js, cb)
                except Exception as e:
                    print("evaluateJavascript 失败:", e)
                try:
                    self._keepalive.remove(cb)
                except ValueError:
                    pass
                # 不等待，立即返回 None；调用方会走异步/兜底路径
                return None
            Clock.schedule_once(safe_cb(_run), 0)
            if not done.wait(timeout):
                print("evaluateJavascript 超时")

            try:
                self._keepalive.remove(cb)
            except ValueError:
                pass
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
        """取直链。返回 (url_or_None, error_or_None)"""
        # WebView 未就绪 → 用纯 Python 引擎兜底
        if not self.ready or self.wv is None:
            return self._get_url_via_python(source, quality, music_info)

        # ---- WebView 路径 ----
        args = "%s, %s, %s" % (json.dumps(source), json.dumps(quality),
                               json.dumps(json.dumps(music_info, ensure_ascii=False)))
        raw = self._eval_sync("lxGetUrl(%s)" % args, timeout=8)
        if raw is None:
            # WebView 没响应，退回纯 Python 引擎再试一次
            url, err = self._get_url_via_python(source, quality, music_info)
            if url:
                return url, None
            return None, err or "WebView 无响应"

        deadline = time.time() + timeout
        while time.time() < deadline:
            poll = self._eval_sync("lxPoll()", timeout=5)
            if poll is None:
                time.sleep(0.3)
                continue
            try:
                s = json.loads(poll)
                if isinstance(s, str):
                    s = json.loads(s)
            except Exception:
                time.sleep(0.3)
                continue
            if isinstance(s, dict) and s.get("done"):
                if s.get("error"):
                    return None, s["error"]
                return s.get("url") or None, None
            time.sleep(0.3)
        # 超时也兜底试一次纯 Python
        url, err = self._get_url_via_python(source, quality, music_info)
        if url:
            return url, None
        return None, "超时"

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

# 深色主题配色
C_BG_HEADER = (0.13, 0.15, 0.20, 1)
C_CARD = (0.17, 0.19, 0.25, 1)
C_CTRL = (0.24, 0.27, 0.34, 1)
C_ACCENT = (0.26, 0.55, 0.96, 1)
C_TEXT = (0.93, 0.95, 0.98, 1)
C_DIM = (0.62, 0.67, 0.75, 1)
QUALITY_ORDER = ["128k", "192k", "320k", "flac", "flac24bit", "hires", "master"]


class DownloaderApp(App):
    title = "落雪音源下载器"

    def build(self):
        self.engine = WebViewEngine()
        self.sources = []
        self.song_list = []
        self.busy = False

        # 注册中文字体（必须在创建控件前），否则中文显示成空方块
        self.cn_font = register_cn_font()
        F = {"font_name": self.cn_font} if self.cn_font else {}

        root = BoxLayout(orientation="vertical")

        # ══════════════════════════════════════════
        #  顶部标题栏
        # ══════════════════════════════════════════
        header = BoxLayout(size_hint_y=None, height=dp(52),
                           padding=(dp(14), dp(8)), spacing=dp(8),
                           canvas_before=self._bg(C_BG_HEADER))
        title = Label(text="落雪音源下载器", bold=True, font_size=dp(18),
                      halign="left", valign="middle", color=C_TEXT, **F)
        title.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        header.add_widget(title)
        root.add_widget(header)

        # ══════════════════════════════════════════
        #  控制区（卡片）
        # ══════════════════════════════════════════
        panel = BoxLayout(orientation="vertical", size_hint_y=None,
                          padding=(dp(14), dp(12)), spacing=dp(10),
                          canvas_before=self._bg(C_CARD))
        panel.bind(minimum_height=panel.setter("height"))

        # --- 第一行：音源 ---
        r1 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        r1.add_widget(self._field_label("音源", F))
        self.sp_source = CNSpinner(text="加载中…", values=[], size_hint_x=None,
                                 width=dp(150), font_size=dp(14), **F)
        self.sp_source.background_color = C_CTRL
        r1.add_widget(self.sp_source)
        self.btn_pick = Button(text="更换", size_hint_x=None, width=dp(72),
                               font_size=dp(14), background_color=C_ACCENT,
                               background_normal="", color=(1, 1, 1, 1), **F)
        self.btn_pick.bind(on_release=self.pick_source_file)
        r1.add_widget(self.btn_pick)
        panel.add_widget(r1)

        # --- 第二行：平台 + 音质 ---
        r2 = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        r2.add_widget(self._field_label("平台", F))
        self.sp_platform = CNSpinner(text="网易云", values=["网易云"], size_hint_x=None,
                                   width=dp(105), font_size=dp(14), **F)
        self.sp_platform.background_color = C_CTRL
        r2.add_widget(self.sp_platform)
        r2.add_widget(self._field_label("音质", F))
        self.sp_quality = CNSpinner(text="320k", values=QUALITY_ORDER, size_hint_x=None,
                                  width=dp(105), font_size=dp(14), **F)
        self.sp_quality.background_color = C_CTRL
        r2.add_widget(self.sp_quality)
        panel.add_widget(r2)

        # --- 第三行：搜索 ---
        r3 = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        self.ti_search = TextInput(hint_text="输入歌名或歌手", multiline=False,
                                   size_hint_x=1, font_size=dp(15),
                                   padding=(dp(10), dp(12)),
                                   background_color=C_CTRL, foreground_color=C_TEXT,
                                   hint_text_color=(0.55, 0.6, 0.68, 1), **F)
        self.ti_search.bind(on_text_validate=self.do_search)
        r3.add_widget(self.ti_search)
        self.btn_search = Button(text="搜索", size_hint_x=None, width=dp(78),
                                 font_size=dp(15), bold=True,
                                 background_color=C_ACCENT, background_normal="",
                                 color=(1, 1, 1, 1), **F)
        self.btn_search.bind(on_release=self.do_search)
        r3.add_widget(self.btn_search)
        panel.add_widget(r3)
        root.add_widget(panel)

        # ══════════════════════════════════════════
        #  结果列表（占满剩余空间）
        # ══════════════════════════════════════════
        list_wrap = BoxLayout(orientation="vertical", padding=(dp(8), dp(4)))

        self.hint = Label(text="搜索后点结果即可下载", size_hint_y=None, height=dp(30),
                          font_size=dp(13), color=C_DIM, **F)
        list_wrap.add_widget(self.hint)

        self.sv = ScrollView(bar_width=dp(3), bar_color=(0.4, 0.45, 0.55, 1),
                             bar_inactive_color=(0.3, 0.33, 0.4, 1))
        self.results_box = BoxLayout(orientation="vertical", size_hint_y=None,
                                     spacing=dp(6), padding=(0, dp(2)))
        self.results_box.bind(minimum_height=self.results_box.setter("height"))
        self.sv.add_widget(self.results_box)
        list_wrap.add_widget(self.sv)
        root.add_widget(list_wrap)

        # ══════════════════════════════════════════
        #  底部状态栏
        # ══════════════════════════════════════════
        footer = BoxLayout(orientation="vertical", size_hint_y=None,
                           padding=(dp(14), dp(8)), spacing=dp(6),
                           canvas_before=self._bg(C_BG_HEADER))
        footer.bind(minimum_height=footer.setter("height"))

        self.pb = ProgressBar(max=100, size_hint_y=None, height=dp(6))
        footer.add_widget(self.pb)

        self.log = Label(text="正在启动…", size_hint_y=None, height=dp(34),
                         font_size=dp(12), color=C_DIM,
                         halign="left", valign="middle", **F)
        self.log.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        footer.add_widget(self.log)
        root.add_widget(footer)

        Clock.schedule_once(safe_cb(self._boot), 0.3)
        return root

    # ---------- UI 小工具 ----------
    def _bg(self, color):
        """生成一个纯色矩形作为背景"""
        from kivy.graphics import Color, Rectangle

        def _draw(widget, *_):
            widget.canvas.before.clear()
            with widget.canvas.before:
                Color(*color)
                Rectangle(pos=widget.pos, size=widget.size)
        return _draw

    def _field_label(self, text, F):
        """表单左侧的小标签，固定宽度保证各行对齐"""
        lb = Label(text=text, size_hint_x=None, width=dp(44),
                   font_size=dp(14), color=C_DIM,
                   halign="left", valign="middle", **F)
        lb.bind(size=lambda b, v: setattr(b, "text_size", (v[0], None)))
        return lb

    # ---------- 启动 ----------
    def _boot(self, dt):
        try:
            request_storage_permission()
            extract_default_source()
            if IS_ANDROID:
                self.engine.start(on_ready=self._on_engine_ready)
            else:
                # 桌面调试：用内置的 dukpy 引擎
                self._on_engine_ready(True)
        except Exception as e:
            self.set_status("启动失败: %s" % e)
            print("_boot 异常:", traceback.format_exc())

    def _on_engine_ready(self, ok):
        # 这个回调从 UI 线程的 WebView 流程里调过来，必须全包住
        try:
            if not ok:
                # WebView 失败 → 放后台线程试纯 Python 引擎，别卡死 UI
                self.set_status("WebView 不可用，尝试备用引擎...")

                def _bg():
                    try:
                        self.load_source(DEFAULT_SCRIPT)
                    except Exception as e:
                        print("备用引擎失败:", e)
                        Clock.schedule_once(safe_cb(lambda dt: self.set_status("引擎不可用: %s" % e)), 0)

                threading.Thread(target=_bg, daemon=True).start()
                return
            self.set_status("引擎就绪，加载音源...")
            self.load_source(DEFAULT_SCRIPT)
        except Exception as e:
            self.set_status("引擎初始化出错: %s" % e)
            print("_on_engine_ready 异常:", traceback.format_exc())

    def load_source(self, path):
        try:
            self._load_source_inner(path)
        except Exception as e:
            self.set_status("音源加载异常: %s" % e)
            print("load_source 异常:", traceback.format_exc())

    def _load_source_inner(self, path):
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
        if not isinstance(info, dict):
            self.set_status("音源返回数据异常")
            return
        self.sources = []
        labels = []
        for key, v in (info.get("sources") or {}).items():
            v = v or {}
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
                        Clock.schedule_once(safe_cb(lambda dt: app.load_source(DEFAULT_SCRIPT)), 0)
                    except Exception as e:
                        msg = str(e)
                        Clock.schedule_once(safe_cb(lambda dt: app.set_status("读取音源失败: %s" % msg)), 0)

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
        """点「搜索」按钮 / 回车进来。任何异常都必须被吞掉并显示，绝不能闪退。"""
        try:
            kw = self.ti_search.text.strip()
            if not kw:
                self.set_status("请先输入歌名")
                return
            if self.busy:
                self.set_status("正在处理中，请稍候...")
                return
            self.busy = True
            self.set_status("搜索中: %s" % kw)
            self.clear_results()
            threading.Thread(target=self._search_worker, args=(kw,),
                             daemon=True).start()
        except Exception as e:
            self.busy = False
            self.set_status("搜索出错: %s" % e)
            print("do_search 异常:", traceback.format_exc())

    def _search_worker(self, kw):
        """后台线程：只做网络请求，结果通过 Clock 回主线程"""
        songs, err = [], None
        try:
            songs = search_netease(kw, 15)
        except Exception as e:
            err = str(e)
            print("搜索网络异常:", traceback.format_exc())
        # 统一回主线程处理，并且回调内部再包一层 try
        Clock.schedule_once(safe_cb(lambda dt: self._after_search(songs, err)), 0)

    def _after_search(self, songs, err):
        try:
            self.busy = False
            if err:
                self.set_status("搜索失败: %s" % err)
                return
            self._show_results(songs)
        except Exception as e:
            self.busy = False
            self.set_status("显示结果出错: %s" % e)
            print("_after_search 异常:", traceback.format_exc())

    def clear_results(self):
        self.results_box.clear_widgets()

    def _show_results(self, songs):
        self.song_list = list(songs or [])
        self.clear_results()
        if not self.song_list:
            self.set_status("没有找到结果，换个关键词试试")
            return
        for i, s in enumerate(songs):
            bkw = {"font_name": self.cn_font} if self.cn_font else {}
            btn = Button(text="%d. %s — %s  [%s]" % (i + 1, s["name"], s["singer"], s["interval"]),
                         size_hint_y=None, height=dp(46), halign="left", valign="middle",
                         font_size=dp(12), **bkw)
            # Kivy 的 Button 不会自动按 halign 换行，需手动把文字宽度绑到按钮宽度
            btn.bind(size=lambda b, v: setattr(b, "text_size", (v[0] - dp(8), None)))
            btn.bind(on_release=lambda b, idx=i: self.start_download(idx))
            self.results_box.add_widget(btn)
        self.set_status("找到 %d 首，点一首开始下载" % len(songs))

    # ---------- 下载 ----------
    def start_download(self, idx):
        try:
            self._start_download_inner(idx)
        except Exception as e:
            self.set_status("开始下载出错: %s" % e)
            print("start_download 异常:", traceback.format_exc())

    def _start_download_inner(self, idx):
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
                Clock.schedule_once(safe_cb(lambda dt, qq=q: self.set_status(
                    "向音源请求直链 (%s)..." % qq)), 0)
                u, e = self.engine.get_url(plat, q, info)
                if u and not is_restricted(u):
                    url, quality = u, q
                    break
                err = e or "直链受限"
                if qi < 2:
                    Clock.schedule_once(safe_cb(lambda dt: self.set_status(
                        "直链受限，降级重试...")), 0)
            if url is None:
                Clock.schedule_once(safe_cb(lambda dt: self.set_status(
                    "取直链失败: %s（该歌曲可能版权受限）" % err)), 0)
                return
            ext = guess_ext(url, quality)
            base = safe_name("%s - %s" % (song["name"], song["singer"]))
            out_dir = get_download_dir()
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError:
                pass
            dest = os.path.join(out_dir, "%s.%s" % (base, ext))

            def prog(got, total):
                Clock.schedule_once(safe_cb(
                    lambda dt: self._update_prog(got, total, song["name"])), 0)

            Clock.schedule_once(safe_cb(lambda dt: self.set_status("下载中: %s" % song["name"])), 0)
            size = download_file(url, dest, on_progress=prog)
            Clock.schedule_once(safe_cb(lambda dt: self._done(dest, size)), 0)
        except Exception as e:
            msg = str(e)
            Clock.schedule_once(safe_cb(lambda dt: self.set_status("下载失败: %s" % msg)), 0)

    def _update_prog(self, got, total, name):
        self.pb.value = got * 100.0 / max(total, 1)
        self.set_status("下载中 %s: %.1f%% (%.1f/%.1f MB)"
                        % (name, self.pb.value, got / 1048576, total / 1048576))

    def _done(self, path, size):
        self.pb.value = 100
        self.set_status("✓ 完成: %s (%.1f MB)\n保存于 %s"
                        % (os.path.basename(path), size / 1048576,
                           os.path.dirname(path)))

    def set_status(self, txt):
        try:
            self.status.text = str(txt)
        except Exception:
            pass


# ============================================================
#  兜底：把任何未捕获异常写到界面 + 日志文件，而不是直接闪退
# ============================================================
def _install_crash_guard():
    import sys as _sys

    log_path = os.path.join(APP_DIR, "crash.log")

    def _hook(exc_type, exc, tb):
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
        except Exception:
            text = "%s: %s" % (exc_type, exc)
        print(text, file=_sys.stderr)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(text + "\n" + "-" * 50 + "\n")
        except Exception:
            pass
        # Kivy 的异常发生在事件循环里时，不要把整个 App 拖死
        try:
            app = App.get_running_app()
            if app is not None and hasattr(app, "set_status"):
                first = text.strip().splitlines()[-1] if text.strip() else "未知错误"
                app.set_status("出错了: %s" % first[:120])
        except Exception:
            pass

    _sys.excepthook = _hook


if __name__ == "__main__":
    _install_crash_guard()
    try:
        DownloaderApp().run()
    except Exception:
        import sys as _s
        text = traceback.format_exc()
        print(text, file=_s.stderr)
        try:
            with open(os.path.join(APP_DIR, "crash.log"), "a",
                      encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        raise
