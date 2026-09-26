# 给下一个 AI 的启动提示词（直接复制这段）

---

我在做一个**落雪音源下载器**（安卓 APK + 命令行工具），代码在
`/sdcard/我的文件/linux共享/落雪音源+源码/`。

**请先读 `交接文档.md`**（在同一个目录），里面写全了：
架构、三条线程的规则、p4a/Kivy 的所有坑、以及前一个 AI 犯过的错。
**读完再动代码**，否则很容易把已经修好的问题改回去。

## 关键信息速览

- **APK 代码**：`apk_build/`，入口 `main.py`
- **CLI 代码**：`lx_crawler/`
- **音源文件**：`音源/`（落雪/LX Music 的 user-API .js）
- **GitHub 仓库**：`github.com/vkxkzsp769-cloud/lx-apk`
- **发版方式**：改完 push 到 main → GitHub Actions 云端编译 → **自动发布 Release**
  （不需要人工上传 APK，也不需要本地 Android SDK）
- **改完后必须先跑测试**：
  ```bash
  cd apk_build && SDL_VIDEODRIVER=offscreen python test_ui.py
  ```
  40 项，约 20 秒。**用 SDL offscreen 可以在没有显示器的机器上真跑 Kivy 界面。**

## 三条线程（最容易搞错）

1. **Android UI 线程（Java）** —— WebView 的宿主线程，
   创建 WebView 和调 `evaluateJavascript` **都必须**在它上面（用 `@run_on_ui_thread`）
2. **Kivy/Python 主线程** —— 改控件。**它不是 Android UI 线程**
3. **后台工作线程** —— 发起 JS/网络调用并阻塞等结果

**千万别用 `Clock.schedule_once` 去调 WebView** —— 那是 Kivy 线程，会报
`Calling WebView methods on a different thread than the one it was created on`。

## 三个硬性约束

- 内置音源的**文件名必须纯 ASCII**（中文名经 p4a 打包后会变乱码），
  显示名放 `appenv.SOURCE_TITLES`
- **不要重新声明 Kivy 已有的属性**（如 `font_name`），会把默认值覆盖成 None 导致崩溃
- 判断直链能不能用**必须用 `songinfo.verify()`** —— "能解析出字符串"不等于"能用"

## 我最在意的两件事

1. **不要静默替换用户的选择**。我之前做过"选 QQ 取不到就自动用网易云"，
   这是错的 —— 用户要什么就给什么，不行就如实报错，让他自己决定。
2. **改完要验证测试真的能失败**。我曾经写了个"空跑"的测试
   （因为没 import 导致异常被吞掉，永远通过），差点漏掉一个启动崩溃。

## 现在最该做的（按优先级）

1. 用 `verify()` **重跑音源×平台矩阵** —— 之前那版用"能解析"判断，
   结论是错的（说聚合音源 5 平台全通，实际只有 wy/kw）
2. QQ 依赖第三方代理（`qqresolve.QQ_PROXIES`），随时会失效，
   需要更多备用代理或做成可配置
3. 下载目录目前写死 `Download/落雪音源`，加个"自己选目录"的功能
4. `音源/` 里有 4 个源在 dukpy 下加载失败，但在 APK 的 WebView 下可能能跑，值得测

## 真机出问题时

让用户发 `Android/data/com.lxdl.lxdownloader/files/` 下的
**`diag.log`** 和 **`crash.log`** —— diag.log 里有字体选择、音源加载、
`QQ 解析成功 level=x size=x 耗时x`、下载尝试的 status/Content-Type，基本能直接定位。

---

**（如果我的额度不够了，请务必先把这份提示词和 `交接文档.md` 保存下来再开始。）**
