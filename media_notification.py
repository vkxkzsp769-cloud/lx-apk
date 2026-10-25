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
        """Drain up to 64 commands in the caller's Kivy tick (no auto-scheduling)."""
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
                # A UI action failure must not escape the tick or lose later actions.
                _LOG.exception("Media notification action failed: %s", command)

    def close(self):
        """Idempotently cancel notification, unregister receiver, release session."""
        if self._closed:
            return
        self._closed = True
        helper, self._helper = self._helper, None
        if helper is not None:
            try:
                helper.close()
            except Exception:
                self._report("close")
        self._on_action = None
