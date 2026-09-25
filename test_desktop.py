#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌面链路测试：验证「搜索 → 取直链 → 下载」这条链路，
不需要安装 Kivy，也不需要 Android。

目的是在把代码传到 GitHub 编译 APK 之前，
先确认音源能加载、搜索能用、直链能下 —— 免得等云端编译十几分钟才发现逻辑问题。

用法:
    python3 test_desktop.py "海阔天空"
"""
import os
import re
import sys
import json
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

APP_DIR = HERE
SCRIPT_DIR = os.path.join(APP_DIR, "sources")
DOWNLOAD_DIR = os.path.join(APP_DIR, "downloads")
os.makedirs(SCRIPT_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DEFAULT_SCRIPT = os.path.join(SCRIPT_DIR, "default.js")

RESTRICTED_PATTERNS = ("music.163.com/song/media/outer/url", "/404", "404.mp3")


def is_restricted(url):
    if not url:
        return True
    low = url.lower()
    return any(p.lower() in low for p in RESTRICTED_PATTERNS)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s)
    return (re.sub(r"\s+", " ", s).strip(" .") or "unknown")[:120]


def guess_ext(url, quality):
    m = re.search(r"\.(mp3|flac|m4a|ape|wav)(?:\?|$)", url, re.I)
    if m:
        return m.group(1).lower()
    return {"flac": "flac", "flac24bit": "flac", "hires": "flac",
            "master": "flac"}.get(quality, "mp3")


UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


def search_netease(kw, limit=5):
    url = ("https://music.163.com/api/search/get/web?s=%s&type=1&offset=0&limit=%d"
           % (urllib.parse.quote(kw), limit))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://music.163.com/",
        "Cookie": "appver=8.9.70;"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for s in ((d.get("result") or {}).get("songs") or []):
        dur = int((s.get("duration") or 0) // 1000)
        out.append({
            "id": str(s.get("id")), "name": s.get("name", ""),
            "singer": "、".join(a.get("name", "") for a in (s.get("artists") or [])),
            "album": (s.get("album") or {}).get("name", ""),
            "interval": "%02d:%02d" % (dur // 60, dur % 60),
            "platform": "wy"})
    return out


def download_file(url, dest, referer=None, on_progress=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=30) as r:
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
        os.remove(tmp)
        raise RuntimeError("文件过小，直链无效")
    with open(tmp, "rb") as f:
        magic = f.read(4)
    if magic[:4] == b"<htm" or magic[:1] == b"{":
        os.remove(tmp)
        raise RuntimeError("下载到的是网页，直链失效")
    if os.path.exists(dest):
        os.remove(dest)
    os.rename(tmp, dest)
    return got


def ensure_source():
    if os.path.exists(DEFAULT_SCRIPT) and os.path.getsize(DEFAULT_SCRIPT) > 1000:
        return DEFAULT_SCRIPT
    for cand in (os.path.join(APP_DIR, "assets", "default_source.js"),
                 os.path.join(HERE, "..", "K×H测试 v1.7.17.js")):
        if os.path.exists(cand):
            with open(cand, encoding="utf-8", errors="replace") as f:
                code = f.read()
            with open(DEFAULT_SCRIPT, "w", encoding="utf-8") as f:
                f.write(code)
            return DEFAULT_SCRIPT
    return None


def run(kw, platform_key="wy", quality="320k"):
    print("=" * 62)
    print("  落雪音源 APK · 桌面链路测试")
    print("=" * 62)

    script = ensure_source()
    if not script:
        print("✗ 找不到音源文件"); return 1
    print("音源文件: %s (%d bytes)" % (os.path.basename(script),
                                    os.path.getsize(script)))

    print("\n[1/3] 搜索「%s」..." % kw)
    try:
        songs = search_netease(kw, 5)
    except Exception as e:
        print("  ✗ 搜索失败:", e); return 1
    if not songs:
        print("  ✗ 没搜到"); return 1
    for i, s in enumerate(songs, 1):
        print("  %d. %s — %s  [%s] ID=%s"
              % (i, s["name"], s["singer"], s["interval"], s["id"]))

    print("\n[2/3] 请求直链（多歌曲依次尝试）...")
    try:
        import lx_engine
    except ImportError:
        print("  ✗ 未安装 dukpy，请先 pip install dukpy"); return 1

    eng = lx_engine.LxEngine(script)
    meta, srcs = eng.info()
    print("  音源: %s v%s | 平台: %s"
          % (meta.get("name"), meta.get("version"),
             ",".join(x["source"] for x in srcs)))

    url = song = None
    for cand in songs:
        info = {"id": cand["id"], "name": cand["name"], "singer": cand["singer"],
                "source": platform_key, "meta": {"songId": cand["id"]}}
        for q in [quality, "320k", "128k"]:
            try:
                u = eng.music_url(platform_key, q, info)
                if u and not is_restricted(u):
                    url, quality, song = u, q, cand
                    break
                print("  %s《%s》受限，重试..." % (q, cand["name"]))
            except Exception as e:
                print("  %s《%s》失败: %s" % (q, cand["name"], str(e)[:60]))
        if url:
            break
    if not url:
        print("  ✗ 所有候选都没拿到可用直链"); return 1
    print("  ✓ 直链:", url[:100])
    print("  歌曲:", song["name"], "—", song["singer"], "| 音质:", quality)

    print("\n[3/3] 下载...")
    ext = guess_ext(url, quality)
    base = safe_name("%s - %s" % (song["name"], song["singer"]))
    dest = os.path.join(DOWNLOAD_DIR, "%s.%s" % (base, ext))
    last = [0]
    def prog(got, total):
        pct = got * 100.0 / max(total, 1)
        if pct - last[0] > 25:
            last[0] = pct
            print("  %.0f%%  (%.1f/%.1f MB)" % (pct, got/1048576, total/1048576))
    size = download_file(url, dest, on_progress=prog)
    print("  ✓ 完成: %s (%.1f MB)" % (os.path.basename(dest), size/1048576))

    with open(dest, "rb") as f:
        magic = f.read(4)
    ok = magic[:3] == b"ID3" or magic[:4] == b"fLaC" or magic[:4] == b"OggS"
    print("  文件头: %r -> %s" % (magic, "是音频 ✓" if ok else "不是音频 ✗"))
    print("\n保存于:", DOWNLOAD_DIR)
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    kw = sys.argv[1] if len(sys.argv) > 1 else "海阔天空"
    sys.exit(run(kw))
