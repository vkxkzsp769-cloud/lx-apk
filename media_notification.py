"""Process-local Android media controls; not a foreground playback service.

Integration (call all methods from Kivy's thread)::

    media = MediaNotification(on_action)
    media.request_permission()  # after activity is visible; never gates playback
    # in the existing Kivy tick:
    media.poll()
    media.update(song, player.state, player.position(), player.duration())
    # on application shutdown:
    media.close()

``on_action(action)`` runs synchronously in poll(), never a Java callback thread.
Actions: previous, toggle, next, play, pause, stop, seek:<seconds>.
Song is a dict (name/title, singer/artist, album/albumName); times are seconds.
States: idle/stopped (hide), buffering, playing, paused. update(None, ...) hides.
Non-Android (including machines without Kivy) is a safe no-op. This module does
not schedule a Clock event, change playback, start a service, or promise playback
when Android suspends Kivy/the process or kills the app. Polling requires live ticks.
"""
import logging
import math
import threading

try:
    from kivy.utils import platform
except ImportError:
    platform = "unknown"

_LOG = logging.getLogger(__name__)


def _milliseconds(value):
    try:
        number = float(value)
        return min(int(number * 1000), 9223372036854775807) if math.isfinite(number) and number > 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _text(song, *keys):
    for key in keys:
        value = song.get(key)
        if value:
            if isinstance(value, (list, tuple)):
                value = " / ".join(str(v.get("name", "")) if isinstance(v, dict) else str(v) for v in value)
            return str(value)
    return ""


class MediaNotification:
    """Optional notification bridge. Backend/permission failures never stop audio."""

    def __init__(self, on_action):
        self._on_action = on_action
        self._helper = None
        self._closed = False
        self._reported = set()
        self._poller = None
        self._poller_stop = None
        self._poller_interval = 0.25
        if platform == "android":
            try:
                from jnius import autoclass
                activity = autoclass("org.kivy.android.PythonActivity").mActivity
                self._helper = autoclass("com.lxdl.media.MediaNotificationHelper")(activity)
            except Exception:
                self._report("initialization")

    def _report(self, operation):
        if operation not in self._reported:
            self._reported.add(operation)
            _LOG.warning("Media notification %s failed (audio unaffected)", operation, exc_info=True)

    def request_permission(self):
        """Return True if already granted; False for pending/denied/unavailable.

        Android 13+ prompts on the Android UI thread. Explicit calls only: neither
        update() nor poll() prompts after denial. At most one prompt per instance;
        later calls check the current grant (e.g. after changing system settings).
        Playback must not depend on its result.
        """
        if self._closed or self._helper is None:
            return False
        try:
            from jnius import autoclass
            activity = autoclass("org.kivy.android.PythonActivity").mActivity
            return bool(self._helper.requestPermission(activity))
        except Exception:
            self._report("permission")
            return False

    def update(self, song, state, position, duration):
        """Publish metadata/state; position and duration are seconds, not ms."""
        if self._closed or self._helper is None:
            return
        try:
            metadata = song if isinstance(song, dict) else {}
            title = _text(metadata, "name", "title") or "落雪音乐"
            artist = _text(metadata, "singer", "artist", "singers")
            album = _text(metadata, "album", "albumName")
            normalized = str(state or "idle").lower()
            if not song or normalized not in ("playing", "paused", "buffering"):
                normalized = "idle"
            self._helper.update(title, artist, album, normalized,
                                _milliseconds(position), _milliseconds(duration))
        except Exception:
            self._report("update")

    def poll(self):
        """Drain queued commands now (called from the Kivy tick when visible)."""
        self._drain()

    def _drain(self):
        if self._closed or self._helper is None:
            return
        for _ in range(64):
            if self._closed:
                break
            try:
                command = self._helper.drainCommand()
            except Exception:
                self._report("poll")
                break
            if not command:
                break
            try:
                self._on_action(str(command))
            except Exception:
                # A UI action failure must not escape or lose later actions.
                _LOG.exception("Media notification action failed: %s", command)

    def start_polling(self, interval=0.25):
        """起一个**常驻后台线程**轮询通知命令。

        ⚠ 为什么必须这样：原来只在 Kivy 的 `_tick`（0.5s 一次）里 poll()，
          而 **App 退到后台时 Kivy 的 Clock 会停摆** —— 那恰恰是用户
          点通知栏「上一首/下一首/暂停」的时刻，命令永远取不出去，
          表现就是「通知按钮全都没反应」。
        Android 侧 drainCommand() 只是从 ArrayBlockingQueue 取字符串，
        在 Python 后台线程调用完全安全；动作本身再用 ui() 转回 Kivy 线程。
        """
        if self._poller is not None or self._closed:
            return
        self._poller = threading.Thread(target=self._poll_loop,
                                        name="media-poll", daemon=True)
        self._poller_stop = threading.Event()
        self._poller_interval = max(0.05, float(interval))
        self._poller.start()

    def _poll_loop(self):
        while not self._poller_stop.is_set():
            try:
                self._drain()
            except Exception:
                _LOG.exception("media poll loop")
            self._poller_stop.wait(self._poller_interval)

    def stop_polling(self):
        if self._poller is None:
            return
        try:
            self._poller_stop.set()
        except Exception:
            pass
        self._poller = None

    def close(self):
        """Idempotently cancel notification, unregister receiver, release session."""
        if self._closed:
            return
        self._closed = True
        self.stop_polling()
        helper, self._helper = self._helper, None
        if helper is not None:
            try:
                helper.close()
            except Exception:
                self._report("close")
        self._on_action = None
