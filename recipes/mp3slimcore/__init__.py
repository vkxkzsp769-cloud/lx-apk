"""p4a recipe：把 native/mp3slim 编译成 libmp3slim.so，打进 APK。

为什么自己写 recipe 而不用 p4a 自带的 ffmpeg：
  1. 官方 `ffmpeg` recipe 产出的是「一堆 .so + 一个可执行文件」，
     而 Android 的应用目录是 noexec —— 那个 ffmpeg 二进制根本 exec 不起来；
  2. 我们只需要「MP3 解码 + MP3 编码」，ffmpeg 那套要连带编
     libx264 / libvpx / libshine，20 分钟起步，还容易挂在 NDK 上。

这里把解码器（dr_libs：单头文件、public domain）与编码器
（LAME 3.100 源码、LGPL）编进**同一个** .so：
  - 无外部依赖，APK 只多 500KB 左右；
  - Python 侧 ctypes 调 3 个函数，不需要 subprocess、不需要 ffmpeg；
  - ctypes 直接 dlopen nativeLibraryDir 里的 .so，不受 noexec 影响
    （dlopen 是 mmap 映射，不要求文件带可执行位）。

源码**全部在仓库里**（native/mp3slim/）：构建机不下载任何东西。
"""
import os
import glob

import sh
from pythonforandroid.logger import info, warning, shprint
from pythonforandroid.recipe import Recipe
from pythonforandroid.util import current_directory


# 仓库根目录（本文件在 <repo>/recipes/mp3slimcore/__init__.py）。
# 不用 p4a 的 ctx/root_dir：那些字段在不同版本里改过名，而 __file__ 恒成立。
REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SLIM_SRC = os.path.join(REPO_ROOT, "native", "mp3slim")
LAME_SRC = os.path.join(SLIM_SRC, "codec", "lame")


class Mp3SlimCoreRecipe(Recipe):
    # version 只用于给 p4a 的内部构建目录命名，以及方便在日志里认出这一版。
    # 改了 native/ 下的源码就把版本号往上加，强制重建。
    version = "1.0.0"

    # 不下载任何东西 —— 源码已经在仓库里了。
    # 覆写 download_if_necessary 是为了绕开 p4a 的下载流程：
    # 没有 url 的 recipe 在这里会试图下载 None。
    url = None
    depends = []

    # 产物：build 目录 lib/ 下的 libmp3slim.so，p4a 会拷进 APK 的 lib/<abi>/
    built_libraries = {"libmp3slim.so": "lib"}

    def download_if_necessary(self, arch=None):
        if not os.path.isdir(SLIM_SRC):
            raise RuntimeError(
                "找不到 native/mp3slim（期望在 %s）。本 recipe 依赖仓库内的源码，"
                "不会联网下载。" % SLIM_SRC)

    def should_build(self, arch):
        target = os.path.join(self.get_build_dir(arch.arch), "lib",
                              "libmp3slim.so")
        return not os.path.exists(target)

    def _sources(self):
        """核心 + LAME 的全部 .c（排除 mpglib_interface.c 与 vector/ 下的 MMX 实现）"""
        srcs = [os.path.join(SLIM_SRC, "mp3slim_core.c")]
        for path in sorted(glob.glob(os.path.join(LAME_SRC, "*.c"))):
            name = os.path.basename(path)
            if name == "mpglib_interface.c":
                # mpglib 是 LAME 自带的 MP3 解码器，我们不用它（解码交给 dr_mp3），
                # 而且它要 <interface.h> 等一串 mpglib/ 下的头文件。
                continue
            srcs.append(path)
        return srcs

    def build_arch(self, arch):
        srcs = self._sources()
        if not os.path.exists(srcs[0]):
            raise RuntimeError("缺少 native 源文件: %s" % srcs[0])

        build_dir = self.get_build_dir(arch.arch)
        lib_dir = os.path.join(build_dir, "lib")
        os.makedirs(lib_dir, exist_ok=True)
        out = os.path.join(lib_dir, "libmp3slim.so")

        env = self.get_recipe_env(arch)
        # ⚠ 必须带上 env['CFLAGS']：NDK 交叉编译的 --sysroot / target 参数都在
        # 里面（arch.get_env() 提供）。只给 -I 会让 clang 用宿主头文件，
        # 编出来的 .so 链接到不存在的 libc 符号，装到手机上一加载就崩。

        # ⚠ 不要把 env["CC"] 整体当成可执行文件路径传给 sh.Command()。
        # p4a 的 arch.get_env()["CC"] 是一整条**带空格和参数的命令行**：
        #   "/usr/bin/ccache /home/.../android-ndk-r25b/.../bin/clang -target
        #    aarch64-linux-android21 -fomit-frame-pointer -march=armv8-a -fPIC"
        # 直接 sh.Command(cc) 会抛 CommandNotFound —— 实测整个 create 直接失败。
        #
        # 正确做法：把这条命令串**按空白切成若干段**，
        #   第 1 段（ccache 或 clang）当可执行文件，其余段原样排在编译参数**前面**。
        # 这样 ccache 包装、NDK 的 --target / -march 全都原样生效，
        # 也不用经过 shell（省掉引号转义带来的所有麻烦）。
        raw_cc = env.get("CC") or arch.get_env().get("CC") or "clang"
        cc_parts = str(raw_cc).split()
        if not cc_parts:
            cc_parts = ["clang"]

        cmd = cc_parts + [
            "-O3", "-fPIC", "-shared",
            "-DHAVE_CONFIG_H",        # 用 native/mp3slim/config.h
            "-funroll-loops",
            # LAME 3.100 里有几十条 -Wshift-negative-value / -Wabsolute-value，
            # 全是它自己的老代码风格，不影响正确性。压掉它们，
            # 让编译日志里只剩下**我们**关心的东西（缺文件、链接失败）。
            "-w",
            "-I" + SLIM_SRC,
            "-I" + LAME_SRC,
            "-I" + os.path.join(LAME_SRC, "vector"),
            "-o", out,
        ] + srcs + ["-lm"]

        info("mp3slimcore: 编译 libmp3slim.so（%d 个 C 文件，arch=%s）"
             % (len(srcs), arch.arch))
        info("mp3slimcore: cc = %s" % " ".join(cc_parts))
        with current_directory(build_dir):
            # cc_parts[0] 是 ccache 或 clang，其余（NDK 的 --target 等）当参数
            shprint(sh.Command(cmd[0]), *cmd[1:], _env=env)

        if not os.path.exists(out):
            raise RuntimeError("libmp3slim.so 没有生成")

        size = os.path.getsize(out)
        info("mp3slimcore: 生成 %s（%.1f KB）" % (out, size / 1024.0))
        if size < 50 * 1024:
            warning("mp3slimcore: libmp3slim.so 只有 %.1f KB，可能没链进 LAME"
                    % (size / 1024.0))


recipe = Mp3SlimCoreRecipe()
