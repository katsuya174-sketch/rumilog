"""Grounding citation先ページの安全な取得と「サイトの名乗り」の抽出(Step45.5)。

このモジュールはブランドの判断をしない(product_collection_pipelineの
公式情報源判定が、ここで抽出したサイト自身の名乗りを使って判定する)。

SSRF対策(サーバーから外部URLを取得するため):
- http/httpsのみ、port 80/443のみ
- DNS解決結果の全IPを検査し、global unicastのみ許可
  (loopback/private/link-local/multicast/reserved/unspecified等を拒否)
- 検査したIPへ直接接続する(DNS rebinding対策)。HTTPSはurllib3の
  server_hostname/assert_hostnameでSNIと証明書のホスト名検証を元の
  ホスト名で維持し、CAはcertifi(requestsと同じ)を使う
- リダイレクトは手動で処理し、各段階でURL・IPを再検査(最大5回)
- 接続/読み込みtimeoutと全体の締め切り
- 受信は最大1MB(展開後のサイズで数える)
- text/htmlのみ
- Cookie・認証情報は送らない。環境変数のproxy設定は使わない(urllib3を直接使用)
"""

import codecs
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import certifi
import urllib3

MAX_REDIRECTS = 5
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 8
TOTAL_DEADLINE_SECONDS = 20
MAX_RESPONSE_BYTES = 1_000_000
_ALLOWED_PORTS = {"http": 80, "https": 443}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_USER_AGENT = "Mozilla/5.0 (compatible; lumilog-citation-check/1.0)"

# 取得結果のstatus
OK = "ok"                    # HTML取得成功
REJECTED = "rejected"        # 安全要件・形式要件で拒否(このsourceは使わない)
UNVERIFIABLE = "unverifiable"  # timeout/403等で確認できない(次のsourceへ)


class _Rejected(Exception):
    pass


# テストではconftest.pyがこれを差し替え、実DNSへ到達させない。
_getaddrinfo = socket.getaddrinfo


def _resolve_global_ips(host):
    """hostのDNS解決結果がすべてglobal unicastならそのIP一覧を返す。"""
    try:
        infos = _getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise _Rejected("dns_error")
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (not ip.is_global or ip.is_multicast or ip.is_loopback or ip.is_private
                or ip.is_link_local or ip.is_reserved or ip.is_unspecified):
            raise _Rejected(f"non_global_ip:{ip}")
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise _Rejected("dns_no_address")
    return ips


def _check_url(url):
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_PORTS:
        raise _Rejected(f"scheme_not_allowed:{scheme}")
    host = parts.hostname
    if not host:
        raise _Rejected("no_host")
    try:
        port = parts.port
    except ValueError:
        raise _Rejected("invalid_port")
    if port is not None and port != _ALLOWED_PORTS[scheme]:
        raise _Rejected(f"port_not_allowed:{port}")
    if parts.username or parts.password:
        raise _Rejected("credentials_in_url")
    try:
        ipaddress.ip_address(host)
        raise _Rejected("ip_literal_host")
    except ValueError:
        pass
    return scheme, host, _ALLOWED_PORTS[scheme], (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def _open_url(scheme, host, port, path, ip):
    """検査済みIPへ接続してGETする(リダイレクトは追わない)。テストでは
    conftest.pyでこの関数を差し替え、実ネットワークへ到達させない。"""
    timeout = urllib3.Timeout(connect=CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS)
    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            str(ip), port, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
            server_hostname=host, assert_hostname=host, timeout=timeout, retries=False, maxsize=1,
        )
    else:
        pool = urllib3.HTTPConnectionPool(str(ip), port, timeout=timeout, retries=False, maxsize=1)
    return pool.urlopen(
        "GET", path, redirect=False, preload_content=False, retries=False,
        headers={"Host": host, "User-Agent": _USER_AGENT,
                 "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "gzip, deflate"},
    )


def _detect_charset(content_type, head_bytes):
    match = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    candidate = match.group(1) if match else None
    if not candidate:
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", head_bytes[:4096], re.I)
        candidate = meta.group(1).decode("ascii", "ignore") if meta else None
    try:
        return codecs.lookup(candidate).name if candidate else "utf-8"
    except LookupError:
        return "utf-8"


def fetch_html(url):
    """urlを安全要件付きで取得する。戻り値:
    {"status": ok/rejected/unverifiable, "reason", "final_url", "final_domain", "html"(okのみ)}"""
    deadline = time.monotonic() + TOTAL_DEADLINE_SECONDS
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            scheme, host, port, path = _check_url(current)
            ip = _resolve_global_ips(host)[0]
        except _Rejected as e:
            return {"status": REJECTED, "reason": str(e), "final_url": current}
        try:
            response = _open_url(scheme, host, port, path, ip)
        except (urllib3.exceptions.TimeoutError, socket.timeout):
            return {"status": UNVERIFIABLE, "reason": "timeout", "final_url": current}
        except urllib3.exceptions.SSLError:
            return {"status": REJECTED, "reason": "tls_verification_failed", "final_url": current}
        except (urllib3.exceptions.HTTPError, OSError):
            return {"status": UNVERIFIABLE, "reason": "connection_error", "final_url": current}
        try:
            if response.status in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    return {"status": REJECTED, "reason": "redirect_without_location", "final_url": current}
                current = urljoin(current, location)
                continue
            if response.status != 200:
                return {"status": UNVERIFIABLE, "reason": f"http_{response.status}", "final_url": current}
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type.lower():
                return {"status": REJECTED, "reason": "non_html", "final_url": current}
            body = b""
            for chunk in response.stream(65536, decode_content=True):
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    return {"status": REJECTED, "reason": "response_too_large", "final_url": current}
                if time.monotonic() > deadline:
                    return {"status": UNVERIFIABLE, "reason": "timeout", "final_url": current}
            html_text = body.decode(_detect_charset(content_type, body), errors="replace")
            return {"status": OK, "reason": None, "final_url": current,
                    "final_domain": host.lower(), "html": html_text}
        except (urllib3.exceptions.TimeoutError, socket.timeout):
            return {"status": UNVERIFIABLE, "reason": "timeout", "final_url": current}
        except (urllib3.exceptions.HTTPError, OSError):
            return {"status": UNVERIFIABLE, "reason": "connection_error", "final_url": current}
        finally:
            response.release_conn()
    return {"status": REJECTED, "reason": "too_many_redirects", "final_url": current}


# ===== サイトの名乗りの抽出 =====
# 使うのはサイト自身を表す値だけ: og:site_name、トップレベル(または@graph直下)の
# JSON-LD Organization/Corporation/WebSite.name、<title>の最後の区切り以降。
# Product.brand/Brand・商品名・本文・title前半の商品部分は抽出しない。
# Step45.10: title_site_nameは診断用に抽出・保存するだけで、公式判定の
# 肯定材料には使わない(product_collection_pipeline._page_identity_confirms_brand)。
_SITE_IDENTITY_TYPES = {"Organization", "Corporation", "WebSite"}
_TITLE_SEPARATOR_RE = re.compile(r"\s*(?:\||｜| - | – | — )\s*")
_MAX_FIELD_LENGTH = 200


class _MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.canonical = None
        self.og_site_name = None
        self.jsonld_blobs = []
        self._in_title = False
        self._in_jsonld = False
        self._buffer = []

    def handle_starttag(self, tag, attrs):
        attrs = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title" and self.title is None:
            self._in_title, self._buffer = True, []
        elif tag == "link" and "canonical" in attrs.get("rel", "").lower().split() and self.canonical is None:
            self.canonical = attrs.get("href") or None
        elif tag == "meta" and attrs.get("property", "").lower() == "og:site_name" and self.og_site_name is None:
            self.og_site_name = attrs.get("content") or None
        elif tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._in_jsonld, self._buffer = True, []

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self.title = "".join(self._buffer).strip()
            self._in_title = False
        elif tag == "script" and self._in_jsonld:
            self.jsonld_blobs.append("".join(self._buffer))
            self._in_jsonld = False

    def handle_data(self, data):
        if self._in_title or self._in_jsonld:
            self._buffer.append(data)


def _top_level_jsonld_names(blobs):
    names = []
    for blob in blobs:
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        expanded = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)
        for item in expanded:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            types = set(types) if isinstance(types, list) else {types}
            name = item.get("name")
            if types & _SITE_IDENTITY_TYPES and isinstance(name, str) and name.strip():
                names.append(name.strip()[:_MAX_FIELD_LENGTH])
    return names[:5]


def extract_site_identity(html_text):
    """HTMLから、サイト自身の名乗りとcanonicalだけを抽出する。"""
    parser = _MetadataParser()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        pass
    title_site_name = None
    if parser.title:
        parts = [p for p in _TITLE_SEPARATOR_RE.split(parser.title) if p.strip()]
        if len(parts) >= 2:
            title_site_name = parts[-1].strip()[:_MAX_FIELD_LENGTH]
    return {
        "canonical": (parser.canonical or None),
        "og_site_name": (parser.og_site_name or "").strip()[:_MAX_FIELD_LENGTH] or None,
        "jsonld_names": _top_level_jsonld_names(parser.jsonld_blobs),
        "title_site_name": title_site_name,
    }
