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
def ensure_source():
    """确保 SOURCE_FILE 存在；返回路径。首次启动从 APK assets 释放。"""
    if os.path.exists(SOURCE_FILE) and os.path.getsize(SOURCE_FILE) > 1000:
        return SOURCE_FILE

    code = None
    # 1) 优先从 APK assets 读（这是打包进去的那份）
    if IS_ANDROID:
        try:
            from jnius import autoclass
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            stream = act.getAssets().open("default_source.js")
            buf = bytearray()
            chunk = stream.read(8192)
            while chunk != -1 and chunk is not None:
                buf.extend(chunk)
                chunk = stream.read(8192)
            stream.close()
            if buf:
                code = bytes(buf).decode("utf-8", "replace")
        except Exception as e:
            # 这里是「预期内」的失败：p4a 把 source.include_patterns 的文件
            # 打进 assets/private.tar，而不是 APK 根 assets，
            # 所以 getAssets().open() 必然找不到，随后会走下面的本地回退。
            # 不要写进 crash.log，否则每次启动都刷一条假异常。
            log("APK asset 里没有 default_source.js（正常），改用本地副本:", e)

    # 2) 退回项目里的 assets 目录（桌面调试用）
    if not code and os.path.exists(BUILTIN_SOURCE):
        try:
            with open(BUILTIN_SOURCE, encoding="utf-8", errors="replace") as f:
                code = f.read()
        except Exception:
            log_exc("read BUILTIN_SOURCE")

    if not code or len(code) < 200:
        raise RuntimeError("找不到内置音源（APK assets 里没有 default_source.js）")

    with open(SOURCE_FILE, "w", encoding="utf-8") as f:
        f.write(code)
    log("已释放内置音源 %d 字节 -> %s" % (len(code), SOURCE_FILE))
    return SOURCE_FILE


def save_source(text):
    """保存用户选的音源"""
    with open(SOURCE_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    return SOURCE_FILE
