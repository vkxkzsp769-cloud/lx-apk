"""网易云搜索。

音源本身只提供「按歌曲信息取直链」，不提供搜索，
所以搜索走网易云公开接口拿歌曲 id/name/singer，再交给音源取直链。
"""
import json
import urllib.parse
import urllib.request

from appenv import log, log_exc

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


def _get_json(url, headers=None, timeout=15):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def search(keyword, limit=15):
    """按歌名搜索，返回 [{id,name,singer,album,interval,platform}]"""
    url = ("https://music.163.com/api/search/get/web"
           "?s=%s&type=1&offset=0&limit=%d"
           % (urllib.parse.quote(keyword), limit))
    data = _get_json(url, {"Referer": "https://music.163.com/",
                           "Cookie": "appver=8.9.70;"})

    songs = (data.get("result") or {}).get("songs") or []
    out = []
    for s in songs:
        try:
            dur = int((s.get("duration") or 0) // 1000)
            out.append({
                "id": str(s.get("id")),
                "name": s.get("name") or "",
                "singer": "、".join(a.get("name") or ""
                                    for a in (s.get("artists") or [])),
                "album": (s.get("album") or {}).get("name") or "",
                "interval": "%02d:%02d" % (dur // 60, dur % 60),
                "platform": "wy",
            })
        except Exception:
            log_exc("解析搜索结果项")
    log("搜索 %r 得到 %d 条" % (keyword, len(out)))
    return out
