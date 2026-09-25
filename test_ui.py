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

    print()
    if FAILS:
        print("✗ 失败 %d 项: %s" % (len(FAILS), FAILS))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
