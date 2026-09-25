"""HTTP 工具。

关于证书校验
------------
真机日志里出现大量：
  SSLCertVerificationError: certificate verify failed:
  self-signed certificate in certificate chain
这通常是因为手机所在网络有代理/网关做了 SSL 拦截（企业网、部分运营商、
抓包工具、某些 VPN），链里带了自签证书，Python 默认的严格校验就会失败。

策略：先用「严格校验」请求；只有在证书校验失败时才退回「不校验」重试，
并且记一条诊断日志。这样正常网络仍然是安全校验，异常网络也能用。
"""
import json
import ssl
import urllib.error
import urllib.request

from appenv import diag, log

UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

# 懒加载，避免启动时就去建 SSL context
_UNVERIFIED = None
_cert_problem_hosts = set()


def _unverified_ctx():
    global _UNVERIFIED
    if _UNVERIFIED is None:
        _UNVERIFIED = ssl._create_unverified_context()
    return _UNVERIFIED


def urlopen(url_or_req, headers=None, timeout=20):
    """打开 URL。证书校验失败时自动退回不校验模式重试。"""
    if isinstance(url_or_req, str):
        h = {"User-Agent": UA}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url_or_req, headers=h)
    else:
        req = url_or_req

    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLError) and \
                "CERTIFICATE_VERIFY_FAILED" not in str(e):
            raise
        host = getattr(req, "host", "") or getattr(req, "full_url", "")
        if host not in _cert_problem_hosts:
            _cert_problem_hosts.add(host)
            diag("SSL 证书校验失败，改用不校验模式重试: %s" % host)
        log("SSL 证书校验失败，退回不校验模式:", host)
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=_unverified_ctx())
    except ssl.SSLError:
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=_unverified_ctx())


def get_json(url, headers=None, timeout=20):
    with urlopen(url, headers=headers, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))
