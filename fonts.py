"""中文字体。

Kivy 默认字体 Roboto 不含任何中文字形，不注册的话中文全是空方块。
必须在创建任何 Kivy 控件之前调用 register()。
"""
import os

from kivy.utils import platform

from appenv import IS_ANDROID, diag, log, log_exc

FONT_NAME = "CNFont"
_registered_path = None


# 打包进 APK 的简体中文字体（1.5MB，从 Noto Sans CJK SC 裁剪）
# 为什么必须自带：
#   很多机器的 /system/fonts/NotoSansCJK-Regular.ttc 里 face[0] 是
#   「Noto Sans CJK JP」（日文），而 Kivy 用的 SDL_ttf 只会打开 face 0 ——
#   于是中文会用日文字形渲染，看起来就是「中文字符显示错误」
#   （直/骨/今/画 这类字最明显）。
#   所以优先用自带的 SC 字体，彻底摆脱设备差异。
BUNDLED_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "fonts", "NotoSansSC-subset.otf")


def _candidates():
    # 第一优先：自带的简体字体
    out = [BUNDLED_FONT]
    # 兜底：设备上可能存在的简体字体（同样避开 .ttc）
    if IS_ANDROID:
        # 顺序很重要：先 .otf/.ttf（纯简体字形）。
        # .ttc 是字体集合，face0 通常是日文变体，中文会渲染成日文字形，
        # 所以放最后。
        fixed = [
            "/system/fonts/NotoSansSC-Regular.otf",
            "/system/fonts/NotoSansHans-Regular.otf",
            "/system/fonts/DroidSansFallbackFull.ttf",
            "/system/fonts/DroidSansFallback.ttf",
            "/system/fonts/MiSans-Regular.ttf",
            "/system/fonts/MiSans-Normal.ttf",
            "/system/fonts/HarmonyOS_Sans_SC_Regular.ttf",
            "/system/fonts/HarmonyOS_SansSC_Regular.ttf",
            "/system/fonts/NotoSansCJK-Regular.ttc",
        ]
    else:
        fixed = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]

    out.extend(fixed)

    found = []
    for d in ("/system/fonts", "/system/font"):
        try:
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                low = fn.lower()
                if any(k in low for k in ("notosanssc", "notosanshans",
                                          "droidsansfallback", "misans",
                                          "harmony", "sourcehansans")):
                    found.append(os.path.join(d, fn))
        except Exception:
            log_exc("扫描字体目录 %s" % d)
    found.sort(key=lambda p: p.lower().endswith(".ttc"))
    out.extend(found)
    return out


def register():
    """注册中文字体，返回 True 表示成功（之后用 font_kwargs() 取参数）"""
    global _registered_path
    if _registered_path:
        return True

    try:
        from kivy.core.text import LabelBase
    except Exception:
        log_exc("import LabelBase")
        return False

    for path in _candidates():
        try:
            if not (path and os.path.exists(path)
                    and os.path.getsize(path) > 10000):
                continue
            LabelBase.register(name=FONT_NAME, fn_regular=path)
            _registered_path = path
            log("已注册中文字体:", path)
            diag("中文字体 = %s%s" % (
                path, "（自带简体，字形正确）" if path == BUNDLED_FONT else
                "（设备字体，可能是日文字形）"))
            return True
        except Exception:
            log_exc("注册字体 %s" % path)

    log("警告: 未找到中文字体，中文可能显示成方块")
    return False


def font_kwargs():
    """给 Kivy 控件的构造参数（注册失败就返回空，用默认字体）"""
    return {"font_name": FONT_NAME} if _registered_path else {}
