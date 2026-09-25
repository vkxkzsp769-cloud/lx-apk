"""歌曲元信息：品质/格式映射，以及直链探测（格式 + 大小）。

关于「格式」的说明
------------------
落雪音源的 musicUrl 契约只接受 quality，**格式由音源内部决定**，
外部不能直接指定。所以这里做的是：
  * 由品质推断格式（320k -> mp3，flac -> flac ...）
  * 探测到直链后用 Content-Type 校正（有些源 128k 会给 m4a）
  * 界面上「格式」是个筛选器：选了 MP3 就只列 mp3 类品质，
    选了 FLAC 就只列无损类品质，而不是凭空要求音源换容器。
"""
import urllib.request

import downloader
import netutil
from appenv import diag, log

# 品质 -> 预期格式
FORMAT_BY_QUALITY = {
    "128k": "mp3", "192k": "mp3", "320k": "mp3",
    "flac": "flac", "flac24bit": "flac", "hires": "flac", "master": "flac",
    "atmos": "m4a", "atmos_plus": "m4a",
}

# 品质展示名
QUALITY_LABEL = {
    "128k": "128k 标准", "192k": "192k 较高", "320k": "320k 高",
    "flac": "FLAC 无损", "flac24bit": "FLAC 24bit", "hires": "Hi-Res",
    "master": "母带", "atmos": "全景声", "atmos_plus": "全景声+",
}

FORMAT_ORDER = ["自动", "MP3", "FLAC"]
MIME_TO_FORMAT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/flac": "flac", "audio/x-flac": "flac",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "m4a",
    "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/ape": "ape", "audio/x-ape": "ape",
}


def format_of(quality):
    return FORMAT_BY_QUALITY.get(quality, "mp3")


def quality_label(q):
    return QUALITY_LABEL.get(q, q)


def filter_qualities(qualities, prefer):
    """按格式偏好筛品质。prefer 取 自动 / MP3 / FLAC"""
    if prefer in (None, "", "自动"):
        return list(qualities)
    want = "mp3" if prefer == "MP3" else "flac"
    picked = [q for q in qualities if format_of(q) == want]
    return picked or list(qualities)   # 筛没了就不过滤，别让用户没法选


def human_size(n):
    if not n or n <= 0:
        return "未知"
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576)
    if n >= 1024:
        return "%.0f KB" % (n / 1024)
    return "%d B" % n


def _from_content_type(ct):
    ct = (ct or "").split(";")[0].strip().lower()
    return MIME_TO_FORMAT.get(ct)


def probe(url, timeout=12):
    """探测直链的格式与大小。返回 {format, size, content_type}

    先试 HEAD；有些服务器不支持 HEAD（405/501），
    再退回 Range: bytes=0-1 的 GET，从 Content-Range 里读总长度。
    """
    info = {"format": None, "size": 0, "content_type": ""}

    # ---- 1) HEAD ----
    try:
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": netutil.UA, "Accept": "*/*"})
        with netutil.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type") or ""
            cl = int(r.headers.get("Content-Length") or 0)
            info["content_type"] = ct
            info["size"] = cl
            info["format"] = _from_content_type(ct)
        if info["size"] and info["format"]:
            return info
    except Exception as e:
        log("HEAD 探测失败，改用 Range:", e)

    # ---- 2) Range GET ----
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": netutil.UA, "Accept": "*/*",
                          "Range": "bytes=0-1"})
        with netutil.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type") or ""
            cr = r.headers.get("Content-Range") or ""
            info["content_type"] = ct or info["content_type"]
            if "/" in cr:
                total = cr.rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    info["size"] = int(total)
            info["format"] = info["format"] or _from_content_type(ct)
    except Exception as e:
        log("Range 探测失败:", e)

    if not info["format"]:
        info["format"] = downloader.guess_ext(url, "")
    diag("探测直链: format=%s size=%s ct=%s"
         % (info["format"], info["size"], info["content_type"]))
    return info
