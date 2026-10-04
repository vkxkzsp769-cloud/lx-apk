"""中文字体。

Kivy 默认字体 Roboto 不含任何中文字形，不注册的话中文全是空方块。
必须在创建任何 Kivy 控件之前调用 register()。
"""
import os

from kivy.utils import platform

from appenv import IS_ANDROID, diag, log, log_exc

FONT_NAME = "CNFont"
_registered_path = None

# 等宽字体（只用于时间这类纯数字文本，例如播放条的 "01:23 / 04:05"）
#
# 为什么需要：iOS 的播放时间用等宽字形，数字跳动时宽度不变，进度条不会
# 跟着抖。中文数字在 Noto Sans SC 里是比例宽度，所以另找一个等宽字体。
# 找不到就走 FONT_NAME 兜底 —— 等宽是锦上添花，绝不能因为缺字体而崩。
MONO_NAME = "CNMono"
_mono_path = None


# 打包进 APK 的中文字体（~9.9MB）
#
# 为什么必须自带：
#   很多机器的 /system/fonts/NotoSansCJK-Regular.ttc 里 face[0] 是
#   「Noto Sans CJK JP」（日文），而 Kivy 用的 SDL_ttf 只会打开 face 0 ——
#   于是中文会用日文字形渲染，看起来就是「中文字符显示错误」
#   （直/骨/今/画 这类字最明显）。
#   所以优先用自带的 SC 字体，彻底摆脱设备差异。
#
# 覆盖范围（这一版的关键修复）：
#   上一版只裁到 GB2312（6763 汉字），于是**繁体字、日文汉字、韩文、
#   以及 ©®™♥♪ 这类符号**全部缺字 —— 歌名/歌手名里一出现就是方块
#   （用户反馈「大部分正常，但部分字是方块」就是这个原因）。
#   现在覆盖：CJK 基本区(简+繁+日文汉字) + 扩展A + 兼容汉字 + 假名
#   + 全角标点 + 常用符号，共 29717 个码点。
#
# 想重新生成（换字重/裁体积）：
#   从 Noto Sans SC 可变字体实例化到 wght=400 再子集化，
#   命令见 编译APK说明.md 的「字体」一节。
#   注意 VF 的默认轴是 100(Thin)，必须显式指定 400，否则字形过细。
BUNDLED_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", "fonts", "NotoSansSC-full.ttf")


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


def popup_kwargs():
    """Popup 的字体参数。

    Popup **不是** Label：它不接受 font_name，标题字体用的是独立属性
    `title_font`（Kivy 默认 'Roboto'）。所以不能用 font_kwargs()。
    漏传的后果是弹窗标题（如「歌曲信息」）用 Roboto 渲染 —— 中文全是方块。
    （这个缺口一直没被发现，是因为 test_ui 的控件树检查只遍历了
      popup.content，从没检查 Popup 自身的标题。）
    """
    return {"title_font": FONT_NAME} if _registered_path else {}


def _mono_candidates():
    """等宽字体候选。只用来渲染时间这类 ASCII 文本，所以不挑字形。

    各家 Android 的等宽字体名字差得很远（RobotoMono / NotoSansMono /
    DroidSansMono / MonoSpace…），光靠写死的几个路径命中率不高。
    所以写死的列表之后**再扫一遍 /system/fonts**，凡是文件名里带 mono 的
    都收进来 —— 和中文字体那边同一套思路。

    等宽只是「数字不跳宽度」，命中不了就走 font_kwargs() 兜底，
    绝不能因为缺字体让界面出问题，所以这里一律 try 住。
    """
    if IS_ANDROID:
        out = [
            "/system/fonts/RobotoMono-Regular.ttf",
            "/system/fonts/NotoSansMono-Regular.ttf",
            "/system/fonts/DroidSansMono.ttf",
            "/system/fonts/CutiveMono.ttf",
            "/system/fonts/MonoSpace.ttf",
        ]
        found = []
        for d in ("/system/fonts", "/system/font"):
            try:
                if not os.path.isdir(d):
                    continue
                for fn in sorted(os.listdir(d)):
                    low = fn.lower()
                    if "mono" in low and low.endswith((".ttf", ".otf")):
                        found.append(os.path.join(d, fn))
            except Exception:
                log_exc("扫描等宽字体目录 %s" % d)
        out.extend(found)
        return out

    return [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/Cour.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ]


def register_mono():
    """注册等宽字体。找不到返回 False，调用方用 mono_kwargs() 会自动兜底。"""
    global _mono_path
    if _mono_path:
        return True
    try:
        from kivy.core.text import LabelBase
    except Exception:
        log_exc("import LabelBase(mono)")
        return False
    for path in _mono_candidates():
        try:
            if not (path and os.path.exists(path)
                    and os.path.getsize(path) > 10000):
                continue
            LabelBase.register(name=MONO_NAME, fn_regular=path)
            _mono_path = path
            log("已注册等宽字体:", path)
            return True
        except Exception:
            log_exc("注册等宽字体 %s" % path)
    log("未找到等宽字体，时间文本退回中文字体（不影响显示）")
    return False


def mono_kwargs():
    """时间这类「用等宽更好」的文本控件用。

    找不到等宽字体就返回中文字体参数 —— 数字仍然正常显示，
    只是失去等宽（不会变方块，所以这里不需要专门报错）。
    """
    if _mono_path:
        return {"font_name": MONO_NAME}
    return font_kwargs()
