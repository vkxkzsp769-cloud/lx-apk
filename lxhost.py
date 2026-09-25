"""注入到 WebView 里的「落雪 lx 契约」宿主实现（纯 JavaScript）。

这段代码运行在 Android WebView 中，负责：
  - 实现 globalThis.lx（request / on / send / env / version）
  - 把音源脚本 eval 起来，拿到它注册的 request 处理器
  - 对外暴露 lxInit(code) / lxGetUrl(...) / lxPoll() 供 Python 调用

要点：
  - 网络请求用 XMLHttpRequest（WebView 里没有 fetch 到 file:// 的权限问题，
    但 XHR 更稳）
  - http:// 会先升级成 https:// 再试，失败再回退原始 http:// ——
    Android targetSdk>=28 默认禁止明文流量，直接发 http 会被系统拦掉
  - 所有结果用 JSON 字符串返回，避免桥接层序列化问题
"""

LX_HOST_JS = r"""

var __HANDLER = null, __INITED = null, __RESULT = null, __LOGS = [];

function __mkRequest() {
  // 明文 http 的处理：
  // Android targetSdk>=28 默认禁止明文流量（network security policy），
  // WebView 里对 http:// 发 XHR 会被系统直接拦掉，表现为 onerror/status 0。
  // 音源里有 11 个 http:// 主机（y.qq.com / dl.stream.qqmusic.qq.com /
  // www.kugou.com / music.migu.cn 等）。
  //
  // 策略：先把 http:// 升级为 https:// 再请求（这些接口绝大多数都支持 https），
  //       若失败（网络层错误 / status 0）再回退原始 http://（万一明文是放开的）。
  // 这样无论 manifest 里有没有 usesCleartextTraffic 都能工作。
  function __upgrade(u) {
    return /^http:\/\//i.test(u) ? u.replace(/^http:\/\//i, 'https://') : null;
  }

  return function lxRequest(url, options, callback) {
    if (typeof options === 'function') { callback = options; options = {}; }
    options = options || {};
    var method = (options.method || ((options.body || options.form) ? 'POST' : 'GET')).toUpperCase();
    var body = null;
    if (options.form) {
      var parts = [];
      for (var k in options.form) parts.push(encodeURIComponent(k)+'='+encodeURIComponent(options.form[k]));
      body = parts.join('&');
    } else if (options.json) { body = JSON.stringify(options.json); }
    else if (options.body != null) { body = String(options.body); }

    var upgraded = __upgrade(url);

    function attempt(target, allowFallback) {
      var xhr = new XMLHttpRequest();
      var settled = false;
      function fail(err) {
        if (settled) return;
        settled = true;
        // https 失败 -> 回退原始 http
        if (allowFallback) { attempt(url, false); return; }
        callback(err, null);
      }
      try { xhr.open(method, target, true); }
      catch (e) { fail(new Error('bad url')); return; }

      var h = options.headers || {};
      for (var hk in h) { try { xhr.setRequestHeader(hk, h[hk]); } catch (e) {} }
      if (body && !h['Content-Type'] && !h['content-type'])
        try { xhr.setRequestHeader('Content-Type','application/x-www-form-urlencoded'); } catch (e) {}
      xhr.timeout = options.timeout || 8000;

      xhr.onreadystatechange = function () {
        if (xhr.readyState !== 4) return;
        if (settled) return;
        // status 0 = 网络层失败（含被明文策略拦截）
        if (xhr.status === 0) { fail(new Error('network')); return; }
        settled = true;
        callback(null, { statusCode: xhr.status, headers: {}, body: xhr.responseText });
      };
      xhr.ontimeout = function () { fail(new Error('timeout')); };
      xhr.onerror = function () { fail(new Error('network')); };
      try { xhr.send(body); } catch (e) { fail(e); }
    }

    attempt(upgraded || url, !!upgraded);
  };
}

var EVENT_NAMES = { request:'request', inited:'inited', updateAlert:'updateAlert' };
var lx = {
  EVENT_NAMES: EVENT_NAMES, env:'mobile', version:'2.11.0',
  request: __mkRequest(),
  on: function (n, h) { if (n === EVENT_NAMES.request) __HANDLER = h; },
  send: function (n, p) { if (n === EVENT_NAMES.inited) __INITED = p; },
  utils: {
    buffer: { from: function (s) { return s; } },
    crypto: { md5: function (s) { return s; } }
  }
};
window.lx = lx;

function lxInit(code) {
  __HANDLER = null; __INITED = null; __RESULT = null;
  window.console = { log: function(){}, error: function(){}, warn: function(){},
                     debug: function(){}, info: function(){} };
  try { (new Function(code))(); } catch (e) { return 'ERR:' + e.message; }
  if (!__INITED) return 'ERR:no-inited';
  try { return JSON.stringify(__INITED); } catch (e) { return 'ERR:serialize'; }
}

function lxGetUrl(source, quality, musicInfoJson) {
  __RESULT = { done: false, url: '', error: null };
  if (!__HANDLER) { __RESULT.error = 'no-handler'; __RESULT.done = true; return 'ERR'; }
  try {
    var mi = JSON.parse(musicInfoJson);
    var p = __HANDLER({ source: source, action: 'musicUrl',
                        info: { type: quality, musicInfo: mi } });
    if (p && typeof p.then === 'function') {
      p.then(function (u) {
        __RESULT.url = (typeof u === 'string') ? u : ((u && u.url) || '');
        __RESULT.done = true;
      }, function (e) {
        __RESULT.error = String((e && e.message) || e); __RESULT.done = true;
      });
    } else {
      __RESULT.url = (typeof p === 'string') ? p : ''; __RESULT.done = true;
    }
  } catch (e) { __RESULT.error = String(e.message || e); __RESULT.done = true; }
  return 'STARTED';
}

function lxPoll() { return JSON.stringify(__RESULT || {done:false,url:'',error:null}); }
"""
