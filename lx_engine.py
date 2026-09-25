#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lx_engine.py —— 纯 Python 的落雪(LX)音源引擎
================================================================
让音源 .js 文件在【没有 Node.js】的手机上（如 Pydroid 3）直接运行。

原理：
  用 dukpy(内置 QuickJS) 在 Python 进程内执行音源脚本，
  并实现 LX Music 注入的 globalThis.lx 契约：

    lx.on(EVENT_NAMES.request, handler)   ← 脚本注册处理器
    lx.send(EVENT_NAMES.inited, {sources})← 脚本声明平台/音质
    lx.request(url, opts, cb)             ← 脚本发网络请求
                                            → 转到 Python urllib 执行
                                            → 结果回传 JS（同步）

  音源脚本发起 HTTP 时是「同步回调」风格，而 Python 网络是阻塞的，
  所以我们把 Python 的请求函数通过 call_python 暴露给 JS 同步调用，
  天然满足脚本的同步假设。

对外只暴露两个函数：
    load(script_path)          -> 读取音源，返回 (meta, sources)
    get_music_url(...)         -> 换取音频直链
================================================================
"""

import os
import json
import ssl
import gzip
import zlib
import time
import urllib.parse
import urllib.request
import urllib.error

# ---------------------------------------------------------------
# 1. 引擎加载（dukpy / 可选 Node 由外面决定）
# ---------------------------------------------------------------
_ENGINE_ERROR = None
try:
    import dukpy
    HAS_DUKPY = True
except Exception as e:          # pragma: no cover
    dukpy = None
    HAS_DUKPY = False
    _ENGINE_ERROR = str(e)


def engine_available():
    return HAS_DUKPY


def engine_error():
    return _ENGINE_ERROR


# ---------------------------------------------------------------
# 2. 网络层：Python 实现，供 JS 同步调用
# ---------------------------------------------------------------
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

DEFAULT_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


class _Http:
    """无状态 HTTP 客户端。JS 每次请求通过 call_python 调进来。"""

    @staticmethod
    def request(url, opts_json="{}"):
        """
        执行一次 HTTP 请求，返回 dict：
          { statusCode, headers, body }
        body 为字符串（JSON 文本原样返回，由 JS 自己 parse）。
        异常时返回 statusCode=0 并在 body 里给错误信息。
        """
        try:
            opts = json.loads(opts_json) if opts_json else {}
        except Exception:
            opts = {}

        method = str(opts.get("method") or "GET").upper()
        headers = dict(opts.get("headers") or {})

        # --- 组装 body ---
        data = None
        if opts.get("form"):
            data = urllib.parse.urlencode(opts["form"]).encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif opts.get("json") is not None:
            data = json.dumps(opts["json"]).encode()
            headers.setdefault("Content-Type", "application/json")
        elif opts.get("body") is not None:
            b = opts["body"]
            data = b if isinstance(b, bytes) else str(b).encode()

        if data is not None and method == "GET":
            method = "POST"

        headers.setdefault("User-Agent", DEFAULT_UA)
        headers.setdefault("Accept", "*/*")
        # 让服务器返回未压缩内容，省去解压逻辑（部分接口不支持也無妨）
        headers.setdefault("Accept-Encoding", "gzip, deflate")

        timeout = float(opts.get("timeout") or 8000) / 1000.0
        timeout = max(2.0, min(timeout, 30.0))

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                try:
                    if "gzip" in enc:
                        raw = gzip.decompress(raw)
                    elif "deflate" in enc:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                except Exception:
                    pass
                return {
                    "statusCode": r.status,
                    "headers": {k.lower(): v for k, v in r.headers.items()},
                    "body": raw.decode("utf-8", "replace"),
                }
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            return {
                "statusCode": e.code,
                "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
                "body": raw.decode("utf-8", "replace"),
            }
        except Exception as e:
            # statusCode=0 表示连接层面失败，脚本据此判定该 API 不可用
            return {
                "statusCode": 0,
                "headers": {},
                "body": json.dumps({"error": f"{type(e).__name__}: {e}"}),
            }


# ---------------------------------------------------------------
# 3. 沙箱 JS 外壳：实现 lx 对象并把网络转给 Python
# ---------------------------------------------------------------
_SANDBOX_JS = r"""
// ---------- 收集脚本的声明与处理器 ----------
var __LX_INITED = null;
var __LX_HANDLER = null;
var __LX_LOGS = [];

function __lx_log(level, args) {
  try {
    var s = Array.prototype.map.call(args, function (x) {
      if (typeof x === 'string') return x;
      try { return JSON.stringify(x); } catch (e) { return String(x); }
    }).join(' ');
    __LX_LOGS.push(level + ' ' + s);
    if (__LX_LOGS.length > 400) __LX_LOGS.shift();
  } catch (e) {}
}

var console = {
  log:   function () { __lx_log('', arguments); },
  info:  function () { __lx_log('', arguments); },
  debug: function () { __lx_log('DBG', arguments); },
  warn:  function () { __lx_log('WARN', arguments); },
  error: function () { __lx_log('ERR', arguments); },
  trace: function () {}
};

// ---------- 安全序列化：跳过函数与循环引用 ----------
function __lx_safe_json(v) {
  var seen = [];
  try {
    return JSON.stringify(v, function (k, val) {
      if (typeof val === 'function') return undefined;
      if (val && typeof val === 'object') {
        if (seen.indexOf(val) >= 0) return undefined;   // 循环引用直接断开
        seen.push(val);
      }
      return val;
    });
  } catch (e) {
    return 'null';
  }
}

// ---------- 网络：同步调用 Python ----------
function __lx_http(url, opts, callback) {
  var optsJson;
  try { optsJson = JSON.stringify(opts || {}); } catch (e) { optsJson = '{}'; }
  var resp;
  try {
    resp = globalThis.call_python('lx_http_request', String(url), optsJson);
  } catch (e) {
    resp = { statusCode: 0, headers: {}, body: '{"error":"bridge failed"}' };
  }
  if (typeof callback === 'function') {
    if (resp && resp.statusCode) {
      callback(null, resp);
    } else {
      callback(new Error('request failed'), null);
    }
  }
  return resp;
}

// ---------- LX 契约 ----------
var EVENT_NAMES = { request: 'request', inited: 'inited', updateAlert: 'updateAlert' };

var lx = {
  EVENT_NAMES: EVENT_NAMES,
  env: 'mobile',
  version: '2.11.0',
  request: __lx_http,
  on: function (name, handler) {
    if (name === EVENT_NAMES.request) __LX_HANDLER = handler;
  },
  send: function (name, payload) {
    if (name === EVENT_NAMES.inited) __LX_INITED = payload;
  },
  // 部分音源会探测这些工具方法
  utils: {
    buffer: {
      from: function (s, enc) { return s; },
      bufToString: function (b) { return String(b); }
    },
    crypto: {
      md5: function (s) { return String(s); },
      randomBytes: function (n) { return ''; }
    }
  }
};

// 让脚本里的 globalThis.lx 生效
if (typeof globalThis === 'undefined') { var globalThis = this; }
globalThis.lx = lx;
globalThis.window = globalThis;
globalThis.self = globalThis;

// 关键：dukpy 会序列化 evaljs 的最后一条表达式，
// 上面赋值的是自引用对象会导致 "circular reference"。
// 以原始值收尾，强制返回可序列化的结果。
0;
"""


# ---------------------------------------------------------------
# 4. 引擎封装
# ---------------------------------------------------------------
class LxEngine:
    """
    一个音源实例。用 with 语句管理生命周期：

        with LxEngine("音源.js") as eng:
            meta, sources = eng.info()
            url = eng.music_url("wy", "320k", music_info)
    """

    def __init__(self, script_path):
        if not HAS_DUKPY:
            raise RuntimeError(
                "缺少 JS 引擎 dukpy。请先安装：pip install dukpy\n"
                f"（原始错误：{_ENGINE_ERROR}）"
            )
        self.script_path = os.path.abspath(script_path)
        if not os.path.isfile(self.script_path):
            raise FileNotFoundError(f"音源文件不存在: {self.script_path}")

        self._interp = None
        self._logs = []
        self._loaded = False
        self._meta = {}
        self._sources = {}

    # ---------- 生命周期 ----------
    def __enter__(self):
        self.load()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._interp = None

    # ---------- 加载 ----------
    def load(self):
        if self._loaded:
            return self

        src = open(self.script_path, encoding="utf-8", errors="replace").read()

        interp = dukpy.JSInterpreter()

        # 把 Python 网络函数暴露给 JS（call_python 由 dukpy 提供）
        def lx_http_request(url, opts_json="{}"):
            return _Http.request(url, opts_json)

        interp.export_function("lx_http_request", lx_http_request)

        # 注入沙箱外壳
        interp.evaljs(_SANDBOX_JS)

        # 执行音源脚本
        # 注意：用 IIFE 包裹并以 0 收尾，避免 dukpy 序列化脚本最后一条
        # 表达式（可能是自引用对象）而抛 "circular reference"
        wrapped = "(function(){\n" + src + "\n;return 0;})()"
        try:
            interp.evaljs(wrapped)
        except Exception as e:
            raise RuntimeError(f"音源脚本执行失败: {type(e).__name__}: {e}")

        self._interp = interp

        # 取回脚本声明
        inited = self._safe_get("__LX_INITED")
        if not inited:
            raise RuntimeError("音源脚本没有调用 send(inited)，可能不是 LX 音源文件")

        self._meta = inited.get("meta") or {}
        self._sources = inited.get("sources") or {}
        self._loaded = True
        return self

    # ---------- 内部工具 ----------
    def _safe_get(self, expr, default=None):
        """
        在 JS 里求值并转成 Python 对象。
        用 __lx_safe_json 而非裸 JSON.stringify —— 音源的 inited payload
        里可能存在循环引用（如 sources 互相引用），裸 stringify 会直接抛错。
        """
        try:
            val = self._interp.evaljs(f"__lx_safe_json(({expr}))")
        except Exception:
            return default
        if val is None:
            return default
        try:
            return json.loads(val)
        except Exception:
            return default

    def logs(self):
        """取回音源内部日志（用于排错）"""
        return self._safe_get("__LX_LOGS", []) or []

    def info(self):
        """返回 (meta, sources_list)"""
        self.load()
        srcs = []
        for key, v in self._sources.items():
            srcs.append({
                "source": key,
                "name": v.get("name", key),
                "type": v.get("type", "music"),
                "actions": v.get("actions") or [],
                "qualitys": v.get("qualitys") or [],
            })
        return self._meta, srcs

    # ---------- 核心：取直链 ----------
    def music_url(self, source, quality, music_info, timeout_ms=75000):
        """
        向音源请求音频直链。
          source      : 'wy' / 'tx' / 'kw' / 'kg' / 'mg'
          quality     : '128k' / '320k' / 'flac' / ...
          music_info  : dict，需含 id，可选 name/singer/hash/strMediaMid 等
        返回直链字符串；失败抛异常。
        """
        self.load()

        if not self._safe_get("typeof __LX_HANDLER === 'function'", False):
            # JSON.stringify(function) 返回 undefined，换一种探测
            try:
                ok = self._interp.evaljs("typeof __LX_HANDLER === 'function'")
            except Exception:
                ok = False
            if not ok:
                raise RuntimeError("音源没有注册 request 处理器")

        payload = {
            "source": source,
            "action": "musicUrl",
            "info": {"type": quality, "musicInfo": music_info},
        }

        # 关键点：音源 handler 是 async 的，返回 Promise。
        # dukpy 的 evaljs 只跑同步代码，不会自动 drain 微任务队列，
        # 所以这里挂一个「完成标志」在全局，然后由 Python 轮询它。
        js = (
            "(function(){"
            "  globalThis.__LX_ASYNC_DONE = false;"
            "  globalThis.__LX_ASYNC_VAL = '';"
            "  globalThis.__LX_ASYNC_ERR = null;"
            "  var p = " + json.dumps(payload, ensure_ascii=False) + ";"
            "  try {"
            "    var r = __LX_HANDLER(p);"
            "    if (r && typeof r.then === 'function') {"
            "      r.then(function(v){"
            "        globalThis.__LX_ASYNC_VAL = (typeof v==='string') ? v : ((v&&v.url)||'');"
            "        globalThis.__LX_ASYNC_DONE = true;"
            "      }, function(e){"
            "        globalThis.__LX_ASYNC_ERR = String((e&&e.message)||e);"
            "        globalThis.__LX_ASYNC_DONE = true;"
            "      });"
            "    } else {"
            "      globalThis.__LX_ASYNC_VAL = (typeof r==='string') ? r : ((r&&r.url)||'');"
            "      globalThis.__LX_ASYNC_DONE = true;"
            "    }"
            "  } catch (e) {"
            "    globalThis.__LX_ASYNC_ERR = String((e&&e.message)||e);"
            "    globalThis.__LX_ASYNC_DONE = true;"
            "  }"
            "  return 0;"
            "})()"
        )

        self._interp.evaljs(js)

        # 轮询等待 Promise 落定（同时驱动微任务队列）
        deadline = time.time() + timeout_ms / 1000.0
        out = {}
        while True:
            try:
                st = self._interp.evaljs(
                    "JSON.stringify({d: globalThis.__LX_ASYNC_DONE===true,"
                    " v: globalThis.__LX_ASYNC_VAL||'',"
                    " e: globalThis.__LX_ASYNC_ERR||null})"
                )
                out = json.loads(st) if st else {}
            except Exception:
                out = {}
            if out.get("d"):
                out = {"done": True, "value": out.get("v") or "", "error": out.get("e")}
                break
            if time.time() > deadline:
                raise RuntimeError(f"获取直链超时({timeout_ms}ms)")
            time.sleep(0.12)

        if out.get("error"):
            raise RuntimeError(out["error"])
        url = out.get("value") or ""
        if not url:
            raise RuntimeError("音源未返回直链")
        return url
