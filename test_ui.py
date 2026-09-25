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

    print("[4] 静态检查")
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
