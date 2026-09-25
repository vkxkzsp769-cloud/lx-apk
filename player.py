"""在线播放（缓冲 + 播放 + 进度/拖动）。

为什么先缓冲成文件再播
----------------------
SDL_mixer（Kivy 的 audio_sdl2 后端）需要一个「可 seek」的音源，
直接把 HTTP 流喂给它不可靠：解析 mp3/flac 头就要来回 seek。
所以策略是：
    后台把音频下到缓存文件（边下边报进度）
    -> 用 SoundLoader 打开本地文件
    -> 正常播放 / 拖动进度条
一首 320k 的歌大约 8MB，缓冲几秒，之后拖动进度完全流畅。

对外用回调把状态抛给界面，播放器本身不碰任何 Kivy 控件。
"""
import os
import threading
import time

from appenv import diag, log, log_exc


class Player:
    # 状态
    IDLE = "idle"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError:
            pass
        self._sound = None
        self._path = None
        self._state = self.IDLE
        self._cancel = False
        self._fallback_len = 0.0     # 音源给的时长，SDL 拿不到时用它
        self._lock = threading.Lock()
        try:
            from kivy.core.audio import SoundLoader
            self._loader = SoundLoader
        except Exception:
            self._loader = None
            log_exc("import SoundLoader")

    # ---------- 查询 ----------
    @property
    def state(self):
        return self._state

    def is_active(self):
        return self._state in (self.BUFFERING, self.PLAYING, self.PAUSED)

    def position(self):
        s = self._sound
        if s is None:
            return 0.0
        try:
            return float(s.get_pos() or 0.0)
        except Exception:
            return 0.0

    def duration(self):
        s = self._sound
        if s is not None:
            try:
                n = float(s.length or 0.0)
                if n > 0:
                    return n
            except Exception:
                pass
        return self._fallback_len

    # ---------- 控制 ----------
    def stop(self):
        self._cancel = True
        with self._lock:
            if self._sound is not None:
                try:
                    self._sound.stop()
                    self._sound.unload()
                except Exception:
                    log_exc("player.stop")
                self._sound = None
            self._state = self.IDLE
        self._cleanup_cache()

    def toggle(self):
        s = self._sound
        if s is None:
            return
        try:
            if self._state == self.PLAYING:
                s.stop()                 # SDL2 的 pause 不可靠，用 stop+seek 代替
                self._resume_at = self.position()
                self._state = self.PAUSED
            else:
                pos = getattr(self, "_resume_at", 0.0)
                s.play()
                if pos > 0:
                    s.seek(pos)
                self._state = self.PLAYING
        except Exception:
            log_exc("player.toggle")

    def seek(self, seconds):
        s = self._sound
        if s is None:
            return
        try:
            total = self.duration()
            seconds = max(0.0, min(float(seconds), total or seconds))
            s.seek(seconds)
            self._resume_at = seconds
        except Exception:
            log_exc("player.seek")

    # ---------- 播放 ----------
    def play(self, url, on_event, ext="mp3", fallback_len=0.0):
        """缓冲并播放。

        on_event(kind, payload) 会在后台线程被调用，kind 取:
            "buffering" -> payload 为 0..1 的进度
            "ready"     -> payload 为时长(秒)
            "error"     -> payload 为错误文本
        界面里记得用 ui() 转回主线程。
        """
        self.stop()
        self._cancel = False
        self._fallback_len = float(fallback_len or 0)
        self._state = self.BUFFERING
        t = threading.Thread(target=self._work, args=(url, on_event, ext),
                             name="player", daemon=True)
        t.start()

    def _cache_path(self, ext):
        return os.path.join(self.cache_dir, "playing.%s" % (ext or "mp3"))

    def _cleanup_cache(self):
        p = self._path
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    def _work(self, url, on_event, ext):
        import downloader
        path = self._cache_path(ext)
        try:
            def prog(got, total):
                if self._cancel:
                    raise RuntimeError("已取消")
                if total:
                    on_event("buffering", got / float(total))

            downloader.download(url, path, on_progress=prog)
            if self._cancel:
                return
            self._path = path

            if self._loader is None:
                on_event("error", "设备不支持音频播放")
                return

            sound = self._loader.load(path)
            if sound is None:
                on_event("error", "无法解码该音频（格式可能不受支持）")
                return

            with self._lock:
                self._sound = sound
                self._resume_at = 0.0
            sound.play()
            self._state = self.PLAYING
            dur = self.duration()
            diag("开始播放 %s 时长=%.1fs" % (os.path.basename(path), dur))
            on_event("ready", dur)
        except Exception as e:
            if self._cancel:
                return
            log_exc("player._work")
            self._state = self.IDLE
            on_event("error", str(e))
