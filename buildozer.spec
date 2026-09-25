[app]
title = 落雪音源下载器
package.name = lxdownloader
package.domain = com.lxdl

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,js,json

# 内置默认音源（用户也能在 App 里换）
source.include_patterns = assets/*, sources/*

version = 1.0.0

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

# 音源里有大量 http:// 接口（y.qq.com / dl.stream.qqmusic.qq.com /
# www.kugou.com / music.migu.cn ...）。targetSdk>=28 默认禁止明文，
# WebView 里的 XHR 会被 network security policy 拦掉，取直链必失败。
# 注意：这里填的是「文件路径」，buildozer 会 open() 读取其内容作为
#       <application> 的属性；直接写内联字符串会 FileNotFoundError。
android.extra_manifest_application_arguments = manifest_app_args.txt

# 不开调试日志
android.logcat_filters = *:S python:D

p4a.bootstrap = sdl2
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
