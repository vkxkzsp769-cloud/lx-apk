"""下载音频 + 直链有效性判断。"""
import os
import re

import netutil
from appenv import log

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


def download(url, dest, on_progress=None):
    """下载到 dest。返回字节数。失败抛异常。

    做三重校验，避免把「网页」当音频存下来：
      1) Content-Type 是 html/json 且体积很小 -> 失败
      2) 总大小 < 10KB -> 失败
      3) 文件头是 <htm 或 { -> 失败
    """
    tmp = dest + ".part"

    try:
        with netutil.urlopen(url,
                             headers={"Accept": "*/*",
                                      "Accept-Encoding": "identity"},
                             timeout=30) as r:
            total = int(r.headers.get("Content-Length") or 0)
            ctype = (r.headers.get("Content-Type") or "").lower()
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

        if os.path.exists(dest):
            os.remove(dest)
        os.rename(tmp, dest)
        return got
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
