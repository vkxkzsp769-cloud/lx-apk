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
        # 播放代际号：每次 play/stop 递增。
        # 修复「快速换歌双首重叠」：_build 是投到 Android UI 线程执行的，
        # self._mp 要等它在 UI 线程跑完才被赋值；在那之前用户又点了下一首，
        # stop() 看到的 _mp 还是旧的/None → 两首各自 start()，
        # 而界面只握得住后创建的那个（前面的成孤儿，停都停不掉）。
        self._gen = 0
        # 每个 MediaPlayer 出生时的代际号。key 用 Java 的 hashCode()：
        # pyjnius 每次跨 JNI 都会把同一个 Java 对象包成**新的 Python 代理**，
        # 用代理当 dict 键（默认按 id 哈希）注册时和回调时根本对不上
        # —— 上一版因此把每首歌都拒播了。hashCode() 才是底层对象的稳定身份。
        self._gen_of = {}
        # 登记**所有**还活着的 MediaPlayer。
        # self._mp 只记得住最后一个；一旦某次创建没被接管（代际不对、
        # 或 UI 线程上两条创建任务交错），那个实例就成了孤儿 —— 它会继续
        # 发声，而界面手里没有任何句柄能暂停它（真机现象：两首同时响、
        # 按钮按了没反应、只能杀后台）。
        # ⚠ 只在 _lock 内读写，且 stop() 要「清空 + 释放」原子完成，
        #   不能先取快照再清空 —— 那中间 UI 线程新建的实例会漏网（复现过）。
        self._live = []
        self._lock = threading.RLock()
        # 自检序号：每次 play/stop 递增。UI 线程上的 _build 完成时比一次，
        # 发现自己是"上帝已死"的旧代际就地释放，绝不留到下一首。
        self._sound = None         # 桌面兜底
        self._state = self.IDLE
        self._duration = 0.0
        self._ui = None            # 投到 UI 线程执行用的函数
        self._listeners = []       # 保持 Java 回调对象存活（防 GC 崩溃）
        self._on_event = None
        if IS_ANDROID:
            self._prepare_ui()

    # ---------- 生命周期（内部）----------
    def _kill_all(self, release_fn=None):
        """清空并释放**所有**存活实例 —— 在 _lock 内一次做完

        release_fn(mp) 由调用方给（UI 线程 vs 当前线程）；不给就直接调。
        关键：不能在锁外"先取快照、再清空"，那中间的窗口会让 UI 线程刚
        创建好的实例漏掉 —— 那个实例就会一直发声且无人能停（已复现）。
        """
        with self._lock:
            live = list(self._live)
            self._live = []
            self._gen_of = {}
            self._mp = None
        if not live:
            return
        for mp in live:
            try:
                (release_fn or self._release_mp)(mp)
            except Exception:
                log_exc("_kill_all 释放")

    @staticmethod
    def _release_mp(mp):
        """stop → reset → release，每一步都要各自 try

        MediaPlayer 在错误状态下 stop()/reset() 会抛 IllegalStateException，
        三个动作绑在一个 try 里的话，第一个抛了后面 release 就不执行 ——
        实例既没停也没释放，正是「杀后台才能安静」的来源。
        """
        for fn in ("stop", "reset", "release"):
            try:
                getattr(mp, fn)()
            except Exception:
                pass

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
        """停掉一切。**原子**：递增代际 → 清空登记 → 释放全部

        顺序很重要：
          1. 先递增 _gen —— 让所有"正在 UI 线程排队创建"的实例立刻作废，
             它们跑起来时会自检并自我释放；
          2. 再在锁内一次性摘走 _live 并释放 —— 不能分两步，
             中间窗口会让刚创建的实例漏网（复现过，会一直发声）。
        """
        try:
            self._gen += 1            # 先作废在途的创建
            self._abandon_focus()
            ui = self._ui
            # 释放动作必须在 Android UI 线程上做（MediaPlayer 不是线程安全的）
            self._kill_all(release_fn=(lambda mp: ui(lambda: self._release_mp(mp)))
                           if ui is not None else None)
            snd = self._sound
            self._sound = None
            if snd is not None:
                try:
                    snd.stop()
                    snd.unload()
                except Exception:
                    log_exc("player.stop sound")
        except Exception:
            log_exc("player.stop")
        finally:
            self._state = self.IDLE
            self._listeners = []
            self._duration = 0.0

    # ---------- 错误确认 ----------
    def _note_error(self, what, extra, delay=2.5):
        """延后确认「到底是不是真的失败了」。

        真机实测：有些直链会先回调一次 onError(what=-38)，紧接着
        onPrepared 又成功（时长 276s，正常播放）。如果 onError 里立刻
        报「播放失败」，用户看到的就是错误提示闪一下、随即变成正在播放 ——
        像出了 bug。所以先记下，等一会儿确认还没就绪才真的报。
        """
        self._error_gen = getattr(self, "_error_gen", 0) + 1
        gen = self._error_gen

        def _confirm():
            if gen != getattr(self, "_error_gen", -1):
                return                      # 期间已经就绪 / 换歌了
            if self._state in (self.PLAYING, self.PAUSED):
                return                      # 已经播上了，虚惊一场
            self._state = self.IDLE
            self._emit("error", "播放器错误(%s/%s)，可能是直链失效或格式不支持"
                       % (what, extra))

        t = threading.Timer(delay, _confirm)
        t.daemon = True
        t.start()

    # ---------- 播放 ----------
    def play(self, url, on_event):
        """开始播放。on_event(kind, payload) 会在「主线程之外」被调用。

        kind: "buffering"(无 payload) / "ready"(时长秒) /
              "error"(文本) / "ended"(无)
        界面记得用 ui() 转回主线程。
        """
        # stop() 内部先递增 _gen，再**同步**把释放任务投到 UI 线程。
        # 之后 _play_android 也往 UI 线程投创建任务 —— 两者都是投递到
        # 同一个串行队列，先投的必定先执行，所以"旧的先停、新的再建"
        # 这个顺序是有保证的。
        self.stop()
        self._gen += 1
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
                    # 代际校验：这实例出生时的那首歌已被取代 → 不上场，自我了断
                    # ⚠ 不能用 mp 本身当 dict 键——pyjnius 每次跨 JNI 都会把同一
                    #   个 Java 对象包成**新的 Python 代理**，默认哈希按代理算，
                    #   注册时和回调时根本对不上（上一版因此把每首都拒播了）。
                    #   Java 的 Object.hashCode() 才是同一底层对象的稳定身份。
                    try:
                        _mk = mp.hashCode()
                    except Exception:
                        _mk = id(mp)
                    with player._lock:
                        my_gen = player._gen_of.get(_mk, -1)
                        stale = (my_gen != player._gen)
                        if stale:
                            try:
                                player._live.remove(mp)
                            except ValueError:
                                pass
                    if stale:
                        player._release_mp(mp)
                        diag("onPrepared 时已过期，释放（gen %s）" % my_gen)
                        return
                    # 作废可能已经排上的错误确认（见 _note_error）
                    player._error_gen = getattr(player, "_error_gen", 0) + 1
                    player._duration = max(0.0, mp.getDuration() / 1000.0)
                    # 最后一道兜底：start **之前**把登记表里除自己以外的
                    # 实例全部停掉。理论上前面几道闸已经保证不会有多余实例，
                    # 但「有声音重叠」是用户最能感知的故障，值得在最热的
                    # 那个点上再花一次 O(n)（n 正常是 0 或 1）。
                    siblings = []
                    with player._lock:
                        for other in player._live:
                            try:
                                same = (other.hashCode() == _mk)
                            except Exception:
                                same = (other is mp)
                            if not same:
                                siblings.append(other)
                    for other in siblings:
                        player._release_mp(other)
                        with player._lock:
                            try:
                                player._live.remove(other)
                            except ValueError:
                                pass
                        diag("启动前清理了多余实例")
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
                # 不立刻报错：见 _note_error 的说明（有些链会先报错再成功）
                player._note_error(what, extra)
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

        my_gen = self._gen        # play() 里刚递增过，这是本次播放的代际

        def _build():
            try:
                # 第一道自检：排到 UI 线程时已经过期 → 连创建都省了
                if self._gen != my_gen:
                    diag("_build 排到时已过期（gen %d != %d），跳过创建"
                         % (my_gen, self._gen))
                    return
                # 先抢音频焦点：Android 会因此打断别的音频流
                # （包括本 App 里万一漏掉的另一个 MediaPlayer）。
                # 这是在应用层之外多一层系统级保证 —— 重叠播放声音
                # 是用户最能感知的故障，值得加这一道。
                try:
                    self._request_focus()
                except Exception:
                    log_exc("request_focus")
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
                # 登记与接管必须在**同一个锁区间**内完成，中间不能让
                # stop() 插进来（否则 stop 拿到不含新实例的快照，
                # 而新实例又已 prepare 完毕开始出声 —— 就是重叠播放）。
                with self._lock:
                    try:
                        self._gen_of[mp.hashCode()] = my_gen
                    except Exception:
                        self._gen_of[id(mp)] = my_gen
                    if self._gen != my_gen:
                        # 锁内自检：确认自己还是当前代际才登记
                        stale = True
                    else:
                        stale = False
                        self._live.append(mp)
                        self._mp = mp
                if stale:
                    self._release_mp(mp)
                    diag("MediaPlayer 创建期间已被取代，已释放")
                    return
                mp.prepareAsync()
                diag("MediaPlayer 已创建并 prepareAsync: %s" % url[:80])
            except Exception as e:
                log_exc("MediaPlayer 创建")
                self._state = self.IDLE
                self._emit("error", "无法开始播放: %s" % e)

        if self._ui is None:
            self._emit("error", "设备不支持播放")
            return
        self._ui(_build)

    def _audio_manager(self):
        """惰性拿 AudioManager（拿不到就返回 None，不影响播放）"""
        am = getattr(self, "_am", False)
        if am is not False:
            return am
        self._am = None
        try:
            from jnius import autoclass
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            self._am = act.getSystemService("audio")
        except Exception:
            log_exc("_audio_manager")
        return self._am

    def _request_focus(self):
        """请求 AUDIOFOCUS_GAIN（音乐类）。

        失败/超时都不拦播放 —— 焦点只是"更礼貌、更干净"，
        真正保证不重叠的仍是上层的一次只允许一个实例。
        """
        am = self._audio_manager()
        if am is None:
            return
        try:
            from jnius import autoclass
            AD = autoclass("android.media.AudioManager")
            listener = self._focus_listener()
            if listener is None:
                return
            am.requestAudioFocus(listener, AD.STREAM_MUSIC, AD.AUDIOFOCUS_GAIN)
            diag("已请求音频焦点")
        except Exception as e:
            diag("请求音频焦点失败（忽略）: %r" % (e,))

    def _focus_listener(self):
        """音频焦点回调对象（保持存活，否则会被 GC 掉导致回调失效）"""
        if getattr(self, "_focus_lst", None) is not None:
            return self._focus_lst
        try:
            from jnius import PythonJavaClass, java_method

            player = self

            class _Focus(PythonJavaClass):
                __javainterfaces__ = [
                    "android/media/AudioManager$OnAudioFocusChangeListener"]
                __javacontext__ = "app"

                @java_method("(I)V")
                def onAudioFocusChange(self, change):
                    # 永久失去焦点 -> 暂停（别硬顶着放，会和别的音频打架）
                    try:
                        if change in (-1, -2):      # LOSS / LOSS_TRANSIENT
                            diag("失去音频焦点 change=%s，暂停" % change)
                            player._pause_from_focus()
                    except Exception:
                        log_exc("onAudioFocusChange")

            self._focus_lst = _Focus()
            return self._focus_lst
        except Exception:
            log_exc("_focus_listener")
            return None

    def _pause_from_focus(self):
        try:
            mp = self._mp
            if mp is not None:
                mp.pause()
                self._state = self.PAUSED
        except Exception:
            log_exc("_pause_from_focus")

    def _abandon_focus(self):
        try:
            am = self._audio_manager()
            lst = getattr(self, "_focus_lst", None)
            if am is not None and lst is not None:
                am.abandonAudioFocus(lst)
        except Exception:
            log_exc("_abandon_focus")

    # ---- 桌面兜底 ----
    def _play_desktop(self, url):
        """桌面没有 MediaPlayer：缓冲成文件再用 SoundLoader 播（仅供测试）"""
        import tempfile

        def _work():
            import downloader
            try:
                # 必须带扩展名：SoundLoader 靠后缀挑解码器，
                # 原来写死成没有后缀的 "lx_test_audio"，桌面兜底基本解不出来
                ext = downloader.guess_ext(url, "") or "mp3"
                path = os.path.join(tempfile.gettempdir(),
                                    "lx_test_audio." + ext)
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
