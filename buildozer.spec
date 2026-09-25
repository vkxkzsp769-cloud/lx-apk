[app]
title = 落雪音源下载器
package.name = lxdownloader
package.domain = com.lxdl

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,js,json

# 内置默认音源（用户也能在 App 里换）
source.include_patterns = assets/*, sources/*

version = 1.0.0

requirements = python3,kivy==2.3.0,pyjnius,android

orientation = portrait
fullscreen = 0

# Android 版本
android.api = 33
android.minapi = 21
android.ndk = 25b
# 只编 64 位；如需支持老机型再改回 arm64-v8a, armeabi-v7a
android.archs = arm64-v8a
android.allow_backup = True

# 需要网络权限
android.permissions = INTERNET, ACCESS_NETWORK_STATE, WRITE_EXTERNAL_STORAGE, READ_EXTERNAL_STORAGE

# 不开调试日志
android.logcat_filters = *:S python:D

p4a.bootstrap = sdl2
p4a.branch = develop
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
