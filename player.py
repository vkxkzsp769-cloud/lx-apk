"""在线播放。

Android 上用 MediaPlayer（真·流式 + 真·可拖动）
---------------------------------------------
之前用 Kivy 的 SoundLoader + 「先整首下完再播」，有两个硬伤：
  1. 不算在线播放 —— 必须等整首下完
  2. seek 基本没用 —— SDL_mixer 的定位不可靠，而且 Kivy 的
     SoundSDL2.seek() 会先校验 self.length，mp3 拿不到长度就直接
     抛 ValueError，被吞掉后表现就是「拖了没反应」

所以改回 Android 自己的 MediaPlayer：
  * setDataSource(url) 直接吃 http(s) 链接，边下边播
  * seekTo(ms) / getCurrentPosition() / getDuration() 都是原生能力
  * 缓冲、解码、格式支持都由系统负责

MediaPlayer 的回调（onPrepared/onError/onCompletion）需要一个带
Looper 的线程，所以整个 MediaPlayer 在 Android UI 线程上创建和操作
（跟 WebView 一个道理，用 @run_on_ui_thread）。

非 Android（桌面跑测试）时退回 Kivy SoundLoader，保证测试能跑。
"""
import os
import threading

from appenv import IS_ANDROID, diag, log, log_exc


class Player:
    IDLE = "idle"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"

    def __init__(self):
        self._mp = None            # android.media.MediaPlayer
        self._sound = None         # 桌面兜底
        self._state = self.IDLE
        self._duration = 0.0
        self._ui = None            # 投到 UI 线程执行用的函数
        self._listeners = []       # 保持 Java 回调对象存活（防 GC 崩溃）
        self._on_event = None
        if IS_ANDROID:
            self._prepare_ui()

    # ---------- 状态查询 ----------
    @property
    def state(self):
        return self._state

    def is_active(self):
        return self._state != self.IDLE

    def position(self):
        try:
            if self._mp is not None:
                return max(0.0, self._mp.getCurrentPosition() / 1000.0)
            if self._sound is not None:
                return float(self._sound.get_pos() or 0.0)
        except Exception:
            pass
        return 0.0

    def duration(self):
        try:
            if self._mp is not None:
                d = self._mp.getDuration() / 1000.0     # 未准备好时返回 -1
                if d > 0:
                    return d
            if self._sound is not None:
                n = float(self._sound.length or 0.0)
                if n > 0:
                    return n
        except Exception:
            pass
        return self._duration

    # ---------- 创建 / 销毁 ----------
    def _prepare_ui(self):
        """准备一个「投到 Android UI 线程执行」的函数"""
        from android.runnable import run_on_ui_thread

        @run_on_ui_thread
        def _run(fn):
            try:
                fn()
            except Exception as e:
                diag("MediaPlayer UI 调用失败: %r" % (e,))
                log_exc("MediaPlayer UI")

        self._ui = _run

    def stop(self):
        try:
            if self._mp is not None and self._ui is not None:
                mp = self._mp
                self._mp = None

                @self._ui
                def _do():
                    try:
                        mp.stop()
                    except Exception:
                        pass
                    try:
                        mp.reset()
                        mp.release()
                    except Exception:
                        pass
            if self._sound is not None:
                self._sound.stop()
                self._sound.unload()
                self._sound = None
        except Exception:
            log_exc("player.stop")
        finally:
            self._state = self.IDLE
            self._listeners = []

    # ---------- 播放 ----------
    def play(self, url, on_event):
        """开始播放。on_event(kind, payload) 会在「主线程之外」被调用。

        kind: "buffering"(无 payload) / "ready"(时长秒) /
              "error"(文本) / "ended"(无)
        界面记得用 ui() 转回主线程。
        """
        self.stop()
        self._on_event = on_event
        self._state = self.BUFFERING
        if IS_ANDROID:
            self._play_android(url)
        else:
            self._play_desktop(url)

    # ---- Android ----
    def _play_android(self, url):
        from jnius import autoclass, PythonJavaClass, java_method, cast

        MediaPlayer = autoclass("android.media.MediaPlayer")
        # Builder 是 AudioAttributes 的「嵌套类」，pyjnius 必须用 $ 写法。
        # 写成 "android.media.AudioAttributes" 再取 .Builder 会报
        #   AttributeError: type object 'AudioAttributes' has no attribute 'Builder'
        AudioAttributesBuilder = autoclass(
            "android.media.AudioAttributes$Builder")
        player = self

        class _Prepared(PythonJavaClass):
            __javainterfaces__ = [
                "android/media/MediaPlayer$OnPreparedListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/media/MediaPlayer;)V")
            def onPrepared(self, mp):
                try:
                    player._duration = max(0.0, mp.getDuration() / 1000.0)
                    mp.start()
                    player._state = player.PLAYING
                    diag("MediaPlayer 就绪，时长=%.1fs" % player._duration)
                    player._emit("ready", player._duration)
                except Exception:
                    log_exc("onPrepared")

        class _Error(PythonJavaClass):
            __javainterfaces__ = [
                "android/media/MediaPlayer$OnErrorListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/media/MediaPlayer;II)Z")
            def onError(self, mp, what, extra):
                diag("MediaPlayer 错误 what=%s extra=%s" % (what, extra))
                player._state = player.IDLE
                player._emit("error", "播放器错误(%s/%s)，可能是直链失效或格式不支持"
                             % (what, extra))
                return True

        class _Done(PythonJavaClass):
            __javainterfaces__ = [
                "android/media/MediaPlayer$OnCompletionListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/media/MediaPlayer;)V")
            def onCompletion(self, mp):
                player._state = player.IDLE
                player._emit("ended", None)

        prep, err, done = _Prepared(), _Error(), _Done()
        self._listeners = [prep, err, done]     # 保活，防 GC

        def _build():
            try:
                mp = MediaPlayer()
                try:
                    b = AudioAttributesBuilder()
                    b.setUsage(1)        # USAGE_MEDIA
                    b.setContentType(2)  # CONTENT_TYPE_MUSIC
                    mp.setAudioAttributes(b.build())
                except Exception as e:
                    # 设置音频属性失败不影响播放，只是没有音频焦点
                    diag("设置 AudioAttributes 失败（忽略）: %r" % (e,))
                mp.setOnPreparedListener(prep)
                mp.setOnErrorListener(err)
                mp.setOnCompletionListener(done)
                mp.setDataSource(url)
                mp.prepareAsync()
                self._mp = mp
                diag("MediaPlayer 已创建并 prepareAsync: %s" % url[:80])
            except Exception as e:
                log_exc("MediaPlayer 创建")
                self._state = self.IDLE
                self._emit("error", "无法开始播放: %s" % e)

        if self._ui is None:
            self._emit("error", "设备不支持播放")
            return
        self._ui(_build)

    # ---- 桌面兜底 ----
    def _play_desktop(self, url):
        """桌面没有 MediaPlayer：缓冲成文件再用 SoundLoader 播（仅供测试）"""
        import tempfile

        def _work():
            import downloader
            try:
                path = os.path.join(tempfile.gettempdir(), "lx_test_audio")
                downloader.download(url, path)
                from kivy.core.audio import SoundLoader
                snd = SoundLoader.load(path)
                if snd is None:
                    self._emit("error", "无法解码")
                    return
                self._sound = snd
                snd.play()
                self._state = self.PLAYING
                self._duration = float(snd.length or 0)
                self._emit("ready", self._duration)
            except Exception as e:
                self._state = self.IDLE
                self._emit("error", str(e))

        threading.Thread(target=_work, daemon=True).start()

    # ---- 控制 ----
    def toggle(self):
        try:
            if self._mp is not None and self._ui is not None:
                mp = self._mp

                def _do():
                    if self._state == self.PLAYING:
                        mp.pause()
                        self._state = self.PAUSED
                    else:
                        mp.start()
                        self._state = self.PLAYING
                self._ui(_do)
            elif self._sound is not None:
                if self._state == self.PLAYING:
                    self._sound.stop()
                    self._state = self.PAUSED
                else:
                    self._sound.play()
                    self._state = self.PLAYING
        except Exception:
            log_exc("player.toggle")

    def seek(self, seconds):
        """跳到指定秒数（真 seek，MediaPlayer 原生支持）"""
        try:
            seconds = max(0.0, float(seconds))
            if self._mp is not None and self._ui is not None:
                mp = self._mp
                ms = int(seconds * 1000)

                def _do():
                    mp.seekTo(ms)
                self._ui(_do)
                diag("seek -> %.1fs" % seconds)
            elif self._sound is not None:
                self._sound.seek(seconds)
        except Exception:
            log_exc("player.seek")

    def _emit(self, kind, payload):
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(kind, payload)
        except Exception:
            log_exc("player 回调")
