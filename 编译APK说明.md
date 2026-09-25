# 编译 APK 完整教程（手机操作，云端编译）

## 结论先说

你**不需要**在手机上装 Android SDK/NDK。用 **GitHub Actions 云端编译**：
你手机只负责把代码传上去、点一下按钮，编译在 GitHub 的服务器上跑，
十几分钟后直接下载 APK 装到手机。

AIDE 做不了这个（它编译 Java，而本项目核心是 Python + JS）。

---

## 为什么用 WebView 跑音源 JS（重要，别改）

落雪音源是 JavaScript。而 python-for-android 的 recipe 里**没有** dukpy/quickjs
这类 JS 引擎（无法编译 C 扩展）。但**安卓系统自带 WebView，它本身就是完整的 JS 引擎**。

所以 App 的架构是：

```
Kivy 界面 (Python)          ← 搜索、下载、进度条
   └─ pyjnius 创建隐藏 WebView
        └─ WebView 里实现 lx 契约 (用 XHR 发网络请求)
             └─ 执行音源 .js → 返回音频直链
                  └─ 回传给 Python 下载
```

这套方案我已经在本地用 Node 模拟 WebView 验证过，
能加载你的 K×H 音源、拿到直链、下载到 43MB 的 FLAC。

---

## 步骤一：把代码传到 GitHub

1. 手机浏览器打开 github.com，登录你的账号
2. 右上角 **+** → **New repository**
3. 名字随便，比如 `lx-apk`，选 **Public**（**Private 也能用 Actions**），
   勾上 **Add a README file** → Create
4. 进入仓库 → **Add file** → **Upload files**
5. 把 `apk_build/` 里的这些**全部**上传：

```
main.py
buildozer.spec
test_desktop.py
lx_engine.py
assets/default_source.js     ← 你的音源（必须）
.github/workflows/build.yml  ← 注意这个在 .github 目录里
```

> ⚠️ 手机上传 `.github` 这种点开头的文件夹比较麻烦。
> **最省事的办法**：在电脑上打包 zip 传，或者用 Termux 的 git 命令：
> ```bash
> cd /sdcard/我的文件/linux共享/落雪音源+源码/apk_build
> pkg install git -y
> git init && git add -A && git commit -m "init"
> git branch -M main
> git remote add origin https://github.com/你的用户名/lx-apk.git
> git push -u origin main
> ```
> （会提示输用户名和 token，token 在 GitHub 设置里生成）

---

## 步骤二：等云端自动编译

推送成功后：

1. 打开你的仓库 → 顶部 **Actions** 标签
2. 会看到一个 **Build APK** 的任务在跑（黄点 = 进行中）
3. **第一次大约 15~25 分钟**（要下载 NDK、编译 Python）
   之后有缓存，**再编译只要 3~5 分钟**
4. 变成 ✅ 绿勾就好了

如果变 ❌ 红叉：
- 点进去看哪一步失败
- 把失败日志发给我，我来改

---

## 步骤三：下载 APK 装到手机

1. 点进那个成功的任务
2. 页面底部 **Artifacts** → 点 **lx-downloader-apk** 下载
3. 得到一个 zip，解压出 `.apk`
4. 点安装（会提示"未知来源"，允许即可）

---

## 怎么用这个 App

```
[音源: 网易云(wy)]  [换音源]
[网易云 ▼]          [320k ▼]
[输入歌名..............] [搜索]
─────────────────────────────
 1. 海阔天空 — Beyond  [03:59]
 2. 晴天 — 周杰伦      [04:29]
 ...
─────────────────────────────
[████████░░░░░░] 65%
下载中: 海阔天空 65% (5.2/8.0 MB)
```

1. 输入歌名 → **搜索**
2. 点结果里的任意一首 → 自动取直链并下载
3. 进度条走完就下好了

**下载的文件在哪？**
```
/storage/emulated/0/Download/落雪音源/
```
也就是手机「文件管理 → 内部存储 → Download → 落雪音源」，
和别的下载放在一起，卸载 App 也不会丢，可以直接播放。

> 首次启动会跳一次「所有文件访问」授权页（Android 11+ 写公共目录需要）。
> 不授权也能用，只是文件会存到 App 私有目录：
> `Android/data/com.lxdl.lxdownloader/files/downloads/`（卸载会清空）

### 换音源（不用重新编译 APK）

点 **换音源** → 从手机里选新的 `.js` 音源文件 → 自动加载。
选好的音源会保存在 App 目录，下次启动直接用。

---

## 常见问题

**Q: 点搜索没反应 / 报错**
先确认网络正常。搜索用的是网易云公开接口，偶尔会限流，等几秒再试。

**Q: 一直提示"直链受限"**
这首在网易云要 VIP。换一首，或点别的搜索结果试试。
App 会自动降音质重试、并依次尝试多个搜索结果。

**Q: 编译失败，提示 Android SDK license**
workflow 里已经加了 `android.accept_sdk_license = True`。
如果还报错，把日志发我。

**Q: 想让 App 更小 / 支持更多手机**
改 `buildozer.spec` 里的 `android.archs`。
现在同时编了 `arm64-v8a`（主流）和 `armeabi-v7a`（老机型）。
只留 `arm64-v8a` 能小一半，但老手机装不了。

**Q: 音源接口挂了怎么办**
两个办法：
1. 你在手机 App 里点"换音源"换一个新的（对方用户也能这么做）
2. 把新的 `assets/default_source.js` 替换掉，重新 push，
   GitHub 会自动重新编译，你下载新 APK 发给别人

**Q: 能加歌词/封面吗**
可以。音源本身支持 `lyric`/`pic` 事件，
在 `main.py` 的 `LX_HOST_JS` 里加对应的调用即可（结构和 `lxGetUrl` 一样）。

---

## 本地先测一遍（推荐，省得白等编译）

在 Termux 里：

```bash
pkg install python -y
pip install dukpy
cd /sdcard/我的文件/linux共享/落雪音源+源码/apk_build
python test_desktop.py "海阔天空"
```

跑通会看到：搜索列表 → 直链 → 下载 → `是音频 ✓`
**这一步过了，云端编译基本不会因为代码问题失败。**

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `main.py` | App 主程序（界面 + 下载 + WebView 引擎） |
| `lx_engine.py` | 纯 Python 引擎（WebView 不可用时兜底） |
| `buildozer.spec` | APK 打包配置（包名/权限/架构） |
| `.github/workflows/build.yml` | 云端自动编译脚本 |
| `assets/default_source.js` | 内置默认音源 |
| `test_desktop.py` | 本地链路测试（不装 Kivy 也能跑） |
