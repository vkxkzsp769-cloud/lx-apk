"""WebView JS 引擎桥。

为什么用 WebView 当 JS 引擎
-------------------------
python-for-android 的 recipe 里没有 dukpy / quickjs / v8 这类 JS 引擎
（都要编译 C 扩展，编不进 APK）。但 Android 系统自带 WebView，
它本身就是一个完整的 JS 引擎 + 网络栈，所以直接拿它跑音源脚本。

线程模型（这里有三个线程，别搞混）
----------------------------------
  1. Android UI 线程（Java）—— WebView 的宿主线程。
     创建 WebView、调用 evaluateJavascript 都必须在它上面，
     否则抛 AndroidRuntimeException:
       "Calling WebView methods on a different thread
        than the one it was created on"
     用 @run_on_ui_thread 把活儿投上去。

  2. Kivy / Python 主线程 —— 跑事件循环、改控件。
     p4a SDL2 下它 **不是** Android UI 线程（两者不同！），
     所以不能靠 Clock.schedule_once 去调 WebView。  ← 之前就栽在这

  3. 后台工作线程 —— 发起一次 JS 调用并阻塞等结果。

  流程：后台线程 -> @run_on_ui_thread 投到 Java UI 线程执行
        -> ValueCallback 在 UI 线程回调 -> 唤醒后台线程。

  对外方法都要求「非 Kivy 主线程」调用；内部 _lock 串行化。
"""
import json
import threading
import time

from kivy.clock import Clock

from appenv import IS_ANDROID, diag, log, log_exc
from lxhost import LX_HOST_JS


class LxBridge:
    def __init__(self):
        self._wv = None
        self._cb_sink = None        # 注入宿主时用的空回调
        self._keepalive = []        # 保持 Java 回调对象存活，防 GC 崩溃
        self._lock = threading.Lock()
        self._ready = False
        self._ui_eval = None        # 投到 Java UI 线程执行 JS 的函数

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
        except Exception as e:
            log_exc("LxBridge.start")
            diag("LxBridge.start 失败: %r" % (e,))
            self._ready = False
            on_ready(False, str(e))

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

        # evaluateJavascript 必须回到「创建 WebView 的那个线程」（Java UI 线程）
        @run_on_ui_thread
        def _eval_on_ui(wv, js, cb, on_error):
            try:
                wv.evaluateJavascript(js, cb)
            except Exception as e:
                diag("evaluateJavascript 抛异常: %s" % e)
                on_error()

        self._ui_eval = _eval_on_ui

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
            except Exception as e:
                log_exc("创建 WebView")
                diag("创建 WebView 失败: %r" % (e,))
                self._ready = False
                self._on_ready(False, "创建 WebView 失败: %s" % e)

        _make()

    def _inject_and_probe(self):
        """等页面加载完 -> 注入宿主 -> 轮询 typeof lx 确认可用"""
        time.sleep(0.6)
        diag("开始注入宿主 LX_HOST_JS (%d 字节)" % len(LX_HOST_JS))
        try:
            r = self._eval(LX_HOST_JS, timeout=10)
            diag("注入返回: %r" % (str(r)[:120],))
        except Exception as e:
            log_exc("注入宿主")
            diag("注入宿主异常: %r" % (e,))

        seen = []
        for _ in range(25):                 # 最多约 8 秒
            try:
                v = self._eval("typeof lx", timeout=3)
            except Exception as e:
                v = "EXC:%s" % e
            seen.append(str(v))
            if v and "object" in str(v):
                self._ready = True
                diag("宿主就绪, typeof lx = %s" % v)
                log("WebView 宿主就绪")
                self._on_ready(True)
                return
            time.sleep(0.2)

        diag("宿主未就绪, typeof lx 采样: %s" % (seen[:6],))
        log("WebView 宿主未就绪（typeof lx 一直不是 object）")
        self._ready = False
        self._on_ready(False, "宿主注入后 typeof lx = %s" % (seen[0] if seen else "无响应"))

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

            # 注意：不能用 Clock.schedule_once —— 那跑在 Kivy 线程上，
            # 而 WebView 是在 Java UI 线程创建的，跨线程调用会抛异常
            self._ui_eval(self._wv, js, cb, done.set)
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
        """执行 JS 并把结果当 JSON 解析。

        这里有「两层」JSON，别只解一层：
          evaluateJavascript 会把返回值再做一次 JSON 编码
          （所以拿到的字符串形如 "\"{...}\"")；
          而 lxInit()/lxPoll() 返回的本身就是一个 JSON 字符串。
        所以要连续解两次，直到拿到的不是字符串为止。
        实测症状：只解一层会得到 str，于是报「音源返回了意外数据」。
        """
        raw = self._eval(js, timeout=timeout)
        if raw is None:
            return None

        value = str(raw)
        for _ in range(3):
            if not isinstance(value, str):
                break
            try:
                value = json.loads(value)
            except Exception:
                break
        return value
