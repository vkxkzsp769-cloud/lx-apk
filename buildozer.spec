[app]
title = 落雪音源下载器
package.name = lxdownloader
package.domain = com.lxdl

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,js,json

# 内置默认音源（用户也能在 App 里换）
source.include_patterns = assets/*, sources/*

version = 1.0.0

requirements = python3,kivy,pyjnius,android,urllib3,certifi

orientation = portrait
fullscreen = 0

# Android 版本
android.api = 33
android.minapi = 21
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = True

# 需要网络权限
# 只保留真正需要的：下载写的是 getExternalFilesDir()（应用私有，免权限），
# targetSdk 33 下 WRITE/READ_EXTERNAL_STORAGE 已是空操作
android.permissions = INTERNET, ACCESS_NETWORK_STATE

# 音源里很多 API 是 http:// ，targetSdk>=28 默认禁止明文，必须显式打开
android.uses_cleartext_traffic = True

# 不开调试日志
android.logcat_filters = *:S python:D

p4a.bootstrap = sdl2
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
