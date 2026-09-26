"""运行环境：日志、路径、存储权限。

本模块只依赖标准库 + kivy.utils.platform，不创建任何界面对象，
因此可以在 App 启动前安全导入。
"""
import os
import sys
import time
import traceback

from kivy.utils import platform

IS_ANDROID = platform == "android"

# ============================================================
#  日志
# ============================================================
_CRASH_LOG = None


def log(*args):
    """统一日志出口（真机上看 logcat 也能看到 python 输出）"""
    try:
        print("[lx]", *args)
        sys.stdout.flush()
    except Exception:
        pass


_DIAG_LOG = None


def diag(*args):
    """诊断日志。

    真机上用户看不到 logcat，所以关键步骤都写进文件，
    出问题时让用户把 diag.log 发回来即可定位
    （路径: Android/data/com.lxdl.lxdownloader/files/diag.log）
    """
    line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"),
                        " ".join(str(a) for a in args))
    log(line)
    if _DIAG_LOG is None:
        return
    try:
        with open(_DIAG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_exc(where):
    """记录异常。不要再写 except: pass —— 之前状态栏不更新就是因为
    异常被静默吞掉，排查了很久。"""
    log("异常 @ %s" % where)
    tb = traceback.format_exc()
    try:
        print(tb, file=sys.stderr)
    except Exception:
        pass
    _append_crash(tb)


def _append_crash(text):
    global _CRASH_LOG
    if _CRASH_LOG is None:
        return
    try:
        with open(_CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n" + "-" * 60 + "\n")
    except Exception:
        pass


def install_crash_guard():
    """把未捕获异常落盘 + 显示到界面，避免静默闪退"""
    sys.excepthook = _excepthook


def _excepthook(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass
    _append_crash(text)


# ============================================================
#  路径
# ============================================================
def _detect_app_dir():
    """应用私有可写目录（音源、崩溃日志）"""
    if IS_ANDROID:
        try:
            from jnius import autoclass
            ctx = autoclass("org.kivy.android.PythonActivity").mActivity
            base = ctx.getExternalFilesDir(None)
            if base is not None:
                return base.getAbsolutePath()
            return ctx.getFilesDir().getAbsolutePath()
        except Exception:
            log_exc("detect_app_dir")
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _detect_app_dir()
SOURCE_DIR = os.path.join(APP_DIR, "sources")
SOURCE_FILE = os.path.join(SOURCE_DIR, "default.js")
BUILTIN_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "assets", "default_source.js")
PRIVATE_DOWNLOAD_DIR = os.path.join(APP_DIR, "downloads")
_CRASH_LOG = os.path.join(APP_DIR, "crash.log")
_DIAG_LOG = os.path.join(APP_DIR, "diag.log")

for _d in (SOURCE_DIR, PRIVATE_DOWNLOAD_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass


def _detect_public_download_dir():
    """公共下载目录（用户能在「文件管理 → Download」里直接看到）"""
    if not IS_ANDROID:
        return os.path.join(os.path.expanduser("~"), "Downloads", "落雪音源")

    try:
        from jnius import autoclass
        Environment = autoclass("android.os.Environment")
        d = Environment.getExternalStoragePublicDirectory(
            Environment.DIRECTORY_DOWNLOADS)
        if d is not None:
            p = d.getAbsolutePath()
            if p:
                return os.path.join(p, "落雪音源")
    except Exception:
        log_exc("detect_public_download_dir")

    for p in ("/storage/emulated/0/Download", "/sdcard/Download"):
        if os.path.isdir(p):
            return os.path.join(p, "落雪音源")
    return "/storage/emulated/0/Download/落雪音源"


PUBLIC_DOWNLOAD_DIR = _detect_public_download_dir()


def is_writable(path):
    """真正写个文件验证（只看 os.path.isdir 会误判）"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".lx_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def download_dir():
    """每次下载前重新判定，这样用户刚授权就能立刻生效（不用重启 App）"""
    if is_writable(PUBLIC_DOWNLOAD_DIR):
        return PUBLIC_DOWNLOAD_DIR
    return PRIVATE_DOWNLOAD_DIR


def using_public_dir():
    return download_dir() == PUBLIC_DOWNLOAD_DIR


def has_all_files_access():
    """Android 11+ 是否已授予「所有文件访问」"""
    if not IS_ANDROID:
        return True
    try:
        from jnius import autoclass
        if autoclass("android.os.Build$VERSION").SDK_INT < 30:
            return is_writable(PUBLIC_DOWNLOAD_DIR)
        Environment = autoclass("android.os.Environment")
        return bool(Environment.isExternalStorageManager())
    except Exception:
        log_exc("has_all_files_access")
        return False


def request_all_files_access():
    """跳系统「所有文件访问」授权页（Android 11+）

    注意：只在真正需要时调用。放在启动时会把用户甩到设置页，像卡死。
    """
    if not IS_ANDROID:
        return False
    try:
        from jnius import autoclass
        from android.permissions import request_permissions, Permission
        Build = autoclass("android.os.Build$VERSION")
        act = autoclass("org.kivy.android.PythonActivity").mActivity

        if Build.SDK_INT >= 30:
            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            Uri = autoclass("android.net.Uri")
            intent = Intent(
                Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
            intent.setData(Uri.parse("package:" + act.getPackageName()))
            act.startActivity(intent)
            return True
        request_permissions([Permission.WRITE_EXTERNAL_STORAGE,
                             Permission.READ_EXTERNAL_STORAGE])
        return True
    except Exception:
        log_exc("request_all_files_access")
        return False


# ============================================================
#  内置音源
# ============================================================
# 项目自带的音源目录（打包进 APK，也在桌面调试时用）
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "assets", "sources")
# 多个内置音源都释放到这里
BUILTIN_DIR = os.path.join(APP_DIR, "builtin_sources")

# 默认用哪个：聚合音源 特供版 实测 5 个平台全部可取直链
DEFAULT_SOURCE_NAME = "01_聚合音源_特供版.js"


def _copy_file(src, dst):
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data)
    return len(data)


def extract_bundled_sources():
    """把内置的多个音源释放到可写目录。返回 [(显示名, 路径)]。

    音源放在 assets/sources/ 下：
      * 桌面调试时就在项目目录里，直接读
      * 打包进 APK 后由 p4a 收进 assets/private.tar，
        启动时已经解到应用私有目录，所以 <app_dir>/assets/sources 也能读到
    """
    os.makedirs(BUILTIN_DIR, exist_ok=True)

    candidates = [BUNDLED_DIR,
                  os.path.join(APP_DIR, "assets", "sources")]
    out = []
    for d in candidates:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".js"):
                continue
            dst = os.path.join(BUILTIN_DIR, fn)
            try:
                if (not os.path.exists(dst)
                        or os.path.getsize(dst) != os.path.getsize(
                            os.path.join(d, fn))):
                    _copy_file(os.path.join(d, fn), dst)
                out.append((fn, dst))
            except Exception:
                log_exc("释放内置音源 %s" % fn)
        if out:
            break

    log("内置音源 %d 个 -> %s" % (len(out), BUILTIN_DIR))
    return out


def _fallback_single_source():
    """兜底：万一 assets/sources/ 没打进包，至少把 default_source.js 放出来。

    正常情况用不到 —— 但「没有音源可用」是致命的，
    所以这里留一条路，保证 App 永远有源可加载。
    """
    if os.path.exists(SOURCE_FILE) and os.path.getsize(SOURCE_FILE) > 1000:
        return SOURCE_FILE
    for cand in (BUILTIN_SOURCE,
                 os.path.join(APP_DIR, "assets", "default_source.js")):
        try:
            if os.path.exists(cand) and os.path.getsize(cand) > 1000:
                _copy_file(cand, SOURCE_FILE)
                log("兜底音源 -> %s（来自 %s）" % (SOURCE_FILE, cand))
                return SOURCE_FILE
        except Exception:
            log_exc("兜底音源 %s" % cand)
    return None


def default_source_path():
    """默认音源路径（找不到就退回第一个，再不行用兜底单文件）"""
    p = os.path.join(BUILTIN_DIR, DEFAULT_SOURCE_NAME)
    if os.path.exists(p):
        return p
    for fn, path in extract_bundled_sources():
        return path
    fb = _fallback_single_source()
    return fb or SOURCE_FILE


def ensure_source():
    """确保有一个可用音源：释放内置音源，返回默认那个的路径。"""
    items = extract_bundled_sources()
    if not items:
        fb = _fallback_single_source()
        if not fb:
            raise RuntimeError("找不到任何内置音源")
        log("警告: assets/sources 为空，已退回单文件音源")
        return fb
    path = default_source_path()
    log("默认音源:", os.path.basename(path))
    return path


def save_source(text):
    """保存用户选的音源"""
    with open(SOURCE_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    return SOURCE_FILE
