"""mp3slim —— MP3 有损压缩引擎（App 内置版）

为什么不是 ffmpeg：
  Android 应用目录是 noexec，ffmpeg 可执行文件跑不起来；p4a 也没有能用的
  ffmpeg recipe。这里改成「一个自编译的 .so + ctypes 调用」：
    native/mp3slim/mp3slim_core.c  →  libmp3slim.so
      · 解码：dr_mp3 / dr_flac / dr_wav（单头文件，public domain）
      · 编码：LAME 3.100（与 ffmpeg 的 libmp3lame 同一实现）
      · 重采样：自写 24 抽头窗 sinc（只在低码率降采样时启用）
  输出仍是标准 .mp3，任何播放器直接能播。

对外接口（界面只依赖这几个）：
  available()                    引擎是否可用（不可用就别让按钮可点）
  engine_info()                  一行说明，写进状态栏/诊断日志
  probe(path)                    时长/采样率/声道/码率
  levels()                       档位列表 [(显示名, 码率, 单声道)]
  compress(src, dst, ..., cb)    压缩一个文件
"""
import ctypes
import os
import sys
import threading

LIB_NAME = "libmp3slim.so"

# 档位：(显示名, 目标码率 kbps, 是否单声道)
# 顺序即下拉顺序，第一项是默认值 —— 与设置页「音质」的排法一致。
LEVELS = [
    ("128k 标准 · 几乎听不出差别", 128, 0),
    ("96k 省三分之一", 96, 0),
    ("64k 单声道 · 有声书/播客", 64, 1),
    ("48k 语音档 · 极致省空间", 48, 1),
    ("192k 高保真", 192, 0),
    ("32k 语音备忘", 32, 1),
]
DEFAULT_LEVEL = LEVELS[0]

SUPPORTED_EXT = (".mp3", ".flac", ".wav")

_lock = threading.Lock()
_lib = None
_lib_path = ""
_load_error = ""


# ───────────────────────────── 动态库定位 ─────────────────────────────
def _candidate_paths():
    """按优先级给出 libmp3slim.so 的可能位置。

    Android 上 p4a 把它和 libpython 一起放进 nativeLibraryDir
    （系统把 APK 里的 lib/<abi>/ 解到这个目录，或者是直接从 APK 映射）。
    桌面调试时用环境变量或 native/build 下的自编译产物。
    """
    out = []
    env = os.environ.get("MP3SLIM_LIB")
    if env:
        out.append(env)

    # Android：applicationInfo.nativeLibraryDir
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        info = act.getApplicationInfo()
        nld = str(info.nativeLibraryDir)
        if nld:
            out.append(os.path.join(nld, LIB_NAME))
    except Exception:
        pass

    # p4a 的 sdl2 bootstrap 会把这些 .so 同时解到私有目录的 lib/ 下，
    # 有的话优先用（不依赖 nativeLibraryDir 的可得性）
    try:
        import appenv
        base = getattr(appenv, "APP_DIR", "")
        if base:
            for sub in ("lib", "assets/lib"):
                out.append(os.path.join(base, sub, LIB_NAME))
    except Exception:
        pass

    # 桌面/CI 调试：仓库自带的构建产物，或环境变量指过去
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, "native", "build", LIB_NAME))

    # 裸名字：交给系统 linker 的默认搜索路径（nativeLibraryDir 通常已在其中）
    out.append(LIB_NAME)
    return out


def _declare(lib):
    """声明函数原型 —— 不声明的话 ctypes 会把 64 位 size 截成 int。"""
    c = ctypes
    lib.mp3slim_probe.argtypes = [
        c.c_char_p,
        c.POINTER(c.c_longlong), c.POINTER(c.c_int),
        c.POINTER(c.c_int), c.POINTER(c.c_int),
        c.POINTER(c.c_char_p),
    ]
    lib.mp3slim_probe.restype = c.c_int
    lib.mp3slim_run.argtypes = [
        c.c_char_p, c.c_char_p,
        c.c_int, c.c_int, c.c_int,
        c.POINTER(c.c_longlong), c.POINTER(c.c_longlong),
        c.CFUNCTYPE(None, c.c_double, c.c_void_p),
        c.c_void_p,
    ]
    lib.mp3slim_run.restype = c.c_int
    lib.mp3slim_last_error.restype = c.c_char_p
    lib.mp3slim_estimate.argtypes = [c.c_longlong, c.c_int, c.c_int]
    lib.mp3slim_estimate.restype = c.c_longlong
    return lib


def load():
    """加载引擎（幂等）。失败返回 None，原因见 engine_info()。"""
    global _lib, _lib_path, _load_error
    with _lock:
        if _lib is not None:
            return _lib
        errors = []
        for path in _candidate_paths():
            try:
                lib = ctypes.CDLL(path)
                _declare(lib)
                _lib = lib
                _lib_path = path
                _load_error = ""
                return _lib
            except OSError as exc:
                errors.append("%s: %s" % (path, exc))
            except Exception as exc:                      # pragma: no cover
                errors.append("%s: %r" % (path, exc))
        _load_error = " | ".join(errors[-3:]) or "没有找到 " + LIB_NAME
        return None


def available():
    return load() is not None


def engine_info():
    """一行可读的引擎状态（状态栏 / diag.log 用）"""
    lib = load()
    if lib is None:
        return "MP3 压缩引擎不可用（%s）" % _load_error
    return "MP3 压缩引擎就绪（LAME 3.100 · %s）" % _lib_path


def engine_path():
    return _lib_path


# ───────────────────────────── 参数工具 ─────────────────────────────
def levels():
    """给下拉框用的档位显示名列表"""
    return [x[0] for x in LEVELS]


def level_by_label(label):
    for item in LEVELS:
        if item[0] == label:
            return item
    return DEFAULT_LEVEL


def supported(path):
    return str(path).lower().endswith(SUPPORTED_EXT)


def human(n):
    """字节数 → 人看的字符串（界面里到处要用）"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "%.1fGB" % n


def estimate_bytes(duration_ms, bitrate, mono=0):
    """按码率估算输出体积（只是估算，用于列表提示）"""
    lib = load()
    if lib is None:
        # 引擎没起来时用纯算术兜底：码率 × 时长，误差在 2% 以内
        if duration_ms <= 0 or bitrate <= 0:
            return 0
        return int(bitrate * 1000 * (duration_ms / 1000.0) / 8.0)
    return int(lib.mp3slim_estimate(int(duration_ms), int(bitrate), int(mono)))


def probe(path):
    """读音频信息。成功返回 dict，失败返回 None（原因在 last_error()）。"""
    lib = load()
    if lib is None:
        return None
    dur = ctypes.c_longlong(0)
    rate = ctypes.c_int(0)
    ch = ctypes.c_int(0)
    kbps = ctypes.c_int(0)
    codec = ctypes.c_char_p(b"")
    rc = lib.mp3slim_probe(os.fsencode(path), ctypes.byref(dur),
                           ctypes.byref(rate), ctypes.byref(ch),
                           ctypes.byref(kbps), ctypes.byref(codec))
    if rc != 0:
        return None
    return {
        "duration_ms": int(dur.value),
        "sample_rate": int(rate.value),
        "channels": int(ch.value),
        "kbps": int(kbps.value),
        "codec": (codec.value or b"").decode("utf-8", "replace"),
        "size": _file_size(path),
    }


def should_skip(info, bitrate, mono=0):
    """这个文件值得压吗？不值得返回原因字符串，值得返回空串。

    有损压缩的铁律：**已经是低码率的文件再往低压只会更糊**，体积还省不下
    多少（而且原文件本来就已经被削过一遍，二次削的损伤是叠加的）。
    所以源文件码率已经不比目标码率高时，直接跳过并如实告诉用户 ——
    比默默压出一个「更糊但没小多少」的文件强得多。
    """
    if not info:
        return ""                      # 探测失败就别拦，让压缩自己报错
    src = int(info.get("kbps") or 0)
    if src <= 0:
        return ""                      # flac/wav 没有「码率」概念，一律要压
    # 单声道档会影响有效码率，但源是立体声 128k 压成 64k 单声道仍有意义，
    # 所以只跟目标码率本身比，留 5% 容差（编码器本身有帧头开销）
    if src <= int(bitrate) * 1.05:
        return "源文件只有 %dkbps，压到 %dk 只会更糊（跳过）" % (src, bitrate)
    return ""


def last_error():
    lib = load()
    if lib is None:
        return _load_error
    try:
        msg = lib.mp3slim_last_error()
        return (msg or b"").decode("utf-8", "replace") or "未知错误"
    except Exception:
        return "未知错误"


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ───────────────────────────── 压缩 ─────────────────────────────
class Progress(object):
    """进度回调包装。

    C 侧每编一段就回调一次（一首歌上千次），所以这里**只保留最后一次**，
    由调用方自己按帧率去取 —— 避免每帧都往 UI 线程排一个闭包。
    """

    __slots__ = ("frac",)

    def __init__(self):
        self.frac = 0.0

    def __call__(self, frac, _ud):
        try:
            self.frac = float(frac)
        except Exception:
            pass


def compress(src, dst, bitrate=128, mono=0, out_rate=0, on_progress=None,
             poll=0.25):
    """压缩一个文件。成功返回 dict（输出大小/时长/耗时），失败抛 RuntimeError。

    on_progress(frac) 会被周期性地调用（不是每次编码回调都调，
    那样 UI 线程会被刷爆）；它由本函数在**当前线程**里驱动，调用方
    自己负责转回主线程（界面里是 app.ui）。
    """
    lib = load()
    if lib is None:
        raise RuntimeError("MP3 压缩引擎不可用：" + _load_error)

    src = os.fspath(src)
    dst = os.fspath(dst)
    if not os.path.exists(src):
        raise RuntimeError("文件不存在: %s" % os.path.basename(src))
    if os.path.exists(dst) and os.path.samefile(src, dst):
        raise RuntimeError("输出与源文件相同，拒绝覆盖")

    prog = Progress()
    cb = ctypes.CFUNCTYPE(None, ctypes.c_double, ctypes.c_void_p)
    cb_obj = cb(prog)

    size = ctypes.c_longlong(0)
    ms = ctypes.c_longlong(0)
    import time as _time
    t0 = _time.time()

    # 压缩是阻塞调用（一首 4 分钟的歌在手机上约 3~9 秒），
    # 期间用一个旁路线程把 prog.frac 抛给 UI —— 否则进度条全程不动。
    stop = threading.Event()
    last = [0.0]

    def _pump():
        while not stop.is_set():
            frac = prog.frac
            if frac - last[0] >= 0.01 and on_progress is not None:
                last[0] = frac
                try:
                    on_progress(min(frac, 1.0))
                except Exception:
                    pass
            stop.wait(poll)

    pump = None
    if on_progress is not None:
        pump = threading.Thread(target=_pump, name="slim-progress", daemon=True)
        pump.start()

    try:
        rc = lib.mp3slim_run(
            os.fsencode(src), os.fsencode(dst),
            int(bitrate), int(mono), int(out_rate),
            ctypes.byref(size), ctypes.byref(ms), cb_obj, None,
        )
    finally:
        stop.set()
        if pump is not None:
            pump.join(timeout=2.0)

    if rc != 0:
        raise RuntimeError(last_error())
    if on_progress is not None:
        try:
            on_progress(1.0)
        except Exception:
            pass

    out_size = int(size.value) or _file_size(dst)
    info = {
        "path": dst,
        "size": out_size,
        "duration_ms": int(ms.value),
        "elapsed": _time.time() - t0,
        "bitrate": int(bitrate),
        "mono": bool(mono),
        "src_size": _file_size(src),
    }
    return info


def default_out_path(src, out_dir=None):
    """输出文件名：歌.mp3 → 歌.slim.mp3（跟原 mp3slim 一致，绝不覆盖源文件）"""
    base = os.path.basename(os.fspath(src))
    stem, ext = os.path.splitext(base)
    if ext.lower() == ".mp3":
        name = stem + ".slim.mp3"
    else:
        name = stem + ".mp3"
    return os.path.join(out_dir or os.path.dirname(os.fspath(src)), name)


def unique_path(path):
    """同名文件自动加 (2)(3)…，不覆盖（Android 上覆盖还会 EPERM）"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 999):
        cand = "%s(%d)%s" % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
    return path


def scan(folder, limit=500):
    """扫一个目录里的可压缩音频（不递归，够用且快）。

    返回 [(路径, 大小)]，按文件名排序。
    """
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if len(out) >= limit:
            break
        if not supported(name):
            continue
        path = os.path.join(folder, name)
        try:
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= 0:
            continue
        out.append((path, size))
    return out


if __name__ == "__main__":        # 命令行自测（桌面用）
    print(engine_info())
    if len(sys.argv) >= 3:
        src, dst = sys.argv[1], sys.argv[2]
        br = int(sys.argv[3]) if len(sys.argv) > 3 else 128
        print(probe(src))
        print(compress(src, dst, br, 0, 0, on_progress=lambda f: None))
