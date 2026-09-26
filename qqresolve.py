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
import os
import time
import urllib.request

import appenv
import netutil
from appenv import diag, log, log_exc
from songinfo import _from_content_type, looks_like_audio

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
#
# 2026-09 实测（大陆网络，songmid=001auUcH4WQs2V，level=exhigh）：
#   kgqq1     -> 206 + audio/mpeg + ID3，**可用**
#   kgqq      -> 403，已死
#   haitangw  -> 403，已死
# 死的两条先留着 —— 这类第三方代理经常「复活」，多一条就多一次机会。
# 但**不能只靠内置**：它们随时可能全部失效，所以有 load_proxies()。
QQ_PROXIES = [
    "http://175.27.166.236/kgqq1/qq.php?type=mp3&id={id}&level={level}",
    "http://175.27.166.236/kgqq/qq.php?type=mp3&id={id}&level={level}",
    "https://music.haitangw.cc/kgqq/qq.php?type=mp3&id={id}&level={level}",
]

# 用户自己的代理列表：一行一个模板（含 {id}，{level} 可选），# 开头是注释。
# 放在应用私有目录，界面上的「代理」按钮可以直接选一个 .txt 放进来 ——
# 这样代理失效时**不用重新编译 APK** 就能换，这是这个文件存在的全部意义。
QQ_PROXY_FILE = os.path.join(appenv.APP_DIR, "qq_proxies.txt")


def load_proxies():
    """内置代理 + 用户自定义（自定义排前面，因为通常更新更及时）"""
    custom = []
    try:
        if os.path.exists(QQ_PROXY_FILE):
            with open(QQ_PROXY_FILE, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "{id}" not in line:
                        log("忽略无效代理（缺少 {id}）: %s" % line[:60])
                        continue
                    if line not in custom:
                        custom.append(line)
    except Exception:
        log_exc("读取 qq_proxies.txt")
    if custom:
        diag("QQ 代理: 自定义 %d 条 + 内置 %d 条"
             % (len(custom), len(QQ_PROXIES)))
    return custom + list(QQ_PROXIES)


def save_proxies(text):
    """保存用户选的代理列表，返回存下来的条数。

    只收「含 {id} 的行」，其余当成注释/噪声丢掉 —— 宁可少存，
    也不要存半条模板进去，那会在请求时才炸。
    """
    lines = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "{id}" in line:
            if line not in lines:
                lines.append(line)
    if not lines:
        raise ValueError("文件里没有可用代理（每行要含 {id}）")
    with open(QQ_PROXY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("已保存 %d 条 QQ 代理 -> %s" % (len(lines), QQ_PROXY_FILE))
    return len(lines)


# 解析结果缓存：同一首歌反复点（换音质、先播放再下载）不必重来。
# 直链是有时效的，所以给了 10 分钟有效期。
_CACHE = {}
_CACHE_TTL = 600


def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    _CACHE.pop(key, None)
    return None


def _probe_once(url, timeout):
    """一次请求同时拿到「是不是音频」和「大小/格式」。

    以前是 r.read(64) 判断 + verify() 再请求一次，等于每个候选发两次请求，
    在证书拦截的网络上每次还要多等一次失败握手，慢得明显。
    现在合并成一次：Range 取前 2KB，既能判魔数也能读 Content-Range。
    """
    req = urllib.request.Request(url, headers={
        "Accept": "*/*", "Accept-Encoding": "identity",
        "User-Agent": netutil.UA, "Referer": "https://y.qq.com/",
        "Range": "bytes=0-2047"})
    with netutil.urlopen(req, timeout=timeout) as r:
        head = r.read(2048)
        ct = r.headers.get("Content-Type") or ""
        size = 0
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                size = int(tail)
        if not size:
            try:
                size = int(r.headers.get("Content-Length") or 0)
            except Exception:
                size = 0
    if not looks_like_audio(head):
        return None, ("返回的不是音频" if head else "空响应")
    return {"format": _from_content_type(ct) or "mp3",
            "size": size, "content_type": ct}, ""


def resolve(songmid, quality="320k", timeout=8):
    """拿到这首歌的可用直链。

    返回 (url, level, meta) 或 (None, 错误, None)
    meta 里带 format/size，省得调用方再探测一次。
    """
    if not songmid:
        return None, "缺少 songmid", None

    level = LEVEL_BY_QUALITY.get(quality, "exhigh")

    # 回退顺序：先请求的音质，再**逐步降级**（省流量、也符合直觉）。
    # 只有实在取不到才考虑升一级 —— 绝不默认把 128k 悄悄换成 320k。
    order = ["standard", "exhigh", "lossless", "hires"]
    idx = order.index(level) if level in order else 1
    levels = [level]
    for lv in reversed(order[:idx]):        # 比它低的，从高到低
        if lv not in levels:
            levels.append(lv)
    for lv in order[idx + 1:]:              # 最后才考虑更高的
        if lv not in levels:
            levels.append(lv)

    # 只认「请求的那个 level」的缓存，不能退而取别的 level ——
    # 否则请求 flac 会命中之前缓存的 320k，等于偷偷降了音质。
    cached = _cache_get((songmid, level))
    if cached:
        diag("QQ 命中缓存 level=%s" % level)
        return cached[0], level, cached[1]

    last = "所有 QQ 代理都不可用"
    proxies = load_proxies()
    for lv in levels:
        for tpl in proxies:
            url = tpl.format(id=songmid, level=lv)
            t0 = time.time()
            try:
                meta, why = _probe_once(url, timeout)
                if meta:
                    diag("QQ 解析成功 level=%s size=%s 耗时%.1fs: %s"
                         % (lv, meta.get("size"), time.time() - t0, url[:60]))
                    _CACHE[(songmid, lv)] = (time.time(), (url, meta))
                    return url, lv, meta
                last = why
                diag("QQ 代理不可用 level=%s %.1fs %s: %s"
                     % (lv, time.time() - t0, tpl[:30], why))
            except Exception as e:
                last = str(e)[:60]
                diag("QQ 代理异常 level=%s %s: %s" % (lv, tpl[:34], last))

    return None, last, None
