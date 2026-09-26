"""随音乐而动的背景。

为什么不是「真频谱」
--------------------
Kivy 这边拿不到音频数据：Android 的 MediaPlayer 不暴露 PCM 缓冲，
要取真实频谱只能挂 android.media.audiofx.Visualizer，而它需要
**RECORD_AUDIO 权限** —— 一个音乐下载器申请麦克风权限，用户第一反应
就是「这玩意儿要偷听我」，应用商店也可能判隐私风险。不值得。

所以改成「播放状态驱动」：播放时缓慢漂移 + 呼吸脉动，暂停/停止后
平滑归于平静。观感上就是随音乐而动，代价是零权限、零额外峰值耗电
（不播放时完全不 tick）。

用法
----
    bg = MusicBackground(root)      # 挂在 root 的 canvas.before 上
    bg.set_playing(True / False)    # 跟播放器状态同步

绘制顺序：必须**在** attach_bg(root, C_BG) 之后创建 —— canvas.before 里
的指令按插入顺序绘制，光斑要压在底色之上、子控件之下。
"""
import math

from kivy.clock import Clock
from kivy.graphics import Color, Ellipse
from kivy.metrics import dp

from appenv import log_exc


class MusicBackground:
    """跟着播放状态缓动的背景光斑。"""

    # 光斑基色：主题蓝 / 青 / 紫，跟界面强调色同一族
    TINTS = (
        (0.29, 0.56, 0.95),
        (0.20, 0.72, 0.86),
        (0.45, 0.38, 0.90),
        (0.16, 0.62, 0.72),
    )

    def __init__(self, host, fps=24):
        self._host = host
        self._fps = float(fps)
        self._t = 0.0
        self._level = 0.0          # 0..1「播放强度」，平滑跟随，避免跳变
        self._playing = False
        self._ev = None
        self._dots = []

        n = len(self.TINTS)
        for i in range(n):
            r, g, b = self.TINTS[i]
            col = Color(r, g, b, 0.0)
            ell = Ellipse(pos=(0, 0), size=(dp(10), dp(10)))
            host.canvas.before.add(col)
            host.canvas.before.add(ell)
            # 每个光斑的相位错开，看起来才不像同步呼吸
            self._dots.append({"col": col, "ell": ell, "ph": i * 1.7})

        host.bind(pos=self._layout, size=self._layout)
        self._layout()

    # ---------- 对外 ----------
    def set_playing(self, playing):
        """播放器状态变化时调用；重复调用同一状态是安全的"""
        playing = bool(playing)
        if playing == self._playing:
            return
        self._playing = playing
        if playing:
            self._start()

    # ---------- 内部 ----------
    def _start(self):
        if self._ev is None:
            self._ev = Clock.schedule_interval(self._step, 1.0 / self._fps)

    def _stop(self):
        if self._ev is not None:
            self._ev.cancel()
            self._ev = None

    def _layout(self, *_):
        """控件尺寸变化时先把光斑收进边界内，避免第一帧画到外面"""
        try:
            self._place(initial=True)
        except Exception:
            log_exc("background.layout")

    def _place(self, initial=False):
        w = max(1, self._host.width)
        h = max(1, self._host.height)
        for d in self._dots:
            ph = d["ph"]
            x = 0.5 + 0.38 * math.sin(self._t * 0.13 + ph)
            y = 0.5 + 0.36 * math.cos(self._t * 0.11 + ph * 1.3)
            breath = 0.5 + 0.5 * math.sin(self._t * 1.6 + ph * 2.1)
            scale = 0.35 + 0.65 * self._level
            size = max(dp(24), min(w, h) * (0.30 + 0.22 * breath) * scale * 2)
            d["ell"].size = (size, size)
            d["ell"].pos = (w * x - size / 2, h * y - size / 2)
            # 静止时压到几乎不可见，避免不播放时还在抢注意力
            d["col"].a = 0.05 + 0.17 * self._level * breath

    def _step(self, dt):
        try:
            target = 1.0 if self._playing else 0.0
            # 平滑跟随：暂停后不会「啪」地定格，而是缓下来
            self._level += (target - self._level) * min(1.0, dt * 2.2)
            self._t += dt
            self._place()
            if not self._playing and self._level < 0.01:
                # 完全静下来就停止 tick —— 不播放时不耗电
                self._level = 0.0
                self._stop()
        except Exception:
            log_exc("background.step")
