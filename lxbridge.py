"""WebView JS 引擎桥。

为什么用 WebView 当 JS 引擎
-------------------------
python-for-android 的 recipe 里没有 dukpy / quickjs / v8 这类 JS 引擎
（都要编译 C 扩展，编不进 APK）。但 Android 系统自带 WebView，
它本身就是一个完整的 JS 引擎 + 网络栈，所以直接拿它跑音源脚本。

线程规则（整个工程只有这一条约定，务必遵守）
-------------------------------------------
  Kivy 的事件循环 == Python 的 main thread。
  WebView 只能在 Android UI 线程创建/调用，而 evaluateJavascript 的
  ValueCallback 也是靠 UI 线程的 Looper 派发的。

  所以：
    * evaluateJavascript 用 Clock.schedule_once 投到 Kivy 线程执行；
    * 调用方必须在「非 Kivy 线程」上阻塞等结果。
  若在 Kivy 线程上阻塞，Looper 无法派发回调 -> 死锁 -> ANR -> 进程被杀。

  本类的对外方法都要求非 UI 线程调用；内部用 _lock 串行化，
  避免两个线程同时发起导致回调互相串台。
"""
import json
import threading
import time

from kivy.clock import Clock

from appenv import IS_ANDROID, log, log_exc
from lxhost import LX_HOST_JS


class LxBridge:
    def __init__(self):
        self._wv = None
        self._cb_sink = None        # 注入宿主时用的空回调
        self._keepalive = []        # 保持 Java 回调对象存活，防 GC 崩溃
        self._lock = threading.Lock()
        self._ready = False

    # ---------- 对外 ----------
    @property
    def ready(self):
        return self._ready

    def start(self, on_ready):
        """创建 WebView 并校验宿主就绪；on_ready(ok) 在后台线程回调"""
        if not IS_ANDROID:
            log("非 Android，WebView 引擎不可用")
            on_ready(False)
            return
        try:
            self._on_ready = on_ready
            self._create_on_ui()
        except Exception:
            log_exc("LxBridge.start")
            self._ready = False
            on_ready(False)

    def load_source(self, code):
        """加载音源。返回 (ok, info_or_error)。必须在非 UI 线程调用。"""
        self._assert_not_ui_thread()
        raw = self._eval_json("lxInit(%s)" % json.dumps(code), timeout=20)
        if raw is None:
            return False, "音源脚本无响应（WebView 未就绪或脚本卡住）"
        if isinstance(raw, str) and raw.startswith("ERR"):
            return False, raw[4:]
        if not isinstance(raw, dict):
            return False, "音源返回了意外数据: %r" % (raw,)
        return True, raw

    def music_url(self, source, quality, music_info, timeout=60):
        """取直链。返回 (url_or_None, error_or_None)。必须在非 UI 线程调用。"""
        self._assert_not_ui_thread()
        args = "%s, %s, %s" % (
            json.dumps(source), json.dumps(quality),
            json.dumps(json.dumps(music_info, ensure_ascii=False)))
        if self._eval_json("lxGetUrl(%s)" % args, timeout=10) is None:
            return None, "WebView 无响应"

        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self._eval_json("lxPoll()", timeout=5)
            if isinstance(res, dict) and res.get("done"):
                if res.get("error"):
                    return None, str(res["error"])
                return res.get("url") or None, None
            time.sleep(0.25)
        return None, "取直链超时（%d 秒）" % timeout

    # ---------- WebView 创建（UI 线程） ----------
    def _create_on_ui(self):
        from jnius import autoclass, PythonJavaClass, java_method
        from android.runnable import run_on_ui_thread

        WebView = autoclass("android.webkit.WebView")
        WebViewClient = autoclass("android.webkit.WebViewClient")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity

        @run_on_ui_thread
        def _make():
            try:
                wv = WebView(activity)
                ws = wv.getSettings()
                ws.setJavaScriptEnabled(True)
                ws.setDomStorageEnabled(True)
                for setter, value in (
                    ("setAllowUniversalAccessFromFileURLs", True),
                    ("setAllowFileAccessFromFileURLs", True),
                    ("setAllowFileAccess", True),
                    ("setMixedContentMode", 0),
                ):
                    try:
                        getattr(ws, setter)(value)
                    except Exception:
                        pass
                try:
                    ws.setCacheMode(2)      # LOAD_NO_CACHE
                except Exception:
                    pass

                wv.setWebViewClient(WebViewClient())

                class _Sink(PythonJavaClass):
                    __javainterfaces__ = ["android/webkit/ValueCallback"]
                    __javacontext__ = "app"

                    @java_method("(Ljava/lang/Object;)V")
                    def onReceiveValue(self, value):
                        pass

                self._cb_sink = _Sink()
                self._wv = wv

                # 用 http://localhost/ 作 base：
                #  - 页面本身不是 https，就不触发 mixed-content 拦截
                #  - 配合 AllowUniversalAccessFromFileURLs 让跨域 XHR 可用
                wv.loadDataWithBaseURL(
                    "http://localhost/", "<html><body></body></html>",
                    "text/html", "utf-8", None)

                threading.Thread(target=self._inject_and_probe,
                                 daemon=True).start()
            except Exception:
                log_exc("创建 WebView")
                self._ready = False
                self._on_ready(False)

        _make()

    def _inject_and_probe(self):
        """等页面加载完 -> 注入宿主 -> 轮询 typeof lx 确认可用"""
        time.sleep(0.6)
        try:
            self._eval(LX_HOST_JS, timeout=10)
        except Exception:
            log_exc("注入宿主")

        for _ in range(25):                 # 最多约 8 秒
            try:
                v = self._eval("typeof lx", timeout=3)
            except Exception:
                v = None
            if v and "object" in str(v):
                self._ready = True
                log("WebView 宿主就绪")
                self._on_ready(True)
                return
            time.sleep(0.2)

        log("WebView 宿主未就绪（typeof lx 一直不是 object）")
        self._ready = False
        self._on_ready(False)

    # ---------- 执行 JS ----------
    @staticmethod
    def _assert_not_ui_thread():
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError(
                "LxBridge 的 JS 调用不能在 UI 线程执行："
                "回调要靠 UI 线程派发，阻塞会死锁")

    def _eval(self, js, timeout=3.0):
        """执行 JS 并同步取回原始字符串。只能由非 UI 线程调用。"""
        if self._wv is None:
            return None
        self._assert_not_ui_thread()

        with self._lock:
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
            self._keepalive.append(cb)      # 防 GC：被回收会导致 native 崩溃

            def _run(dt):
                try:
                    self._wv.evaluateJavascript(js, cb)
                except Exception:
                    log_exc("evaluateJavascript")
                    done.set()

            Clock.schedule_once(_run, 0)
            finished = done.wait(timeout)

            try:
                self._keepalive.remove(cb)
            except ValueError:
                pass

            if not finished:
                log("JS 超时(%ss): %s" % (timeout, js[:70].replace("\n", " ")))
                return None
            return box.get("v")

    def _eval_json(self, js, timeout=3.0):
        """执行 JS 并把结果当 JSON 解析（evaluateJavascript 返回的是 JSON 编码串）"""
        raw = self._eval(js, timeout=timeout)
        if raw is None:
            return None
        s = str(raw)
        try:
            return json.loads(s)
        except Exception:
            return s
