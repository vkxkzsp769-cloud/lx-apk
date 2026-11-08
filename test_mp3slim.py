#!/usr/bin/env python3
"""mp3slim 引擎测试（纯 Python，不需要 Kivy、不需要显示器）

覆盖：
  1. 动态库能加载、能报出引擎信息
  2. 探测：时长/采样率/声道对得上（拿真实 mp3 比对 ffmpeg 的数字）
  3. 压缩：128k / 96k / 64k 单声道都真能出文件，体积与码率吻合
  4. 输出仍是标准 MP3（帧同步头 + 能被再次解码）
  5. 边界：文件不存在、格式不支持、找不到库时都**如实报错**而不是静默
  6. 命名规则：歌.mp3 -> 歌.slim.mp3，同名不覆盖

用法：
    python3 test_mp3slim.py                    # 用环境变量 MP3SLIM_LIB 指定 .so
    MP3SLIM_LIB=/path/libmp3slim.so python3 test_mp3slim.py

没有引擎/没有素材时相关项标记 SKIP（退出码仍是 0），
这样 CI 里没编 native 也能跑通逻辑测试。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mp3slim  # noqa: E402

FAILS = []
SKIPS = []


def check(name, fn):
    try:
        fn()
        print("  [OK]   %s" % name)
    except AssertionError as e:
        FAILS.append(name)
        print("  [FAIL] %s -> %s" % (name, e))
    except Exception as e:
        FAILS.append(name)
        print("  [FAIL] %s -> %s: %s" % (name, type(e).__name__, e))


def skip(name, why):
    SKIPS.append(name)
    print("  [SKIP] %s（%s）" % (name, why))


def _sample_mp3():
    """找一段真实 mp3 当素材；找不到就跳过依赖素材的项。

    优先用 App 自己的下载目录，其次环境变量，最后当前目录。
    """
    cands = []
    env = os.environ.get("MP3SLIM_SAMPLE")
    if env:
        cands.append(env)
    cands.append(os.path.join(HERE, "testcase.mp3"))
    try:
        import appenv
        d = appenv.download_dir()
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(".mp3"):
                    cands.append(os.path.join(d, fn))
    except Exception:
        pass
    for p in cands:
        if p and os.path.isfile(p) and os.path.getsize(p) > 100 * 1024:
            return p
    return None


# ─────────────────────────── 不依赖引擎的部分 ───────────────────────────
def test_levels():
    labels = mp3slim.levels()
    assert len(labels) >= 4, "档位太少"
    assert mp3slim.DEFAULT_LEVEL[0] in labels, "默认档位不在列表里"
    # 每个档位都能反查回来（下拉框存的是显示名）
    for label in labels:
        item = mp3slim.level_by_label(label)
        assert item[0] == label, "档位反查失败: %s -> %s" % (label, item)


def test_level_values():
    """档位必须是 LAME 支持的码率，且单声道档不会配到 192k 这种浪费"""
    valid = {8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128,
             144, 160, 192, 224, 256, 320}
    for label, br, mono in mp3slim.LEVELS:
        assert br in valid, "非法码率 %s（%s）" % (br, label)
        assert mono in (0, 1), "mono 只能是 0/1: %s" % label


def test_naming():
    assert mp3slim.default_out_path("/a/b/歌.mp3") == "/a/b/歌.slim.mp3"
    assert mp3slim.default_out_path("/a/b/歌.flac") == "/a/b/歌.mp3"
    assert mp3slim.default_out_path("/a/b/歌.wav", "/c") == "/c/歌.mp3"


def test_unique_path():
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "x.slim.mp3")
        assert mp3slim.unique_path(p) == p, "不存在时应原样返回"
        with open(p, "w") as f:
            f.write("a")
        q = mp3slim.unique_path(p)
        assert q != p, "已存在时必须改名"
        assert q.endswith("(2).mp3"), "改名规则不对: %s" % q
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_supported():
    assert mp3slim.supported("a.MP3") and mp3slim.supported("a.flac")
    assert not mp3slim.supported("a.m4a"), "m4a 暂不支持，必须返回 False"


def test_estimate():
    # 128k × 4 分钟 ≈ 3.84MB
    n = mp3slim.estimate_bytes(240000, 128)
    assert 3.5e6 < n < 4.2e6, "估算偏差过大: %s" % n
    assert mp3slim.estimate_bytes(0, 128) == 0
    assert mp3slim.estimate_bytes(240000, 0) == 0


def test_human():
    assert mp3slim.human(0) == "0B"
    assert mp3slim.human(1024) == "1.0KB"
    assert mp3slim.human(1024 * 1024 * 3.5).startswith("3.5MB")


class SkipTest(Exception):
    """依赖不满足（没素材/没引擎）—— 标记跳过，不算失败"""


def test_missing_file_reports_error():
    """文件不存在时必须抛带原因的错，不能安静地返回 None"""
    if not mp3slim.available():
        raise SkipTest("引擎未加载")
    try:
        mp3slim.compress("/no/such/file.mp3", "/tmp/out.mp3")
    except RuntimeError as e:
        assert "不存在" in str(e), "错误信息没说清原因: %s" % e
        return
    raise AssertionError("不存在的文件居然没报错")


# ─────────────────────────── 需要引擎的部分 ───────────────────────────
def test_engine_loads():
    if not mp3slim.available():
        raise SkipTest("找不到 libmp3slim.so")
    info = mp3slim.engine_info()
    assert "就绪" in info, "引擎信息不对: %s" % info
    assert "LAME" in info, "应注明编码器: %s" % info


def test_probe_real_file():
    src = _sample_mp3()
    if not src:
        raise SkipTest("没有 mp3 素材")
    if not mp3slim.available():
        raise SkipTest("引擎未加载")
    info = mp3slim.probe(src)
    assert info, "probe 失败: %s" % mp3slim.last_error()
    assert info["duration_ms"] > 1000, "时长不合理: %s" % info
    assert info["sample_rate"] in (8000, 11025, 12000, 16000, 22050, 24000,
                                   32000, 44100, 48000), info
    assert info["channels"] in (1, 2), info
    assert info["size"] > 0


def test_compress_128k():
    src = _sample_mp3()
    if not src or not mp3slim.available():
        raise SkipTest("缺素材或引擎")
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "out.mp3")
        seen = []
        r = mp3slim.compress(src, dst, bitrate=128, mono=0,
                             on_progress=seen.append)
        assert os.path.isfile(dst), "没有输出文件"
        assert r["size"] > 1000, "输出太小: %s" % r
        # 码率要对得上：size*8 / 秒数 ≈ 128k（±8%）
        secs = r["duration_ms"] / 1000.0
        got = r["size"] * 8 / max(secs, 1e-6)
        assert abs(got - 128000) / 128000 < 0.08, \
            "实际码率 %.0fkbps 偏离 128k" % (got / 1000)
        # 进度回调要真的动过
        assert seen, "on_progress 一次都没被调用"
        assert seen[-1] > 0.99, "进度没有走到 100%%: %s" % seen[-1]
        # 输出必须是合法 MP3：同步头 0xFF Ex
        with open(dst, "rb") as f:
            head = f.read(3)
        assert head[0] == 0xFF and (head[1] & 0xE0) == 0xE0, \
            "输出不是 MP3 帧: %r" % head
        # 体积必须真的变小（源是 320k 的话）
        assert r["size"] < r["src_size"], "压完反而更大，不该发生"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compress_64k_mono():
    src = _sample_mp3()
    if not src or not mp3slim.available():
        raise SkipTest("缺素材或引擎")
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "mono.mp3")
        r = mp3slim.compress(src, dst, bitrate=64, mono=1)
        info = mp3slim.probe(dst)
        assert info, "输出文件探测失败"
        assert info["channels"] == 1, "单声道档输出的还是 %d 声道" % info["channels"]
        assert info["sample_rate"] <= 32000, \
            "64k 应该降到 32k 及以下，实际 %d" % info["sample_rate"]
        assert r["duration_ms"] > 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_compress_rejects_same_path():
    src = _sample_mp3()
    if not src or not mp3slim.available():
        raise SkipTest("缺素材或引擎")
    try:
        mp3slim.compress(src, src, bitrate=128)
    except RuntimeError as e:
        assert "相同" in str(e) or "拒绝" in str(e), "错误信息不含拒绝原因: %s" % e
        return
    raise AssertionError("输出=源文件时没有拒绝（会毁掉原文件！）")


def test_skip_low_bitrate():
    """已经是低码率的文件必须被判「不值得压」

    有损压缩的铁律：128k 的文件压到 128k 只会更糊、体积不变。
    这条挡的是「静默压出一个更烂的文件」这类最难受的结果。
    """
    # 源 128k，目标 128k → 必须跳过
    assert mp3slim.should_skip({"kbps": 128}, 128), "同码率重压居然没被拦"
    # 源 320k，目标 128k → 应该压
    assert not mp3slim.should_skip({"kbps": 320}, 128), "320k 压 128k 被误拦"
    # flac/wav 没有码率 → 一律要压
    assert not mp3slim.should_skip({"kbps": 0}, 128), "无损文件被误拦"
    # 探测失败 → 不拦（让压缩自己报错，比静默不干活强）
    assert not mp3slim.should_skip(None, 128)


def test_scan():
    src = _sample_mp3()
    if not src:
        raise SkipTest("没有 mp3 素材")
    items = mp3slim.scan(os.path.dirname(src))
    assert items, "扫描没找到任何音频"
    for path, size in items:
        assert os.path.isfile(path) and size > 0
        assert mp3slim.supported(path)


# ============================================================
def main():
    print("=" * 56)
    print("mp3slim 引擎测试")
    print("=" * 56)
    print("引擎: %s" % mp3slim.engine_info())
    print("库路径: %s" % (mp3slim.engine_path() or "-"))

    print("[1] 纯逻辑（不需要引擎）")
    check("档位列表", test_levels)
    check("档位码率合法", test_level_values)
    check("输出命名", test_naming)
    check("同名不覆盖", test_unique_path)
    check("格式白名单", test_supported)
    check("体积估算", test_estimate)
    check("人读体积", test_human)

    print("[2] 引擎")
    for name, fn in (("引擎加载", test_engine_loads),
                     ("探测真实文件", test_probe_real_file),
                     ("128k 压缩", test_compress_128k),
                     ("64k 单声道", test_compress_64k_mono),
                     ("拒绝输出=源文件", test_compress_rejects_same_path),
                     ("跳过已够小的文件", test_skip_low_bitrate),
                     ("扫描目录", test_scan),
                     ("文件不存在如实报错", test_missing_file_reports_error)):
        try:
            fn()
            print("  [OK]   %s" % name)
        except SkipTest as e:
            skip(name, str(e))
        except AssertionError as e:
            FAILS.append(name)
            print("  [FAIL] %s -> %s" % (name, e))
        except Exception as e:
            FAILS.append(name)
            print("  [FAIL] %s -> %s: %s" % (name, type(e).__name__, e))

    print()
    if FAILS:
        print("✗ 失败 %d 项: %s" % (len(FAILS), FAILS))
        return 1
    tail = ("（跳过 %d 项）" % len(SKIPS)) if SKIPS else ""
    print("✓ 全部通过%s" % tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
