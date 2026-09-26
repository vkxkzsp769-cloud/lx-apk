[app]
title = 落雪音源下载器
package.name = lxdownloader
package.domain = com.lxdl

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,js,json,otf,ttf

# 内置默认音源（用户也能在 App 里换）
source.include_patterns = assets/*, assets/sources/*, assets/fonts/*, sources/*

# 桌面专用的调试工具不进 APK（它们依赖 dukpy，Android 上没有这个 recipe）
source.exclude_patterns = test_desktop.py, lx_engine.py, test_ui.py, tests/*

version = 2.7.0

# release 模式打包成 apk 而不是 aab。
# buildozer 官方默认 `android.release_artifact = aab`（见 buildozer/
# buildozer/default.spec），而我们要的是能直接装的 apk。
# 为什么要走 release：只有 release 构建才会用 p4a 的 P4A_RELEASE_* 那套
# 显式密钥签名（p4a 源码 pythonforandroid/build.py 里 --keystore 等参数
# 映射的正是这几个环境变量）。debug 构建用的是 AGP 自动生成的 debug 钥匙，
# 在 CI 的全新环境里每次都会重新生成 —— 签名每次不同，用户就只能先卸载
# 再装。详见 .github/workflows/build.yml 的签名说明。
android.release_artifact = apk

# versionCode。buildozer 官方默认是 1（default.spec 里
# `# android.numeric_version = 1`），不显式设置的话每个版本都是 1。
# Android 要求新包的 versionCode >= 已装版本，显式递增最稳妥。
# 每次发版把它往上加（跟 version 对应：2.6.7 -> 20607）。
android.numeric_version = 20609

# 只用 kivy（不锁版本）。锁 kivy==2.3.x 会拉 thorvg 依赖，
# 其 recipe 在 NDK r25b 下 glob() 返回空 -> IndexError 导致编译失败。
requirements = python3,kivy,pyjnius,android

orientation = portrait
fullscreen = 0

# Android 版本
android.api = 33
android.minapi = 21
android.ndk = 25b
# 只编 arm64-v8a。双架构会让 openssl/python/libffi 各编两遍，
# 编译时间翻倍且极易超时（实测 13 分钟被打断）。
android.archs = arm64-v8a
android.allow_backup = True

# 网络 + 存储权限
# 下载目标为公共 Downloads/落雪音源：
#   Android 10 及以下：WRITE/READ_EXTERNAL_STORAGE
#   Android 11+：MANAGE_EXTERNAL_STORAGE（所有文件访问）
#                首次启动会跳系统授权页，见 request_storage_permission()
# 注意：MANAGE_EXTERNAL_STORAGE 会被展开成
#       android.permission.MANAGE_EXTERNAL_STORAGE，正是官方权限名，可直接用。
#       不要用 android.extra_manifest_xml —— buildozer 把它当「文件路径」open()，
#       填内联 XML 会 FileNotFoundError 导致编译失败。
android.permissions = INTERNET, ACCESS_NETWORK_STATE, READ_EXTERNAL_STORAGE, WRITE_EXTERNAL_STORAGE, MANAGE_EXTERNAL_STORAGE

# 明文 HTTP（音源有 11 个 http:// 主机）不能靠
# android.extra_manifest_application_arguments 配置 ——
# buildozer 1.5.0 会给内容套上字面双引号并把内部引号转义成 \"，
# 经 sh 直传（不走 shell 解析）后，p4a 把它原样渲染进 XML，
# 产生 " 导致 ManifestMerger 报 Error parsing AndroidManifest.xml（实测）。
# 改为在 workflow 里直接给 p4a 的 AndroidManifest 模板打补丁，
# 见 .github/workflows/build.yml 的「补丁 AndroidManifest 模板」步骤。

# 不开调试日志
android.logcat_filters = *:S python:D

p4a.bootstrap = sdl2
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
