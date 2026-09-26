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


# 各音频容器的「魔数」特征
def looks_like_audio(head):
    """判断开头这几十字节是不是音频（而不是网页/JSON/错误页）"""
    if not head:
        return False
    if head[:3] == b"ID3":                       # mp3（带 ID3 标签）
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True                              # mp3 帧同步
    if head[:4] == b"fLaC":                      # flac
        return True
    if head[:4] == b"OggS":                      # ogg
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    if head[4:8] == b"ftyp":                     # m4a / mp4
        return True
    if head[:4] == b"MAC ":                      # ape
        return True
    return False


def looks_like_error_page(head):
    low = head[:512].lstrip().lower()
    return low.startswith(b"<htm") or low.startswith(b"<!do") \
        or low.startswith(b"{") or low.startswith(b"<?xml")


def verify(url, platform=None, timeout=12):
    """确认这个直链「真的」能取到音频。

    为什么要单独做这一步：
      音源返回 200 只是「解析出了地址」，地址本身可能是死的
      （例如第三方代理挂了会返回 403，QQ 音乐那批就是这样）。
      只看能不能解析出字符串会误判成成功，最后在播放/下载时才炸，
      用户看到的就是莫名其妙的失败。
    返回 (ok, 说明)
    """
    import downloader
    try:
        headers = {"Accept": "*/*", "Accept-Encoding": "identity",
                   "Range": "bytes=0-2047"}
        ref = downloader._referer_for(url, platform)
        if ref:
            headers["Referer"] = ref
        req = urllib.request.Request(url, headers=headers)
        with netutil.urlopen(req, timeout=timeout) as r:
            head = r.read(2048)
            ct = (r.headers.get("Content-Type") or "").lower()
        if not head:
            return False, "空响应"
        if looks_like_audio(head):
            return True, ""
        if looks_like_error_page(head):
            return False, "返回的是网页而不是音频"
        if "html" in ct or "json" in ct:
            return False, "返回的是 %s" % ct
        return False, "响应不是音频（%s）" % (ct or "无 Content-Type")
    except Exception as e:
        msg = str(e)
        if "403" in msg:
            return False, "403 拒绝访问（第三方接口多半已失效）"
        if "404" in msg:
            return False, "404 地址不存在"
        return False, msg[:60]


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
