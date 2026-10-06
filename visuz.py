"""播放频谱（Android Visualizer + Goertzel 频带能量）。

给「正在播放」全屏页的音符频谱环供数据。**只读播放混音，不录任何东西**，
但 Android 把 Visualizer 归为敏感能力，需要 RECORD_AUDIO 运行时权限。

分层：
  * goertzel_bands() / 频率表 —— 纯函数，无 Kivy 无 Java，可单测
  * Visuz —— Android 侧封装；桌面/未授权时 start() 返回 False，
    bands() 返回 None，UI 自行降级（模拟律动），**绝不抛**
"""
import math

from appenv import IS_ANDROID, diag, log_exc

# 24 个对数分布中心频点：55Hz ~ 3100Hz（中低频观感最好，同极光原版取向）
def _centers():
    lo, hi, n = 55.0, 3100.0, 24
    return [lo * (hi / lo) ** (i / float(n - 1)) for i in range(n)]

CENTERS = _centers()
N_CENTER = len(CENTERS)


def downsample(wave, factor=4):
    """1024 点波形 → 256 点（每 factor 个取平均），采样率同比降。"""
    n = (len(wave) // factor) * factor
    out = [0.0] * (n // factor)
    for i in range(0, n, factor):
        s = 0
        for k in range(factor):
            s += wave[i + k]
        out[i // factor] = s / float(factor)
    return out


def goertzel_bands(samples, rate, centers=CENTERS):
    """Goertzel 单频点能量 → 每频带 0..1 的归一化能量。

    输入是 -128..127 的波形（降采样后），输出 len(centers) 个 float。
    归一化：除以「满幅同频正弦」的理论能量 —— 1.0≈该频段有鼓点级强度。
    """
    n = len(samples)
    if n < 8 or rate <= 0:
        return [0.0] * len(centers)
    out = []
    for f in centers:
        if f * 2.0 >= rate:                # 超过奈奎斯特，直接 0
            out.append(0.0)
            continue
        w = 2.0 * math.pi * f / rate
        cw = 2.0 * math.cos(w)
        s1 = s2 = 0.0
        for x in samples:
            s0 = x + cw * s1 - s2
            s2 = s1
            s1 = s0
        power = s1 * s1 + s2 * s2 - cw * s1 * s2
        mag = math.sqrt(power) if power > 0 else 0.0
        # 理论参考：幅值 A 的正弦 Goertzel 输出 ≈ A*n/2；A=128 满幅
        out.append(min(1.0, mag / (128.0 * n / 4.0)))
    return out


def spread_to_bars(bands, bar_count, smooth_state, sens=1.0,
                   attack=0.5, release=0.12):
    """24 频带（对数）→ N 根谱条（对数横轴插值 + 极光原版平滑）。

    攻击快（0.5）、释放慢（0.12）、低频加重（-0.4 斜率）、pow(0.85) ——
    参数和 stage.js 一一对应。smooth_state 是外部持有的 Float 列表（复用）。
    """
    nb = len(bands)
    for i in range(bar_count):
        # 对数横轴：条 i → 频带位置（0..nb-1）
        pos = (math.log(1.0 + (math.e - 1.0) * i / float(max(1, bar_count - 1))) if bar_count > 1 else 0.0)
        g = pos * (nb - 1)
        k = int(g)
        k2 = min(nb - 1, k + 1)
        fr = g - k
        raw = bands[k] * (1.0 - fr) + bands[k2] * fr
        weight = 1.0 - (i / float(bar_count)) * 0.4        # 加重低频
        target = math.pow(max(0.0, min(1.0, raw * sens * weight * 1.6)), 0.85)
        cur = smooth_state[i]
        a = attack if target > cur else release
        smooth_state[i] = cur + (target - cur) * a
    return smooth_state


class Visuz(object):
    """Android Visualizer(0)（主输出混音）封装。非线程安全：主线程调用。"""

    def __init__(self):
        self.vis = None
        self.size = 1024
        self.rate = 44100.0
        self._buf = None
        self.ok = False
        self.smooth = [0.0] * 96
        self._last_err = None

    # ---------- 权限（只发起，回调在 main 侧处理）----------
    def request_permission(self):
        if not IS_ANDROID:
            return
        try:
            from jnius import autoclass
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            String = autoclass("java.lang.String")
            act = PythonActivity.mActivity
            arr = autoclass("[Ljava.lang.String;")(1)
            arr[0] = String("android.permission.RECORD_AUDIO")
            act.requestPermissions(arr, 0x5ACE)
            diag("已发起 RECORD_AUDIO 权限请求")
        except Exception:
            log_exc("request RECORD_AUDIO")

    # ---------- 生命周期 ----------
    def start(self):
        """成功 True；桌面/无权限/驱动拒绝 → False（调用方降级）"""
        if self.ok:
            return True
        if not IS_ANDROID:
            return False
        try:
            from jnius import autoclass
            V = autoclass("android.media.audiofx.Visualizer")
            rng = V.getCaptureSizeRange()
            self.vis = V(0)                          # 0 = 主输出混音（MediaPlayer 不暴露 sessionId）
            self.size = int(max(64, min(1024, rng[len(rng) - 1])))
            self.vis.setCaptureSize(self.size)
            try:
                sr = self.vis.getSamplingRate()
                if sr and sr > 4000:
                    self.rate = float(sr)
            except Exception:
                pass
            self.vis.setEnabled(True)
            self._buf = autoclass("[B")(self.size)
            self.ok = True
            diag("Visualizer 启动 size=%d rate=%d" % (self.size, int(self.rate)))
            return True
        except Exception as e:
            self._last_err = str(e)
            log_exc("Visualizer.start")
            self.vis = None
            self.ok = False
            return False

    def stop(self):
        if self.vis is not None:
            try:
                self.vis.setEnabled(False)
                self.vis.release()
            except Exception:
                log_exc("Visualizer.stop")
        self.vis = None
        self.ok = False

    # ---------- 取数据 ----------
    def bands(self, bar_count=96, sens=1.0):
        """返回长度 bar_count 的 0..1 列表；不可用时 None（不抛）。"""
        if not self.ok or self.vis is None:
            return None
        try:
            self.vis.getWaveform(self._buf)
            raw = [self._buf[i] for i in range(self.size)]
            wave = downsample(raw, 4)
            b = goertzel_bands(wave, self.rate / 4.0)
            if len(self.smooth) != bar_count:
                self.smooth = [0.0] * bar_count
            return spread_to_bars(b, bar_count, self.smooth, sens=sens)
        except Exception:
            log_exc("Visualizer.bands")
            return None
