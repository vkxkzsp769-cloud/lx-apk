# 落雪(LX)音源下载器 · Android APK

选一个**落雪/LX Music 音源文件(.js)**，搜索歌曲并下载 **MP3 / FLAC**。

内置一份默认音源，也可以从手机里选新的 `.js` 音源随时换源。

---

## 怎么拿到 APK

本仓库配好了 GitHub Actions 云端编译，**不需要在手机上装 Android SDK/NDK**：

1. 打开本仓库的 **Actions** 标签页
2. 点左侧 **Build APK** → 右侧 **Run workflow** 按钮（或每次 push 会自动触发）
3. 等编译完成（第一次约 15~25 分钟，之后有缓存约 3~5 分钟）
4. 点进那次成功的任务，页面底部 **Artifacts** → 下载 `lx-downloader-apk`
5. 解压得到 `.apk`，装到手机

> 详细图文步骤见 [`编译APK说明.md`](编译APK说明.md)

---

## 为什么用 WebView 跑音源 JS

落雪音源是 JavaScript，而 python-for-android 的 recipe 里**没有** dukpy/quickjs
这类 JS 引擎（无法编译 C 扩展）。但**安卓系统自带 WebView —— 它本身就是完整的 JS 引擎**。

```
Kivy 界面 (Python)          ← 搜索、下载、进度条
   └─ pyjnius 创建隐藏 WebView
        └─ WebView 里实现 lx 契约 (用 XHR 发网络请求)
             └─ 执行音源 .js → 返回音频直链
                  └─ 回传给 Python 下载
```

这套方案**零 C 扩展依赖**，编译成功率高。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `main.py` | App 主程序（Kivy 界面 + 下载 + WebView 引擎） |
| `lx_engine.py` | 纯 Python 引擎（WebView 不可用时兜底，需 dukpy） |
| `buildozer.spec` | APK 打包配置（包名 / 权限 / 架构） |
| `.github/workflows/build.yml` | 云端自动编译脚本 |
| `assets/default_source.js` | 内置默认音源 |
| `test_desktop.py` | 本地链路测试（不装 Kivy 也能跑） |
| `编译APK说明.md` | 完整编译/使用/排错教程 |

---

## 本地先测一遍（推荐）

在 Termux 或电脑上：

```bash
pip install dukpy
python test_desktop.py "海阔天空"
```

跑通会看到：搜索列表 → 直链 → 下载 → `是音频 ✓`
**这一步过了，云端编译基本不会因为代码问题失败。**

---

## 下载的文件在哪

```
/storage/emulated/0/Download/落雪音源/
```

即「文件管理 → 内部存储 → Download → 落雪音源」，
卸载 App 不会丢失，可直接播放。

> Android 11+ 首次启动会请求「所有文件访问」权限（写公共目录必需）。
> 拒绝也能用，文件会退回 App 私有目录
> `Android/data/com.lxdl.lxdownloader/files/downloads/`（卸载会清空）。

## 换音源

**方式一（用户自己换，不用重编）**
App 里点 **换音源** → 从手机文件管理器选新的 `.js` 文件 → 自动加载。

**方式二（你更新默认音源，重新发版）**
替换 `assets/default_source.js` → push → Actions 自动重新编译 → 下载新 APK。

---

## 免责声明

仅供技术学习使用。音源内的第三方 API 均非本项目提供，
下载的音乐版权归各平台/版权方所有，请勿用于商业用途或传播。

---

## 安全提示

请勿在仓库/聊天中粘贴 GitHub Personal Access Token。
本项目推送只用到 SSH 密钥（`git@github.com:...`），不需要 token。
如果 token 曾经泄露，请到 https://github.com/settings/tokens 立即吊销重建。
