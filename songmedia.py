"""封面与歌词获取 —— 只为「正在播放」全屏页服务，纯展示层。

铁律：
  * 不碰直链/下载/播放逻辑，这里只有「查图、查词」两种只读网络请求
  * 每个接口失败都安静兜底（返回 None / 空串），绝不让播放流程炸
  * 缓存到 APP_DIR/cache/covers，换首歌重进不用重新下载
支持矩阵（2026-10 实测）：
  * wy（网易云）：封面 song/detail 的 album.picUrl；歌词 api/song/lyric ✓
  * tx（QQ音乐）：封面按 albumMid 拼 y.gtimg.cn 规则 URL；歌词 fcg_query_lyric_new ✓
  * kw / kg / mg：暂无稳定免费接口 —— 返回 None，界面用占位圆
"""
import json
import os
import re

import netutil
from appenv import APP_DIR, diag, log_exc

COVER_DIR = os.path.join(APP_DIR, "cache", "covers")

_UA = "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36"


def _get(url, referer=None, timeout=10):
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    resp = netutil.urlopen(url, timeout=timeout, headers=headers)
    return resp.read()


# ── 封面 ─────────────────────────────────────────────────
def cover_url(platform, song):
    """返回封面图片 URL；拿不到返回 None"""
    try:
        if platform == "wy":
            sid = song.get("id")
            if not sid:
                return None
            url = ("https://music.163.com/api/song/detail/?id=%s"
                   "&ids=%%5B%s%%5D" % (sid, sid))
            d = json.loads(_get(url, referer="https://music.163.com/").decode("utf-8", "replace"))
            pic = (d.get("songs") or [{}])[0].get("album", {}).get("picUrl")
            if pic:
                # 官方缩略参数：500 见方，够画 512 的圆了
                return pic + "?param=500y500"
        elif platform == "tx":
            am = (song.get("extra") or {}).get("albumMid")
            if am:
                return ("https://y.gtimg.cn/music/photo_new/"
                        "T002R500x500M000%s.jpg" % am)
    except Exception:
        log_exc("cover_url %s" % platform)
    return None


def download_cover(platform, song):
    """下载封面到缓存，返回本地路径或 None（同曲命中缓存零请求）"""
    try:
        plat = platform or (song.get("platform") or "")
        sid = (song.get("extra") or {}).get("songmid") or song.get("id")
        if not sid:
            return None
        fn = re.sub(r"[^A-Za-z0-9_.-]", "_", "%s_%s.jpg" % (plat, sid))
        path = os.path.join(COVER_DIR, fn)
        if os.path.exists(path) and os.path.getsize(path) > 1024:
            return path
        url = cover_url(plat, song)
        if not url:
            return None
        data = _get(url)
        if not data or len(data) < 1024:
            return None
        head = data[:4]
        if head[:3] != b"\xff\xd8\xff" and head[:4] != b"\x89PNG":
            return None                     # 不是 JPEG/PNG 的一律不要（防 403 页）
        if not os.path.isdir(COVER_DIR):
            os.makedirs(COVER_DIR)
        with open(path, "wb") as f:
            f.write(data)
        diag("封面已缓存 %s (%d KB)" % (fn, len(data) // 1024))
        return path
    except Exception:
        log_exc("download_cover %s" % platform)
        return None


# ── 歌词 ─────────────────────────────────────────────────
def lyric_text(platform, song):
    """返回 LRC 原文；拿不到返回空串"""
    try:
        plat = platform or (song.get("platform") or "")
        if plat == "wy" and song.get("id"):
            url = ("https://music.163.com/api/song/lyric?id=%s"
                   "&lv=1&kv=1&tv=-1" % song["id"])
            d = json.loads(_get(url, referer="https://music.163.com/").decode("utf-8", "replace"))
            return ((d.get("lrc") or {}).get("lyric")) or ""
        if plat == "tx":
            mid = (song.get("extra") or {}).get("songmid") or song.get("id")
            if mid:
                url = ("https://c.y.qq.com/lyric/fcgi-bin/"
                       "fcg_query_lyric_new.fcg?songmid=%s"
                       "&format=json&nobase64=1" % mid)
                d = json.loads(_get(url, referer="https://y.qq.com/").decode("utf-8", "replace"))
                txt = d.get("lyric") or ""
                # 该接口部分字段是 unicode 转义串
                if "\\u" in txt:
                    try:
                        txt = txt.encode("utf-8").decode("unicode_escape")
                    except Exception:
                        pass
                return txt
    except Exception:
        log_exc("lyric_text %s" % platform)
    return ""


_LRC_TAG = re.compile(r"\[(\d+):(\d+)\.?(\d*)\]")


def parse_lrc(text):
    """LRC → [(秒, 歌词), ...]（升序，跳过元信息行和空词行）"""
    out = []
    if not text:
        return out
    for line in text.splitlines():
        tags = _LRC_TAG.findall(line)
        if not tags:
            continue
        words = _LRC_TAG.sub("", line).strip()
        if not words:
            continue
        for mm, ss, frac in tags:
            try:
                t = int(mm) * 60 + int(ss)
                if frac:
                    t += int(frac[:3].ljust(3, "0")) / 1000.0
                out.append((t, words))
            except Exception:
                continue
    out.sort()
    return out


def lyric_at(lines, pos_sec):
    """当前应高亮的行号（-1=还没开始）。lines 为 parse_lrc 结果。"""
    idx = -1
    for i, (t, _w) in enumerate(lines):
        if t <= pos_sec + 0.05:
            idx = i
        else:
            break
    return idx
