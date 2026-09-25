#!/usr/bin/env python3
"""界面冒烟测试（无需真机、无需显示器）

用 SDL 的 offscreen 驱动把 Kivy 界面真正跑起来，验证：
  1. build() 不抛异常（历史上 canvas_before 当构造参数传导致启动即崩）
  2. 结果列表 / 状态栏 / 进度条等运行时分支不抛异常
  3. 中文字体真的能渲染出汉字（而不是方块）

用法:
    pip install "kivy[base]==2.3.0"
    python test_ui.py

退出码 0 = 全部通过。
"""
import os
import sys
import threading
import time
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
from kivy.core.window import Window  # noqa: E402
from kivy.core.text import Label as CoreLabel  # noqa: E402
from kivy.clock import Clock  # noqa: E402

import main as M  # noqa: E402

M.IS_ANDROID = False
FAILS = []


def check(name, fn):
    try:
        fn()
        print("  [OK]   %s" % name)
    except Exception as e:
        FAILS.append(name)
        print("  [FAIL] %s -> %s: %s" % (name, type(e).__name__, e))
        traceback.print_exc()


def test_font():
    """中文字体必须真的能画出汉字。

    判据：font_size=40 时「海阔天空」宽度应接近 4*40=160px。
    若字体没有中文字形，Kivy 会退化成很窄的缺字框（实测约 72px）。
    """
    name = M.register_cn_font()
    print("  注册字体: %s (%s)" % (name, M.CN_FONT_PATH or "未找到"))

    def width_of(text, font):
        lb = CoreLabel(text=text, font_size=40, font_name=font)
        lb.refresh()
        return lb.texture.size[0]

    if name is None:
        print("  [SKIP] 系统没有中文字体（真机上一般都有）")
        return
    w = width_of("海阔天空", name)
    print("  中文字体下「海阔天空」宽度 = %dpx (期望 ≈160)" % w)
    if w < 120:
        raise AssertionError("中文宽度只有 %dpx，字体可能不含中文字形" % w)


def test_build():
    app = M.DownloaderApp()
    root = app.build()
    print("  build() 控件数 = %d, 字体 = %s" % (len(root.children), app.cn_font))
    return app


def test_runtime(app):
    check("_show_results(3 首)", lambda: app._show_results([
        {"id": "1", "name": "海阔天空", "singer": "Beyond",
         "interval": "03:59", "platform": "wy"},
        {"id": "2", "name": "晴天", "singer": "周杰伦",
         "interval": "04:29", "platform": "wy"},
        {"id": "3", "name": "光辉岁月 (Live)", "singer": "黄家驹、Beyond",
         "interval": "05:12", "platform": "wy"},
    ]))
    check("_show_results(空)", lambda: app._show_results([]))
    for t in ("搜索中: 海阔天空", "找到 3 首，点一首开始下载",
              "下载中 海阔天空: 45.2% (1.2/2.7 MB)",
              "✓ 完成: 海阔天空.flac (43.1 MB)\n保存于 /storage/emulated/0/Download/落雪音源",
              "取直链失败: 该歌曲可能版权受限", "出错了: 测试"):
        check("set_status(%s...)" % t[:16], lambda t=t: app.set_status(t))
    check("_update_prog", lambda: app._update_prog(1200000, 2700000, "海阔天空"))
    check("_done", lambda: app._done(
        "/storage/emulated/0/Download/落雪音源/x.flac", 43100000))
    check("get_download_dir", lambda: print("         -> %s" % M.get_download_dir()))


def test_threading_contract():
    """回归：加载音源必须在后台线程跑。

    WebViewEngine.load_source 内部要同步等 JS 结果（_eval_sync），
    在主线程调用会把 Kivy 事件循环堵死 —— 这正是「音源一直加载不出来」
    和历史上「点搜索闪退」的根因。
    """
    app = M.DownloaderApp()
    app.build()

    rec = {}

    class FakeEngine:
        def load_source(self, code):
            rec["thread"] = threading.current_thread().name
            rec["is_main"] = (threading.current_thread()
                              is threading.main_thread())
            return True, {
                "meta": {"name": "测试音源", "version": "9.9"},
                "sources": {"wy": {"name": "网易云", "qualitys": ["320k"]}},
            }

    app.engine = FakeEngine()

    tmp = os.path.join(HERE, "_threadtest.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("// 占位音源\n")

    done = threading.Event()
    inner = app.load_source

    def wrapped(path):
        try:
            inner(path)
        finally:
            done.set()

    app.load_source = wrapped
    app.load_source_async(tmp)
    ok = done.wait(10)
    try:
        os.remove(tmp)
    except OSError:
        pass

    if not ok:
        raise AssertionError("load_source 10 秒内没跑完")
    print("  实际执行线程: %s (主线程=%s)"
          % (rec.get("thread"), rec.get("is_main")))
    if rec.get("is_main"):
        raise AssertionError("load_source 在主线程执行了！会堵死事件循环")

    # 让 Clock 回调跑一下，确认平台列表真的刷新了
    try:
        Clock.tick()
    except Exception:
        pass
    print("  平台下拉 = %s" % (app.sp_source.values,))
    if app.sp_source.values != ["网易云 (wy)"]:
        raise AssertionError("平台列表没刷新: %s" % (app.sp_source.values,))


def test_worker_status_safe():
    """回归：从后台线程调 set_status 不能抛异常。"""
    app = M.DownloaderApp()
    app.build()
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
        raise AssertionError("后台线程调用 set_status 抛异常: %s" % err[0])
    try:
        Clock.tick()
    except Exception:
        pass
    print("  状态栏 = %r" % app.status.text)


def test_no_invalid_kwargs():
    """回归：canvas_before 不是 Kivy 属性，当构造参数传会导致启动即崩。"""
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        st = line.strip()
        if st.startswith("#"):
            continue
        if "canvas_before=" in st:
            bad.append(i)
    if bad:
        raise AssertionError("第 %s 行仍把 canvas_before 当构造参数传" % bad)
    print("  [OK]   没有把 canvas_before 当构造参数传")


def main():
    print("=" * 56)
    print("界面冒烟测试")
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
    else:
        print("  [SKIP] build 失败")

    print("[4] 线程契约（音源加载不能在主线程）")
    check("load_source 在后台线程执行", test_threading_contract)
    check("后台线程 set_status 安全", test_worker_status_safe)

    print("[5] 静态检查")
    check("canvas_before 回归", test_no_invalid_kwargs)

    print()
    if FAILS:
        print("✗ 失败 %d 项: %s" % (len(FAILS), FAILS))
        return 1
    print("✓ 全部通过")
    if app is not None:
        Clock.schedule_once(lambda dt: app.stop(), 0.1)
        app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
