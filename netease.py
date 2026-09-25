"""网易云搜索（带分页）。

音源本身只提供「按歌曲信息取直链」，不提供搜索，
所以搜索走网易云公开接口拿歌曲 id/name/singer，再交给音源取直链。

关于「数量」
------------
该接口单次最多返回 100 条（limit>100 直接返回空），
但一首热门歌往往有几百条结果，所以要靠 offset 分页多拿几页。
want=None 表示「能拿多少拿多少」（直到取完或到达硬上限）。
"""
import time
import urllib.parse

import netutil
from appenv import log, log_exc

# 接口单次硬上限
PAGE = 100
# 分页之间的间隔：接口对连续请求很敏感，会返回
#   {"code":406,"msg":"操作频繁，请稍候再试"}
# （HTTP 还是 200，但 result 为空 —— 不识别就会以为「没搜到」）
PAGE_DELAY = 0.9
# 被限流时的重试次数与退避基数
RETRIES = 4
BACKOFF = 1.6
# 安全上限：避免「全部」时请求过多、也避免一次塞几千个列表项把手机拖卡
MAX_RESULTS = 500


def _is_rate_limited(data):
    """识别限流。

    接口被限流时 HTTP 仍是 200，但 body 里是:
        {"msg":"操作频繁，请稍候再试","code":406}
    result 为空。若不识别就会误判成「没有搜索结果」。
    """
    if not isinstance(data, dict):
        return False
    if data.get("code") == 406:
        return True
    return bool(data.get("msg")) and not data.get("result")


def _fetch(keyword, offset, limit):
    url = ("https://music.163.com/api/search/get/web"
           "?s=%s&type=1&offset=%d&limit=%d"
           % (urllib.parse.quote(keyword), offset, limit))
    headers = {"Referer": "https://music.163.com/",
               "Cookie": "appver=8.9.70;"}

    data = None
    for attempt in range(RETRIES):
        try:
            data = netutil.get_json(url, headers)
        except Exception as e:
            log_exc("搜索请求异常 offset=%d" % offset)
            data = None

        if data is not None and not _is_rate_limited(data):
            return data

        wait = BACKOFF * (attempt + 1)
        if attempt < RETRIES - 1:
            log("接口限流或异常，%.1fs 后重试 (第 %d 次)"
                % (wait, attempt + 1))
            time.sleep(wait)

    return data if isinstance(data, dict) else {}


def _parse(songs, out):
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


def search(keyword, want=100, on_progress=None):
    """按歌名搜索。

    want: 期望数量；None 表示尽量多拿（上限 MAX_RESULTS）
    on_progress: 可选回调 on_progress(got, total)，用于显示「已获取 x/y」
    返回 [{id,name,singer,album,interval,platform}]
    """
    target = MAX_RESULTS if want in (None, 0, -1) else max(1, int(want))
    target = min(target, MAX_RESULTS)

    out = []
    total = None
    offset = 0
    seen = set()

    page = 0
    while len(out) < target:
        if page:
            time.sleep(PAGE_DELAY)      # 避免被接口限流
        page += 1

        limit = min(PAGE, target - len(out))
        try:
            data = _fetch(keyword, offset, limit)
        except Exception as e:
            log_exc("搜索请求失败 offset=%d" % offset)
            if not out:
                raise
            break

        res = data.get("result") or {}
        songs = res.get("songs") or []
        if total is None:
            total = res.get("songCount")
        if not songs:
            break

        # offset 超过总数后接口会重复返回前面的结果，必须主动收手
        if total is not None and offset >= total:
            break

        fresh = [s for s in songs if str(s.get("id")) not in seen]
        for s in fresh:
            seen.add(str(s.get("id")))
        before = len(out)
        _parse(fresh, out)
        offset += len(songs)

        if on_progress:
            try:
                on_progress(len(out), total)
            except Exception:
                pass

        # 整页都是重复的 / 服务器返回的比请求的少 -> 到底了
        if not fresh or len(songs) < limit or len(out) == before:
            break

    log("搜索 %r: 拿到 %d 条（接口共 %s 条）" % (keyword, len(out), total))
    return out[:target]
