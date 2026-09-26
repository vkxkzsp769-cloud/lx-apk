"""下载音频 + 直链有效性判断。"""
import os
import re
import urllib.request          # 构造带自定义请求头的 Request

import netutil
from appenv import diag, log   # diag: 把每次尝试的 status/类型写进 diag.log

# 网易云版权受限时会返回这种跳转地址，下下来是网页而不是音频
RESTRICTED_PATTERNS = ("music.163.com/song/media/outer/url", "/404", "404.mp3")

_EXT_BY_QUALITY = {
    "flac": "flac", "flac24bit": "flac", "hires": "flac", "master": "flac",
}


def is_restricted(url):
    if not url:
        return True
    low = url.lower()
    return any(p.lower() in low for p in RESTRICTED_PATTERNS)


def guess_ext(url, quality):
    m = re.search(r"\.(mp3|flac|m4a|ape|wav)(?:\?|$)", url, re.I)
    if m:
        return m.group(1).lower()
    return _EXT_BY_QUALITY.get(quality, "mp3")


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s)
    return (re.sub(r"\s+", " ", s).strip(" .") or "unknown")[:120]


def unique_path(dest):
    """目标已存在就换个名字，绝不覆盖。

    为什么不能直接覆盖（真机实测两轮才定位到）：
      1) 先 os.remove(dest) 再 rename —— os.path.exists(dest) 返回 True，
         remove 却抛 FileNotFoundError（Android FUSE 看到的是 MediaStore
         视图，真实文件系统里没有）。
      2) 改成 os.replace(tmp, dest) —— 又变成
         OSError [Errno 1] Operation not permitted，因为这个名字的文件
         是当前安装「改不动」的（其他应用/旧安装留下、或权限范围之外）。
    两次都表现成「进度条走到 100% 然后整个下载作废」。
    下载器本来也不该悄悄盖掉用户已有的文件，所以改成自动改名。
    """
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    for i in range(1, 1000):
        cand = "%s (%d)%s" % (base, i, ext)
        if not os.path.exists(cand):
            return cand
    return "%s (copy)%s" % (base, ext)


# 各平台 CDN 大多会校验 Referer，缺了会返回 403/空内容。
# 典型症状：能在线播放（播放器自带来源），但下载失败。
PLATFORM_REFERER = {
    "wy": "https://music.163.com/",
    "tx": "https://y.qq.com/",
    "kw": "https://www.kuwo.cn/",
    "kg": "https://www.kugou.com/",
    "mg": "https://music.migu.cn/",
}

# 万一调用方没传平台，按主机名推断
HOST_REFERER = (
    ("qqmusic", "https://y.qq.com/"),
    ("y.qq.com", "https://y.qq.com/"),
    ("kugou", "https://www.kugou.com/"),
    ("kuwo", "https://www.kuwo.cn/"),
    ("migu", "https://music.migu.cn/"),
    ("126.net", "https://music.163.com/"),
    ("163.com", "https://music.163.com/"),
)


def _referer_for(url, platform=None):
    if platform and platform in PLATFORM_REFERER:
        return PLATFORM_REFERER[platform]
    low = (url or "").lower()
    for key, ref in HOST_REFERER:
        if key in low:
            return ref
    return None


def _headers_for(url, platform=None):
    h = {"User-Agent": netutil.UA, "Accept": "*/*",
         "Accept-Encoding": "identity"}
    ref = _referer_for(url, platform)
    if ref:
        h["Referer"] = ref
        h["Origin"] = ref.rstrip("/")     # 有些 CDN 也看 Origin
    return h


def _plain_headers():
    return {"User-Agent": netutil.UA, "Accept": "*/*",
            "Accept-Encoding": "identity"}


def download(url, dest, on_progress=None, platform=None):
    """下载到 dest，返回字节数。失败抛异常。

    三重校验，避免把「网页」当音频存下来：
      1) Content-Type 是 html/json 且体积很小
      2) 总大小 < 10KB
      3) 文件头是 <htm 或 {

    platform 用于补对应 Referer —— 不少平台 CDN 缺 Referer 就 403，
    表现出来正是「能在线播放但下载失败」。
    """
    tmp = dest + ".part"
    last_err = None

    # 两种头都试：带平台 Referer / 只带 UA（应对不吃 Referer 的 CDN）
    for idx, headers in enumerate((_headers_for(url, platform),
                                   _plain_headers())):
        try:
            req = urllib.request.Request(url, headers=headers)
            with netutil.urlopen(req, timeout=30) as r:
                total = int(r.headers.get("Content-Length") or 0)
                ctype = (r.headers.get("Content-Type") or "").lower()
                diag("下载尝试%d status=%s type=%s len=%s referer=%s"
                     % (idx + 1, getattr(r, "status", "?"), ctype or "-",
                        total or "-", headers.get("Referer", "-")))
                if total and total < 4096 and ("html" in ctype or "json" in ctype):
                    raise RuntimeError("直链已失效（返回网页而非音频）")

                got = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if on_progress and total:
                            on_progress(got, total)

            if got < 10240:
                raise RuntimeError("文件过小（%d 字节），直链无效" % got)

            with open(tmp, "rb") as f:
                magic = f.read(4)
            if magic[:4] == b"<htm" or magic[:1] == b"{":
                raise RuntimeError("下载到的是网页，直链失效")

            # os.replace 在 POSIX 和 Windows 上都是原子覆盖。
            # 但如果目标就是动不了（Android 上目标属于别的应用/旧安装时
            # 会 EPERM），别让整个下载作废 —— 改名保存，并写清日志。
            try:
                os.replace(tmp, dest)
            except OSError as e:
                alt = unique_path(dest)
                log("覆盖 %s 失败(%s)，改存 %s" % (dest, e, alt))
                diag("覆盖失败 %s -> 改为 %s" % (e, alt))
                os.replace(tmp, alt)
            return got

        except Exception as e:
            last_err = e
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if idx == 0:
                diag("下载尝试1失败(%s)，换一组请求头重试" % e)

    raise last_err if last_err else RuntimeError("下载失败")
