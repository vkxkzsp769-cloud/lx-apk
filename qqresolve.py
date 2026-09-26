"""QQ 音乐直链：内置解析器（不依赖音源）。

为什么要单独做这个
------------------
音源（.js）各自接了一批第三方 QQ 代理，而**同一时刻有的活、有的死**：
    聚合音源 特供版 -> 175.27.166.236/kgqq/qq.php   实测 403（已死）
    墨澜聚合音源   -> ws.stream.qqmusic.qq.com/...  实测 404（缺 vkey）
而 175.27.166.236/**kgqq1**/qq.php 是活的，能稳定拿到音频
（实测 5/5 首成功，standard=128k mp3 / exhigh=320k mp3 / lossless=FLAC）。

QQ 官方 vkey 接口虽然通（code=0），但多数歌返回
result=104003（需要 VIP），免费渠道拿不到。

所以这里内置一组已知的 QQ 代理，逐个尝试并**逐个校验**：
只有真的取到音频（按魔数判断）才算成功。
代理会失效，所以列成列表 —— 一个挂了还有下一个。
"""
import urllib.request

import netutil
from appenv import diag, log, log_exc
from songinfo import verify

# 品质 -> 代理的 level 参数
LEVEL_BY_QUALITY = {
    "128k": "standard",
    "192k": "standard",
    "320k": "exhigh",
    "flac": "lossless",
    "flac24bit": "hires",
    "hires": "hires",
    "master": "jymaster",
    "atmos": "jymaster",
    "atmos_plus": "jymaster",
}

# 已知的 QQ 代理（按可靠性排序）。{id} 是 songmid，{level} 是上面那个值。
QQ_PROXIES = [
    "http://175.27.166.236/kgqq1/qq.php?type=mp3&id={id}&level={level}",
    "http://175.27.166.236/kgqq/qq.php?type=mp3&id={id}&level={level}",
    "https://music.haitangw.cc/kgqq/qq.php?type=mp3&id={id}&level={level}",
]


def resolve(songmid, quality="320k", timeout=15):
    """尝试拿到这首歌的可用直链。

    返回 (url, level) 或 (None, 最后一次的错误)
    """
    if not songmid:
        return None, "缺少 songmid"

    level = LEVEL_BY_QUALITY.get(quality, "exhigh")
    # 同 level 优先；不行再退到低一级（很多歌没有无损）
    levels = [level]
    for fallback in ("exhigh", "standard"):
        if fallback not in levels:
            levels.append(fallback)

    last = "所有 QQ 代理都不可用"
    for lv in levels:
        for tpl in QQ_PROXIES:
            url = tpl.format(id=songmid, level=lv)
            try:
                req = urllib.request.Request(
                    url, headers={"Accept": "*/*",
                                  "User-Agent": netutil.UA,
                                  "Referer": "https://y.qq.com/"})
                with netutil.urlopen(req, timeout=timeout) as r:
                    r.read(64)          # 先取一点，确认真的是音频
                ok, why = verify(url, "tx", timeout=timeout)
                if ok:
                    diag("QQ 解析成功 level=%s: %s" % (lv, url[:70]))
                    return url, lv
                last = why
                diag("QQ 代理不可用 level=%s %s: %s" % (lv, tpl[:34], why))
            except Exception as e:
                last = str(e)[:60]
                log_exc("QQ 代理 %s" % tpl[:40])

    return None, last
