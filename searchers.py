"""多平台搜索。

为什么要做这个
--------------
音源（.js）只负责「歌曲信息 -> 直链」，不提供搜索。
之前只实现了网易云搜索，所以另外 4 个平台根本没法用
（搜不到歌，自然「不好用」）。实测这 4 个平台的公开搜索接口都还能用，
所以这里把 5 个平台的搜索补齐：

    wy  网易云   music.163.com/api/search/get/web
    tx  QQ音乐   u.y.qq.com/cgi-bin/musicu.fcg (DoSearchForQQMusicDesktop)
    kw  酷我     search.kuwo.cn/r.s
    kg  酷狗     mobilecdn.kugou.com/api/v3/search/song
    mg  咪咕     app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do

统一返回：
    [{"id", "name", "singer", "album", "interval", "platform", "extra"}]
extra 里放各平台取直链时需要的字段
（如 tx 需要 songmid/strMediaMid，mg 需要 copyrightId）。
"""
import ast
import json
import time
import urllib.parse

import netutil
from appenv import log, log_exc

# 统一的单页上限与节流
PAGE = 100
MAX_RESULTS = 500
DELAY = 0.9
RETRIES = 4
BACKOFF = 1.6


def _mmss(sec):
    try:
        sec = int(sec or 0)
    except Exception:
        sec = 0
    if sec <= 0:
        return "--:--"
    return "%02d:%02d" % (sec // 60, sec % 60)


def _clean(s):
    """去掉 HTML 实体和多余空白"""
    if not s:
        return ""
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&quot;", '"').replace("&#39;", "'")
          .replace("&lt;", "<").replace("&gt;", ">"))
    return " ".join(s.split())


def _get(url, referer=None, timeout=15):
    h = {"Referer": referer} if referer else None
    return netutil.urlopen(url, headers=h, timeout=timeout) \
        .read().decode("utf-8", "replace")


def _json(url, referer=None, timeout=15):
    return json.loads(_get(url, referer, timeout))


def _is_rate_limited(data):
    """网易云限流时 HTTP 仍 200，body 是 {"code":406,"msg":"操作频繁"}"""
    if not isinstance(data, dict):
        return False
    if data.get("code") == 406:
        return True
    return bool(data.get("msg")) and not data.get("result")


# ============================================================
#  网易云
# ============================================================
def _wy_fetch(keyword, offset, limit):
    url = ("https://music.163.com/api/search/get/web"
           "?s=%s&type=1&offset=%d&limit=%d"
           % (urllib.parse.quote(keyword), offset, limit))
    headers = {"Referer": "https://music.163.com/",
               "Cookie": "appver=8.9.70;"}
    data = {}
    for i in range(RETRIES):
        try:
            data = _json(url, "https://music.163.com/")
        except Exception:
            log_exc("网易云搜索请求")
            data = {}
        if data and not _is_rate_limited(data):
            return data
        if i < RETRIES - 1:
            time.sleep(BACKOFF * (i + 1))
    return data


def search_netease(keyword, want=100, on_progress=None):
    target = min(MAX_RESULTS if want in (None, 0, -1) else int(want),
                 MAX_RESULTS)
    out, seen, offset, total, page = [], set(), 0, None, 0
    while len(out) < target:
        if page:
            time.sleep(DELAY)
        page += 1
        n = min(PAGE, target - len(out))
        data = _wy_fetch(keyword, offset, n)
        res = data.get("result") or {}
        songs = res.get("songs") or []
        if total is None:
            total = res.get("songCount")
        if not songs or (total is not None and offset >= total):
            break
        fresh = 0
        for s in songs:
            sid = str(s.get("id"))
            if sid in seen:
                continue
            seen.add(sid)
            fresh += 1
            out.append({
                "id": sid,
                "name": _clean(s.get("name")),
                "singer": "、".join(a.get("name", "")
                                    for a in (s.get("artists") or [])),
                "album": (s.get("album") or {}).get("name", ""),
                "interval": _mmss((s.get("duration") or 0) // 1000),
                "platform": "wy",
                "extra": {},
            })
        offset += len(songs)
        if on_progress:
            on_progress(len(out), total)
        if not fresh or len(songs) < n:
            break
    return out[:target]


# ============================================================
#  QQ 音乐
# ============================================================
def search_qq(keyword, want=100, on_progress=None):
    target = min(MAX_RESULTS if want in (None, 0, -1) else int(want),
                 MAX_RESULTS)
    out, seen, page = [], set(), 1

    while len(out) < target:
        n = min(30, target - len(out))
        req = {"req_1": {
            "module": "music.search.SearchCgiService",
            "method": "DoSearchForQQMusicDesktop",
            "param": {"query": keyword, "num_per_page": n, "page_num": page},
        }}
        url = ("https://u.y.qq.com/cgi-bin/musicu.fcg?format=json&data="
               + urllib.parse.quote(json.dumps(req, ensure_ascii=False)))
        data = None
        for i in range(RETRIES):
            try:
                data = _json(url, "https://y.qq.com/")
                break
            except Exception:
                log_exc("QQ 搜索请求")
                time.sleep(BACKOFF * (i + 1))
        if not data:
            break

        try:
            lst = data["req_1"]["data"]["body"]["song"]["list"]
        except (KeyError, TypeError):
            lst = []
        if not lst:
            break

        fresh = 0
        for s in lst:
            mid = str(s.get("mid") or s.get("songmid") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            fresh += 1
            media_mid = (s.get("file") or {}).get("media_mid") or mid
            album = s.get("album") or {}
            out.append({
                "id": mid,
                "name": _clean(s.get("name") or s.get("title") or ""),
                "singer": "、".join(x.get("name", "")
                                    for x in (s.get("singer") or [])),
                "album": album.get("name", ""),
                "interval": _mmss(s.get("interval")),
                "platform": "tx",
                "extra": {
                    "songmid": mid,
                    "strMediaMid": str(media_mid),
                    "mediaMid": str(media_mid),
                    "albumMid": album.get("mid", ""),
                },
            })
        if on_progress:
            on_progress(len(out), None)
        page += 1
        if not fresh or len(lst) < n:
            break
        time.sleep(DELAY)

    # 主接口全挂时退到老接口（拿不到 media_mid，但至少能搜）
    if not out:
        try:
            fb = _json("https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp"
                       "?w=%s&format=json&p=1&n=%d"
                       % (urllib.parse.quote(keyword), min(30, target)),
                       "https://y.qq.com/")
            for s in ((fb.get("data") or {}).get("song") or {}).get("list") or []:
                mid = str(s.get("songmid") or "")
                if not mid:
                    continue
                out.append({
                    "id": mid,
                    "name": _clean(s.get("songname")),
                    "singer": "、".join(x.get("name", "")
                                        for x in (s.get("singer") or [])),
                    "album": s.get("albumname", ""),
                    "interval": _mmss(s.get("interval")),
                    "platform": "tx",
                    "extra": {"songmid": mid, "strMediaMid": mid,
                              "mediaMid": mid,
                              "albumMid": s.get("albummid", "")},
                })
        except Exception:
            log_exc("QQ 备用搜索")
    return out[:target]


# ============================================================
#  酷我
# ============================================================
def search_kuwo(keyword, want=100, on_progress=None):
    """酷我返回的是「单引号」的类 Python 字面量，不是严格 JSON"""
    target = min(MAX_RESULTS if want in (None, 0, -1) else int(want),
                 MAX_RESULTS)
    out, seen = [], set()
    page = 0
    while len(out) < target:
        url = ("http://search.kuwo.cn/r.s?all=%s&ft=music&itemset=web_2013"
               "&client=kt&pn=%d&rn=30&rformat=json&encoding=utf8"
               % (urllib.parse.quote(keyword), page))
        try:
            txt = _get(url, "http://www.kuwo.cn/")
            data = ast.literal_eval(txt)          # 单引号 -> 用 literal_eval
        except Exception:
            log_exc("酷我搜索")
            break

        items = data.get("abslist") or []
        if not items:
            break
        fresh = 0
        for s in items:
            rid = str(s.get("MUSICRID") or "")     # 形如 MUSIC_321946135
            rid = rid.replace("MUSIC_", "") or str(s.get("DC_TARGETID") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            fresh += 1
            out.append({
                "id": rid,
                "name": _clean(s.get("SONGNAME")),
                "singer": _clean(s.get("ARTIST")),
                "album": _clean(s.get("ALBUM")),
                "interval": _mmss(s.get("DURATION")),
                "platform": "kw",
                "extra": {},
            })
        if on_progress:
            on_progress(len(out), None)
        page += 1
        if not fresh or len(items) < 30:
            break
        time.sleep(DELAY)
    return out[:target]


# ============================================================
#  酷狗
# ============================================================
def search_kugou(keyword, want=100, on_progress=None):
    target = min(MAX_RESULTS if want in (None, 0, -1) else int(want),
                 MAX_RESULTS)
    out, seen = [], set()
    page = 1
    while len(out) < target:
        url = ("http://mobilecdn.kugou.com/api/v3/search/song"
               "?keyword=%s&page=%d&pagesize=30&format=json"
               % (urllib.parse.quote(keyword), page))
        try:
            data = _json(url, "http://m.kugou.com/")
        except Exception:
            log_exc("酷狗搜索")
            break

        items = ((data.get("data") or {}).get("info")) or []
        if not items:
            break
        fresh = 0
        for s in items:
            h = str(s.get("hash") or "")
            if not h or h in seen:
                continue
            seen.add(h)
            fresh += 1
            out.append({
                "id": h,
                "name": _clean(s.get("songname")),
                "singer": _clean(s.get("singername")),
                "album": _clean(s.get("album_name")),
                "interval": _mmss(s.get("duration")),
                "platform": "kg",
                "extra": {
                    "hash": h,
                    "album_id": str(s.get("album_id") or ""),
                    "audio_id": str(s.get("audio_id") or ""),
                },
            })
        if on_progress:
            on_progress(len(out), None)
        page += 1
        if not fresh or len(items) < 30:
            break
        time.sleep(DELAY)
    return out[:target]


# ============================================================
#  咪咕
# ============================================================
def search_migu(keyword, want=100, on_progress=None):
    target = min(MAX_RESULTS if want in (None, 0, -1) else int(want),
                 MAX_RESULTS)
    out, seen = [], set()
    page = 1
    switch = urllib.parse.quote('{"song":1}')
    while len(out) < target:
        url = ("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do"
               "?text=%s&pageNo=%d&pageSize=30&isCopyright=1&sort=1"
               "&searchSwitch=%s"
               % (urllib.parse.quote(keyword), page, switch))
        try:
            data = _json(url, "https://music.migu.cn/")
        except Exception:
            log_exc("咪咕搜索")
            break

        items = ((data.get("songResultData") or {}).get("result")) or []
        if not items:
            break
        fresh = 0
        for s in items:
            cid = str(s.get("copyrightId") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            fresh += 1
            singers = s.get("singers") or s.get("singerList") or []
            if isinstance(singers, list):
                singer = "、".join(x.get("name", "") for x in singers)
            else:
                singer = str(singers)
            albums = s.get("albums") or []
            album = albums[0].get("name", "") if albums else ""
            out.append({
                "id": str(s.get("id") or ""),
                "name": _clean(s.get("name")),
                "singer": _clean(singer),
                "album": _clean(album),
                "interval": _mmss(s.get("duration")),
                "platform": "mg",
                "extra": {"copyrightId": cid},
            })
        if on_progress:
            on_progress(len(out), None)
        page += 1
        if not fresh or len(items) < 30:
            break
        time.sleep(DELAY)
    return out[:target]


# ============================================================
SEARCHERS = {
    "wy": ("网易云", search_netease),
    "tx": ("QQ音乐", search_qq),
    "kw": ("酷我", search_kuwo),
    "kg": ("酷狗", search_kugou),
    "mg": ("咪咕", search_migu),
}


def platforms():
    return list(SEARCHERS.keys())


def search(platform, keyword, want=100, on_progress=None):
    """按平台搜索。平台不支持时抛 KeyError，由调用方处理。"""
    name, fn = SEARCHERS[platform]
    log("搜索 [%s] %r want=%s" % (name, keyword, want))
    return fn(keyword, want, on_progress=on_progress)
