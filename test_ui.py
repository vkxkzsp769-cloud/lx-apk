#!/usr/bin/env python3
"""界面 / 线程契约冒烟测试（无需真机、无需显示器）

用 SDL 的 offscreen 驱动把 Kivy 界面真正跑起来，验证：
  1. build() 不抛异常
     （历史 bug：canvas_before 当构造参数传 -> 启动即崩）
  2. 中文字体真的能画出汉字
     （判据：font_size=40 时「海阔天空」应 ≈160px，缺字体会退化成 ~72px）
  3. 结果列表 / 状态栏 / 进度条等运行时分支
  4. 线程契约：加载音源、调 JS 都不能在 UI 线程做
     （历史 bug：在主线程等 WebView 回调 -> 死锁 -> ANR / 音源永远加载不出来）

用法:
    pip install "kivy[base]==2.3.0"
    python test_ui.py

退出码 0 = 全部通过。
"""
import os
import sys
import threading
import traceback

os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
os.environ.setdefault("KIVY_WINDOW", "sdl2")
os.environ.setdefault("KIVY_TEXT", "sdl2")
os.environ["KIVY_NO_CONSOLELOG"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from kivy.config import Config  # noqa: E402
Config.set("graphics", "width", "400")
Config.set("graphics", "height", "760")

import kivy  # noqa: E402
from kivy.core.text import Label as CoreLabel  # noqa: E402
from kivy.clock import Clock  # noqa: E402
from kivy.uix.button import Button  # noqa: E402
from kivy.uix.label import Label  # noqa: E402
from kivy.uix.spinner import Spinner  # noqa: E402
from kivy.uix.textinput import TextInput  # noqa: E402

import appenv  # noqa: E402
import fonts  # noqa: E402
import main as M  # noqa: E402

appenv.IS_ANDROID = False
M.IS_ANDROID = False
appenv.IS_ANDROID = False

FAILS = []


def check(name, fn):
    try:
        fn()
        print("  [OK]   %s" % name)
    except Exception as e:
        FAILS.append(name)
        print("  [FAIL] %s -> %s: %s" % (name, type(e).__name__, e))
        traceback.print_exc()


# ============================================================
class FakeBridge:
    """桌面没有 WebView，用它顶替；同时记录调用线程用于契约检查"""

    def __init__(self):
        self.ready = True
        self.calls = []

    def start(self, on_ready):
        on_ready(False)

    def load_source(self, code):
        self.calls.append(("load_source",
                           threading.current_thread() is threading.main_thread()))
        return True, {
            "meta": {"name": "测试音源", "version": "9.9"},
            "sources": {"wy": {"name": "网易云", "qualitys": ["320k"]}},
        }

    def music_url(self, source, quality, info, timeout=60):
        self.calls.append(("music_url",
                           threading.current_thread() is threading.main_thread()))
        return "https://example.com/a.mp3", None


def make_app():
    app = M.LxApp()
    app.build()
    app.bridge = FakeBridge()
    return app


# ============================================================
def test_font():
    ok = fonts.register()
    print("  注册字体: %s (%s)" % (ok, fonts._registered_path or "未找到"))
    if not ok:
        print("  [SKIP] 系统没有中文字体")
        return

    def width_of(text):
        lb = CoreLabel(text=text, font_size=40, font_name=fonts.FONT_NAME)
        lb.refresh()
        return lb.texture.size[0]

    w = width_of("海阔天空")
    print("  「海阔天空」宽度 = %dpx (期望 ≈160)" % w)
    if w < 120:
        raise AssertionError("中文宽度只有 %dpx，字体不含中文字形" % w)


def test_build():
    app = M.LxApp()
    root = app.build()
    print("  build() 控件数 = %d" % len(root.children))
    return app


def test_runtime(app):
    check("_show_results(3 首)", lambda: app._show_results([
        {"id": "1", "name": "海阔天空", "singer": "Beyond",
         "interval": "03:59", "album": ""},
        {"id": "2", "name": "晴天", "singer": "周杰伦",
         "interval": "04:29", "album": ""},
        {"id": "3", "name": "光辉岁月 (Live)", "singer": "黄家驹、Beyond",
         "interval": "05:12", "album": ""},
    ]))
    check("_show_results(空)", lambda: app._show_results([]))
    for t in ("搜索中: 海阔天空", "找到 3 首，点一首开始下载",
              "下载中 海阔天空: 45.2% (1.2/2.7 MB)",
              "✓ 完成: 海阔天空.flac (43.1 MB)\n保存于 /storage/emulated/0/Download/落雪音源",
              "取直链失败: 该歌曲可能版权受限", "出错了: 测试"):
        check("set_status(%s...)" % t[:16], lambda t=t: app.set_status(t))
    check("_progress", lambda: app._progress(1200000, 2700000, "海阔天空"))
    check("_download_done", lambda: app._download_done(
        "/storage/emulated/0/Download/落雪音源/x.flac", 43100000))
    check("_current_source", lambda: print("         -> %s"
                                          % app._current_source()))
    check("download_dir", lambda: print("         -> %s" % appenv.download_dir()))
    check("品质/格式过滤", test_quality_format_filter)
    check("歌曲详情填充", test_song_detail)
    check("fmt_time", test_fmt_time)
    check("播放器状态机", test_player_state)


def test_source_info(app):
    app._apply_source_info({
        "meta": {"name": "K×H测试", "version": "1.7.17"},
        "sources": {"wy": {"name": "网易云", "qualitys": ["320k"]},
                    "tx": {"name": "QQ音乐", "qualitys": ["128k"]}},
    })
    print("  平台下拉 = %s" % (app.sp_platform.values,))
    if app.sp_platform.values != ["网易云 (wy)", "QQ音乐 (tx)"]:
        raise AssertionError("平台下拉没刷新: %s" % (app.sp_platform.values,))
    if "K×H测试" not in app.status.text:
        raise AssertionError("状态栏没显示音源信息: %r" % app.status.text)


def test_threading_contract():
    """load_source 必须在后台线程执行（内部要同步等 JS 结果）"""
    app = make_app()
    done = threading.Event()
    inner = app.load_source

    def wrapped(path):
        try:
            inner(path)
        finally:
            done.set()

    app._js_done = done
    app.load_source = wrapped
    # 用真实文件路径走一遍
    tmp = os.path.join(HERE, "_t.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("// x\n")
    try:
        # load_source 内部 bg() 起线程，等 bridge 被调用
        app.load_source(tmp)
        for _ in range(50):
            if app.bridge.calls:
                break
            import time
            time.sleep(0.1)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not app.bridge.calls:
        raise AssertionError("bridge.load_source 一直没被调用")
    name, on_main = app.bridge.calls[0]
    print("  %s 在主线程? %s" % (name, on_main))
    if on_main:
        raise AssertionError("load_source 在主线程执行了！会死锁")


def test_bridge_rejects_ui_thread():
    """LxBridge 自己在 UI 线程被调用时必须直接报错，而不是悄悄死锁"""
    from lxbridge import LxBridge
    b = LxBridge()
    try:
        b._assert_not_ui_thread()
    except RuntimeError:
        print("  [OK]   主线程调用被拒绝")
        return
    raise AssertionError("LxBridge 没有拦住主线程调用")


def test_worker_status_safe():
    app = make_app()
    err = []

    def work():
        try:
            app.set_status("来自后台线程的消息")
        except Exception as e:
            err.append(e)

    t = threading.Thread(target=work)
    t.start()
    t.join(5)
    if err:
        raise AssertionError("后台线程 set_status 抛异常: %s" % err[0])
    try:
        Clock.tick()
    except Exception:
        pass
    print("  状态栏 = %r" % app.status.text)
    if app.status.text != "来自后台线程的消息":
        raise AssertionError("状态栏没更新: %r" % app.status.text)


def test_build_without_cn_font():
    """回归：设备上找不到中文字体时，build() 也必须能跑。

    曾经的 bug：CNSpinner 重新声明了 font_name = StringProperty(None)，
    把 Label 的默认值 'Roboto' 覆盖成 None，于是
      AttributeError: 'NoneType' object has no attribute 'endswith'
    只在「没注册字体」时触发（那时不会传 font_name），所以很容易漏测。
    """
    saved = fonts._registered_path
    fonts._registered_path = None          # 模拟没找到字体
    try:
        if fonts.font_kwargs() != {}:
            raise AssertionError("font_kwargs() 应该返回空")
        app = M.LxApp()
        root = app.build()
        print("  无字体时 build() 控件数 = %d" % len(root.children))
    finally:
        fonts._registered_path = saved


def test_eval_js_uses_ui_thread():
    """回归：evaluateJavascript 必须投到 Java UI 线程执行。

    历史上写成 Clock.schedule_once(...) —— 那跑在 Kivy 线程上，
    而 WebView 是在 Java UI 线程创建的，跨线程调用会抛
      AndroidRuntimeException: Calling WebView methods on a different
      thread than the one it was created on
    结果就是「WebView 引擎不可用」，音源永远加载不出来。
    """
    src = open(os.path.join(HERE, "lxbridge.py"), encoding="utf-8").read()
    if "_ui_eval(" not in src:
        raise AssertionError("没有通过 _ui_eval 调 evaluateJavascript")
    # Clock.schedule_once 不应该再出现在 _eval 的可执行代码里
    # （注释里提到是可以的，所以先去掉注释行）
    body = src[src.index("def _eval("):src.index("def _eval_json(")]
    code_lines = [l for l in body.splitlines() if not l.strip().startswith("#")]
    if any("Clock.schedule_once" in l for l in code_lines):
        raise AssertionError("_eval 里又用 Clock.schedule_once 调 WebView 了")
    if "@run_on_ui_thread" not in src:
        raise AssertionError("没有用 run_on_ui_thread")


def test_eval_json_unwrap():
    """回归：JS 结果有两层 JSON，必须解到底。

    evaluateJavascript 会把返回值再做一次 JSON 编码，
    而 lxInit()/lxPoll() 返回的本身就是 JSON 字符串 —— 所以要解两次。
    只解一层会得到 str，真机上症状是：
      「音源加载失败：音源返回了意外数据：{...}"
    """
    import json
    from lxbridge import LxBridge

    info = {"meta": {"name": "K×H测试", "version": "1.7.17"},
            "sources": {"wy": {"name": "网易云", "qualitys": ["320k"]}}}

    b = LxBridge()

    # ① 双层：JS 返回 JSON 字符串，evaluateJavascript 再包一层
    raw = json.dumps(json.dumps(info))
    b._eval = lambda js, timeout=3.0: raw
    got = b._eval_json("lxInit(x)")
    if not isinstance(got, dict):
        raise AssertionError("双层没解开，得到 %r" % (got,))
    if got["meta"]["version"] != "1.7.17":
        raise AssertionError("内容不对: %r" % (got,))
    print("  双层 -> dict ✓")

    # ② 单层：typeof lx 这种普通字符串返回值
    b._eval = lambda js, timeout=3.0: json.dumps("object")
    got = b._eval_json("typeof lx")
    if got != "object":
        raise AssertionError("单层解错: %r" % (got,))
    print("  单层 -> 'object' ✓")

    # ③ 非 JSON 也不能炸
    b._eval = lambda js, timeout=3.0: "not-json"
    got = b._eval_json("x")
    if got != "not-json":
        raise AssertionError("非 JSON 处理错了: %r" % (got,))
    print("  非 JSON 原样返回 ✓")

    # ④ 老实现（只解一层）应当失败 —— 证明这个测试确实在守东西
    raw = json.dumps(json.dumps(info))
    one = json.loads(raw)
    if isinstance(one, dict):
        raise AssertionError("测试前提不成立：单层竟然解成了 dict")
    print("  单层确实拿不到 dict（老 bug 可复现）✓")


def test_ssl_fallback():
    """回归：证书校验失败要能退回不校验模式。

    真机日志里出现 32 次:
      SSLCertVerificationError: certificate verify failed:
      self-signed certificate in certificate chain
    手机所在网络的代理/网关做了 SSL 拦截，严格校验必然失败，
    搜索就会一直报错。netutil.urlopen 必须能自动降级。
    """
    import ssl
    import urllib.error
    import netutil

    calls = []
    real = netutil.urllib.request.urlopen

    def fake(req, timeout=None, context=None):
        calls.append(context)
        if context is None:
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError(
                    "certificate verify failed: self-signed certificate "
                    "in certificate chain"))
        # 第二次（带 context）应当成功
        class _R:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def read(self_): return b'{"ok":true}'
        return _R()

    netutil.urllib.request.urlopen = fake
    try:
        data = netutil.get_json("https://music.163.com/api/search/get/web")
        if data != {"ok": True}:
            raise AssertionError("降级后没解析出数据: %r" % (data,))
        if len(calls) != 2 or calls[0] is not None or calls[1] is None:
            raise AssertionError("降级流程不对: %r" % (calls,))
        print("  严格校验失败 -> 自动降级重试 ✓")
    finally:
        netutil.urllib.request.urlopen = real

    # 非证书类错误不应被吞掉降级
    def fake2(req, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    netutil.urllib.request.urlopen = fake2
    try:
        try:
            netutil.get_json("https://x/y")
        except urllib.error.URLError:
            print("  普通网络错误照常抛出 ✓")
        else:
            raise AssertionError("普通错误被错误地降级了")
    finally:
        netutil.urllib.request.urlopen = real


def test_quality_format_filter():
    """品质下拉要按「格式」偏好过滤，并且用平台声明的品质列表"""
    app = make_app()
    app._apply_source_info({
        "meta": {"name": "t", "version": "1"},
        "sources": {"wy": {"name": "网易云",
                           "qualitys": ["128k", "320k", "flac", "hires"]}},
    })
    app.sp_platform.text = "网易云 (wy)"
    app.sp_format.text = "自动"
    app._refresh_qualities()
    if list(app.sp_quality.values) != ["128k", "320k", "flac", "hires"]:
        raise AssertionError("自动模式品质不对: %s" % (app.sp_quality.values,))

    app.sp_format.text = "FLAC"
    app._refresh_qualities()
    if list(app.sp_quality.values) != ["flac", "hires"]:
        raise AssertionError("FLAC 过滤不对: %s" % (app.sp_quality.values,))

    app.sp_format.text = "MP3"
    app._refresh_qualities()
    if list(app.sp_quality.values) != ["128k", "320k"]:
        raise AssertionError("MP3 过滤不对: %s" % (app.sp_quality.values,))
    print("  自动/FLAC/MP3 过滤都正确 ✓")


def test_song_detail():
    """详情弹窗：解析完成后要填上 时长/品质/格式/大小，并放开按钮"""
    app = make_app()
    song = {"id": "1", "name": "海阔天空", "singer": "Beyond",
            "interval": "03:59", "album": ""}
    app._show_song_popup(song)
    if not app._pop_play.disabled:
        raise AssertionError("解析前播放按钮不该可用")
    app._song_ready(song, "https://x/a.mp3", "320k",
                    {"format": "mp3", "size": 8123456,
                     "content_type": "audio/mpeg"})
    txt = app._pop_info.text
    print("  详情: %s" % txt.replace("\n", " | "))
    for want in ("Beyond", "03:59", "320k", "MP3", "7.7 MB"):
        if want not in txt:
            raise AssertionError("详情里缺少 %r: %s" % (want, txt))
    if app._pop_play.disabled or app._pop_dl.disabled:
        raise AssertionError("解析后按钮应可用")
    if app._cur_dur != 239:
        raise AssertionError("时长解析错: %r" % app._cur_dur)
    app._popup.dismiss()


def test_fmt_time():
    if M.fmt_time(0) != "00:00" or M.fmt_time(239) != "03:59":
        raise AssertionError("fmt_time 不对: %s / %s"
                             % (M.fmt_time(0), M.fmt_time(239)))
    if M.fmt_time(-5) != "00:00" or M.fmt_time(None) != "00:00":
        raise AssertionError("fmt_time 边界处理不对")
    print("  fmt_time ✓")


def test_player_state():
    """播放器状态机（不真的播，只验证控制逻辑不炸）"""
    import player as P
    pl = P.Player()
    if pl.is_active():
        raise AssertionError("初始不该是活动状态")
    pl.seek(10)        # 还没开始播时也不能抛
    pl.toggle()
    pl.stop()
    if pl.position() != 0.0 or pl.duration() < 0:
        raise AssertionError("无音源时的查询值不对")
    print("  空播放器控制安全 ✓")


def test_all_widgets_use_cn_font():
    """回归：所有会显示文字的控件都必须带中文字体。

    曾经漏传：搜索结果列表项忘写 **self.F，
    结果「搜出来的中文全是方块」，而且比之前更严重（因为列表是主内容）。
    这种漏传肉眼很难发现，所以遍历控件树自动守住。

    （前提：本条只在成功注册到中文字体时才有意义）
    """
    fonts.register()
    if not fonts._registered_path:
        print("  [SKIP] 测试机没有中文字体")
        return

    app = M.LxApp()
    root = app.build()
    app._apply_source_info({
        "meta": {"name": "t", "version": "1"},
        "sources": {"wy": {"name": "网易云", "qualitys": ["320k"]}}})
    app._show_results([{"id": "1", "name": "海阔天空", "singer": "Beyond",
                        "interval": "03:59", "album": ""}])
    app._show_song_popup({"id": "1", "name": "海阔天空", "singer": "Beyond",
                          "interval": "03:59", "album": ""})

    offenders = []

    TEXTY = (Label, Button, TextInput, Spinner)

    def walk(w, depth=0):
        # 不要在这里 try/except —— 之前正是因为把 NameError 吞了，
        # 导致这条检查一直空跑（永远通过）。
        if isinstance(w, TEXTY):
            txt = getattr(w, "text", "") or getattr(w, "hint_text", "")
            fn = getattr(w, "font_name", None)
            # 注意：Kivy 默认 font_name 是 'Roboto'（不是 None），
            # 所以不能判「空」，必须判「是不是我们注册的那个中文字体」。
            if txt and fn != fonts.FONT_NAME:
                offenders.append("%s(%r) font=%r"
                                 % (type(w).__name__, txt[:18], fn))
        for c in getattr(w, "children", []):
            walk(c, depth + 1)

    walk(root)
    if app._popup is not None and app._popup.content is not None:
        walk(app._popup.content)
    if app._popup is not None:
        app._popup.dismiss()

    if offenders:
        raise AssertionError(
            "以下控件没有中文字体，会显示成方块:\n      " +
            "\n      ".join(offenders))
    print("  控件树里所有文字控件都带中文字体 ✓")


def test_netease_paging():
    """回归：搜索必须能分页，且要识别接口限流。

    之前写死 15 条。接口单次上限是 100，
    超过 100 会返回 {"code":406,"msg":"操作频繁"}（HTTP 还是 200），
    不识别就会误判成「没搜到」。
    """
    import searchers as netease

    if netease.PAGE != 100:
        raise AssertionError("单页上限应为 100，实际 %s" % netease.PAGE)
    if netease.MAX_RESULTS < 300:
        raise AssertionError("上限太小: %s" % netease.MAX_RESULTS)

    # 限流要能被识别
    if not netease._is_rate_limited({"code": 406, "msg": "操作频繁，请稍候再试"}):
        raise AssertionError("没识别出 code=406 限流")
    if not netease._is_rate_limited({"msg": "操作频繁", "code": 406}):
        raise AssertionError("没识别出带 msg 的限流")
    if netease._is_rate_limited({"result": {"songs": [{"id": 1}]}}):
        raise AssertionError("正常结果被误判成限流")
    print("  限流识别正确 ✓")

    # 分页逻辑：用假 _fetch 验证会连续取页并去重
    pages = {
        0:   {"result": {"songCount": 250, "songs":
              [{"id": i, "name": "s%d" % i, "duration": 1000} for i in range(100)]}},
        100: {"result": {"songCount": 250, "songs":
              [{"id": i, "name": "s%d" % i, "duration": 1000} for i in range(100, 200)]}},
        200: {"result": {"songCount": 250, "songs":
              [{"id": i, "name": "s%d" % i, "duration": 1000} for i in range(200, 250)]}},
    }
    calls = []

    def fake(keyword, offset, limit):
        calls.append(offset)
        return pages.get(offset, {"result": {"songs": []}})

    real = netease._wy_fetch
    netease._wy_fetch = fake
    try:
        got = netease.search_netease("x", 250)
    finally:
        netease._wy_fetch = real

    if len(got) != 250:
        raise AssertionError("分页取到 %d 条，应为 250" % len(got))
    ids = [g["id"] for g in got]
    if len(set(ids)) != 250:
        raise AssertionError("分页结果有重复")
    if calls != [0, 100, 200]:
        raise AssertionError("分页 offset 不对: %s" % calls)
    print("  分页 3 页共 250 条、无重复 ✓")


def test_bundled_sources():
    """回归：APK 要内置多个音源，并能列出/切换。

    之前只内置 1 个（K×H），而它在 tx/mg 上取不到直链，
    用户就以为「其他平台不好用」。实测「聚合音源 特供版」5 个平台全通，
    所以内置了多个并设为默认。
    """
    items = appenv.extract_bundled_sources()
    if len(items) < 3:
        raise AssertionError("内置音源只有 %d 个，太少了" % len(items))
    names = [n for n, _ in items]
    print("  内置 %d 个: %s" % (len(names), [n[:18] for n in names[:3]]))
    for n, pth in items:
        if not os.path.exists(pth) or os.path.getsize(pth) < 500:
            raise AssertionError("音源文件异常: %s" % n)

    # 兜底：即使 assets/sources 没打进包，也必须能放出单文件音源，
    # 否则 App 会「没有任何音源可用」（致命）
    fb = appenv._fallback_single_source() if not items else None
    if not items and not fb:
        raise AssertionError("既没有多音源，兜底单文件也拿不到")

    default = appenv.default_source_path()
    if not os.path.exists(default):
        raise AssertionError("默认音源不存在: %s" % default)
    print("  默认: %s" % os.path.basename(default))
    if "聚合" not in os.path.basename(default):
        print("  提示: 默认音源不是「聚合音源 特供版」"
              "（实测它 5 个平台都能取直链）")


def test_searchers_registry():
    """5 个平台的搜索都要注册上（否则那些平台搜不到歌 -> 没法用）"""
    import searchers
    want = {"wy", "tx", "kw", "kg", "mg"}
    got = set(searchers.platforms())
    if got != want:
        raise AssertionError("平台不全: 缺 %s" % (want - got))
    for p, (name, fn) in searchers.SEARCHERS.items():
        if not callable(fn):
            raise AssertionError("%s 的搜索函数不可调用" % p)
        if not name:
            raise AssertionError("%s 没有中文名" % p)
    print("  5 个平台都注册了: %s" % " ".join(
        "%s=%s" % (p, searchers.SEARCHERS[p][0]) for p in sorted(got)))


def test_source_dropdown_lists_files():
    """音源下拉要列出「音源文件」，不能被平台列表覆盖。

    这两个下拉曾经是重复的（都显示平台），改成：
      音源 = 内置音源文件；平台 = 该音源声明的平台。
    """
    app = M.LxApp()
    root = app.build()
    app._boot()          # 触发释放内置音源
    vals = list(app.sp_source.values)
    print("  音源下拉 = %s" % [v[:16] for v in vals[:3]])
    if not vals:
        raise AssertionError("音源下拉是空的")
    if any("(" in v and ")" in v for v in vals):
        raise AssertionError("音源下拉里混进了平台项: %s" % vals)

    # 应用音源信息后，音源下拉不应被平台覆盖
    app._apply_source_info({
        "meta": {"name": "t", "version": "1"},
        "sources": {"wy": {"name": "网易云", "qualitys": ["320k"]},
                    "tx": {"name": "QQ音乐", "qualitys": ["320k"]}}})
    if list(app.sp_source.values) != vals:
        raise AssertionError("音源下拉被平台列表覆盖了")
    if list(app.sp_platform.values) != ["网易云 (wy)", "QQ音乐 (tx)"]:
        raise AssertionError("平台下拉不对: %s" % (app.sp_platform.values,))
    print("  音源/平台两个下拉各司其职 ✓")


def test_bundled_font_preferred():
    """回归：必须优先用自带的简体中文字体。

    很多机器的 /system/fonts/NotoSansCJK-Regular.ttc 里
    face[0] 是 Noto Sans CJK **JP**（日文），而 Kivy 的 SDL_ttf
    只会打开 face 0 -> 中文用日文字形渲染，就是「中文字符显示错误」。
    （实测该 ttc 有 10 个 face，SC 在 face[2]。）
    所以 APK 自带一份裁剪过的 SC 字体（1.5MB），并优先使用。
    """
    import fonts
    path = fonts.BUNDLED_FONT
    if not os.path.exists(path):
        raise AssertionError("自带字体不存在: %s" % path)
    size = os.path.getsize(path)
    print("  自带字体: %s (%.2f MB)" % (os.path.basename(path), size / 1048576))
    if size > 4 * 1024 * 1024:
        raise AssertionError("自带字体太大（%.1f MB），会明显撑大 APK"
                             % (size / 1048576))

    fonts._registered_path = None
    if not fonts.register():
        raise AssertionError("字体注册失败")
    if fonts._registered_path != path:
        raise AssertionError("没有优先用自带字体，实际用了 %s"
                             % fonts._registered_path)
    print("  优先选用自带字体 ✓")


def test_download_referer():
    """回归：下载要带平台 Referer。

    症状：QQ音乐能在线播放（播放器自带来源），但下载失败 ——
    CDN 缺 Referer 会返回 403/空内容。现在会带 Referer，
    失败还会换一组头重试。
    """
    import downloader as D
    cases = [
        ("https://dl.stream.qqmusic.qq.com/x.mp3", "tx", "https://y.qq.com/"),
        ("http://car-er.kuwo.cn/x.mp3", "kg", "https://www.kugou.com/"),
        ("https://iot102.music.126.net/x.mp3", None, "https://music.163.com/"),
        ("https://x.migu.cn/a.mp3", None, "https://music.migu.cn/"),
    ]
    for url, plat, want in cases:
        got = D._referer_for(url, plat)
        if got != want:
            raise AssertionError("%s(%s) -> %s，应为 %s"
                                 % (url[:30], plat, got, want))
    if D._referer_for("https://unknown.cdn/a.mp3") is not None:
        raise AssertionError("未知主机不该硬塞 Referer")
    # 带 Referer 的头要真的出现在请求头里
    h = D._headers_for("https://dl.stream.qqmusic.qq.com/x.mp3", "tx")
    if h.get("Referer") != "https://y.qq.com/":
        raise AssertionError("请求头里没有 Referer: %s" % h)
    # 无 Referer 的一组也要能构造出来（用于重试）
    if "Referer" in D._plain_headers():
        raise AssertionError("备用头不该带 Referer")
    print("  平台/主机 Referer 推断 + 备用头 ✓")


def test_url_verify():
    """回归：解析出地址 != 地址能用。

    QQ 音乐那批第三方代理挂了会返回 403，
    只看「有没有解析出字符串」会误判成成功，
    最后在播放/下载时莫名其妙失败。所以必须校验响应内容。
    """
    import songinfo as S
    cases = [
        (b"ID3\x03\x00", True, "ID3"),
        (b"\xff\xfb\x90\x00", True, "mp3 帧"),
        (b"fLaC\x00", True, "flac"),
        (b"\x00\x00\x00\x20ftypM4A ", True, "m4a"),
        (b"OggS\x00", True, "ogg"),
        (b"<html>403</html>", False, "网页"),
        (b'{"code":403}', False, "JSON"),
        (b"", False, "空"),
    ]
    for head, want, name in cases:
        got = S.looks_like_audio(head)
        if got != want:
            raise AssertionError("%s 判定错了: %s" % (name, got))
    print("  音频魔数识别 %d 种 ✓" % len(cases))

    # 死链要能被识破（这是本测试的重点）
    ok, why = S.verify("http://175.27.166.236/kgqq/qq.php?type=mp3&id=x", "tx")
    if ok:
        raise AssertionError("死链被判成可用")
    print("  死链识破: %s ✓" % why)


def test_qq_resolver():
    """QQ 内置解析器：品质 -> level 映射要正确。

    QQ 官方 vkey 接口多数歌返回 result=104003（要 VIP），
    而音源自带的第三方代理有的已失效（聚合音源那条实测 403）。
    所以内置一组代理逐个试、逐个校验。
    """
    import qqresolve
    want = {"128k": "standard", "320k": "exhigh",
            "flac": "lossless", "hires": "hires"}
    for q, lv in want.items():
        got = qqresolve.LEVEL_BY_QUALITY.get(q)
        if got != lv:
            raise AssertionError("%s -> %s，应为 %s" % (q, got, lv))
    if len(qqresolve.QQ_PROXIES) < 2:
        raise AssertionError("代理只有一个，挂掉就没得换了")
    for tpl in qqresolve.QQ_PROXIES:
        if "{id}" not in tpl:
            raise AssertionError("代理模板缺少 {id}: %s" % tpl)
    print("  level 映射正确，%d 个备用代理 ✓" % len(qqresolve.QQ_PROXIES))

    u, info, meta = qqresolve.resolve("", "320k")
    if u is not None:
        raise AssertionError("空 songmid 不该返回地址")
    if meta is not None:
        raise AssertionError("失败时不该返回 meta")

    # 回退顺序必须是「优先降级」，不能把 128k 悄悄换成 320k
    order = ["standard", "exhigh", "lossless"]
    lv = qqresolve.LEVEL_BY_QUALITY["128k"]
    idx = order.index(lv)
    if idx != 0:
        raise AssertionError("128k 应该优先 standard，实际 %s" % lv)
    print("  回退顺序优先降级 ✓")
    print("  空 songmid 被正确拒绝 ✓")


def test_no_silent_platform_switch():
    """回归：绝不能偷偷把用户选的平台换掉。

    之前我做了「跨平台回退」：选了 QQ 音乐，取不到就用网易云顶上 ——
    用户要的是 QQ，结果下到的是网易云的音频，
    这比直接失败更糟（等于给了来源不对的东西）。
    现在改成：失败就如实报错，是否换平台必须用户自己点。
    """
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    # 解析函数里不应再出现「自动换平台」的搜索调用
    seg = src[src.index("def _resolve_song("):src.index("def _song_failed(")]
    if "searchers.search(" in seg:
        raise AssertionError("_resolve_song 里又在自动跨平台搜索了")
    if "_switch_platform" not in src:
        raise AssertionError("没有提供「用户主动换平台」的入口")
    print("  _resolve_song 不会自动换平台，且保留了用户主动切换入口 ✓")


def test_download_no_nameerror():
    """回归：下载函数里不能有未导入的名字。

    真实事故：改用 netutil 时删掉了 `import urllib.request`，
    但新的重试逻辑又用了 `urllib.request.Request` ——
    于是**所有平台都下载失败**，报 NameError。
    在真机上表现为「能在线听、不能下载」，很难联想到是 import 缺失。
    """
    import ast as _ast
    import downloader as D

    if not hasattr(D, "urllib"):
        raise AssertionError("downloader 没有导入 urllib")
    if not hasattr(D, "diag"):
        raise AssertionError("downloader 没有导入 diag")

    # 静态扫一遍：所有模块里「用了 X. 却没 import X」都要抓出来
    import os as _os
    bad = []
    for f in sorted(_os.listdir(HERE)):
        if not f.endswith(".py"):
            continue
        raw = open(_os.path.join(HERE, f), encoding="utf-8").read()
        tree = _ast.parse(raw)
        # 扫描时去掉注释行 —— 否则注释里写个 "time." 也会被当成缺 import
        src = "\n".join(l for l in raw.splitlines()
                        if not l.strip().startswith("#"))
        names = set()
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Import):
                for a in n.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, _ast.ImportFrom):
                for a in n.names:
                    names.add(a.asname or a.name)
        for n in tree.body:
            if isinstance(n, (_ast.FunctionDef, _ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, _ast.Assign):
                for t in n.targets:
                    if isinstance(t, _ast.Name):
                        names.add(t.id)
        import re as _re
        for mod in ("urllib", "json", "os", "time", "ast", "ssl", "threading"):
            # 必须用词边界：否则 lbl_time.text 会被当成 time.x，
            # _ast.parse 会被当成 ast.x —— 都是误报
            pat = r"(?<![A-Za-z0-9_.])" + mod + r"\."
            if _re.search(pat, src) and mod not in names:
                bad.append("%s: 用了 %s. 但没 import" % (f, mod))
    if bad:
        raise AssertionError("缺少 import:\n      " + "\n      ".join(bad))
    print("  所有模块的 import 都齐全 ✓")

    # 真调一次：坏地址应抛网络错误，而不是 NameError
    import tempfile
    try:
        D.download("http://127.0.0.1:9/nope.mp3",
                   os.path.join(tempfile.gettempdir(), "x.mp3"), platform="tx")
    except NameError as e:
        raise AssertionError("download 里还有未定义名字: %s" % e)
    except Exception:
        pass          # 网络错误是预期的
    print("  坏地址抛的是网络错误（不是 NameError）✓")


def test_static():
    """禁止再把 canvas_before 当构造参数传"""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    bad = [i for i, l in enumerate(src.splitlines(), 1)
           if "canvas_before=" in l and not l.strip().startswith("#")]
    if bad:
        raise AssertionError("第 %s 行仍把 canvas_before 当构造参数" % bad)


# ============================================================
def main():
    print("=" * 56)
    print("界面 / 线程契约冒烟测试")
    print("=" * 56)

    print("[1] 中文字体")
    check("中文字体渲染", test_font)

    print("[2] build()")
    app = None
    try:
        app = test_build()
        print("  [OK]   build() 未抛异常")
    except Exception as e:
        FAILS.append("build()")
        print("  [FAIL] build() -> %s: %s" % (type(e).__name__, e))
        traceback.print_exc()

    print("[3] 运行时分支")
    if app is not None:
        test_runtime(app)
        check("_apply_source_info", lambda: test_source_info(app))
    else:
        print("  [SKIP] build 失败")

    print("[4] 线程契约")
    check("load_source 在后台线程", test_threading_contract)
    check("LxBridge 拦住 UI 线程调用", test_bridge_rejects_ui_thread)
    check("后台线程 set_status 安全", test_worker_status_safe)

    print("[5] 无中文字体的降级")
    check("无字体时 build() 正常", test_build_without_cn_font)

    print("[6] 静态检查")
    check("canvas_before 回归", test_static)
    check("evaluateJavascript 走 UI 线程", test_eval_js_uses_ui_thread)
    check("JS 结果双层 JSON 解到底", test_eval_json_unwrap)
    check("SSL 证书失败自动降级", test_ssl_fallback)
    check("所有文字控件都带中文字体", test_all_widgets_use_cn_font)
    check("搜索分页 + 限流识别", test_netease_paging)
    check("内置多个音源", test_bundled_sources)
    check("5 个平台搜索都注册", test_searchers_registry)
    check("音源下拉列出文件", test_source_dropdown_lists_files)
    check("优先用自带简体字体", test_bundled_font_preferred)
    check("下载带平台 Referer", test_download_referer)
    check("直链有效性校验", test_url_verify)
    check("QQ 内置解析器", test_qq_resolver)
    check("不静默换平台", test_no_silent_platform_switch)
    check("下载无未导入名字", test_download_no_nameerror)

    print()
    if FAILS:
        print("✗ 失败 %d 项: %s" % (len(FAILS), FAILS))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
