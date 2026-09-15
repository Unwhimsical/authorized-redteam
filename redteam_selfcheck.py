import os
import re
import sys
import time
import ssl
import socket
from collections import Counter
from urllib.parse import urlparse, urljoin

import requests

CONFIRM = os.getenv("CONFIRM", "")
TARGET = os.getenv("TARGET_BASE_URL", "").rstrip("/")
ALLOWED = os.getenv("ALLOWED_HOST", "")

if CONFIRM != "I_AM_AUTHORIZED":
    print("未确认授权，退出。")
    sys.exit(1)

if not TARGET or not ALLOWED:
    print("缺少 TARGET_BASE_URL 或 ALLOWED_HOST secret。")
    sys.exit(1)

u = urlparse(TARGET)
if u.scheme != "https" or u.hostname != ALLOWED:
    print(f"目标必须是 https://{ALLOWED} 下的地址，当前: {TARGET}")
    sys.exit(1)

BASE = f"https://{u.hostname}"
TIMEOUT = 10

s = requests.Session()
s.headers.update({
    "User-Agent": "AuthorizedSecuritySelfCheck/1.0",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
})

report = []

def log(line=""):
    print(line)
    report.append(line)

def safe_get(url, headers=None):
    try:
        return s.get(
            url,
            headers=headers,
            timeout=TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except Exception as e:
        return None

def head_or_range(url, headers=None):
    try:
        r = s.head(url, headers=headers, timeout=TIMEOUT, allow_redirects=False)
        if r.status_code >= 400:
            r.close()
            r = s.get(
                url,
                headers={**(headers or {}), "Range": "bytes=0-0"},
                timeout=TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
        return r
    except Exception:
        return None

log("# 授权红队自测报告")
log()
log(f"- 目标入口: {TARGET}")
log(f"- 允许主机: {ALLOWED}")
log(f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
log()

# 1. 入口响应
log("## 1. 入口响应")
r = safe_get(TARGET)
if r is None:
    log("- 请求失败")
else:
    body = b""
    if r.status_code == 200:
        body = r.raw.read(512)
    r.close()

    text = body.decode("utf-8", "ignore").lower()
    patterns = []
    for p in ['"apps"', '<plist', '<array', 'itms-services', 'download', 'token', 'ipa']:
        if p in text:
            patterns.append(p)

    log(f"- 状态码: {r.status_code}")
    log(f"- Content-Type: {r.headers.get('content-type', '')}")
    log(f"- 前512字节长度: {len(body)}")
    log(f"- 命中模式: {patterns}")
    log(f"- 响应头: {dict(r.headers)}")
log()

# 2. 敏感路径探测，只记录状态码，不保存内容
log("## 2. 敏感路径探测")
paths = [
    "/.git/HEAD",
    "/install/",
    "/robots.txt",
    "/admin",
    "/FRKToHDckx.php",
    "/appstore.json",
    "/appstore.xml",
    "/api",
    "/api/token",
]
log("| 路径 | 状态码 | Content-Type | Content-Length |")
log("|---|---|---|---|")
for p in paths:
    url = urljoin(BASE + "/", p.lstrip("/"))
    r = safe_get(url)
    if r:
        log(f"| {p} | {r.status_code} | {r.headers.get('content-type','')} | {r.headers.get('content-length','')} |")
        r.close()
    else:
        log(f"| {p} | ERR | | |")
    time.sleep(1)
log()

# 3. 安全头
log("## 3. 安全响应头")
r = safe_get(TARGET)
if r:
    for h in [
        "strict-transport-security",
        "content-security-policy",
        "x-frame-options",
        "x-content-type-options",
        "server",
        "x-powered-by",
    ]:
        log(f"- {h}: {r.headers.get(h, '缺失')}")
    r.close()
else:
    log("- 请求失败")
log()

# 4. TLS 证书
log("## 4. TLS 证书")
try:
    ctx = ssl.create_default_context()
    with socket.create_connection((u.hostname, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=u.hostname) as ssock:
            cert = ssock.getpeercert()
            log(f"- 主体: {cert.get('subject')}")
            log(f"- 颁发者: {cert.get('issuer')}")
            log(f"- 有效期: {cert.get('notBefore')} -> {cert.get('notAfter')}")
            log(f"- SAN: {cert.get('subjectAltName')}")
except Exception as e:
    log(f"- TLS 检查失败: {e}")
log()

# 5. 从入口提取动态链接，只做 HEAD/Range，不下载 IPA
log("## 5. 动态下载链接可访问性")
r = safe_get(TARGET)
links = []
if r and r.status_code == 200:
    body = r.raw.read(8192)
    r.close()
    text = body.decode("utf-8", "ignore")

    for m in re.findall(r'https?://[^\s"\'<>]+', text):
        links.append(m)

    for m in re.findall(r'/[A-Za-z0-9_\-./?=&%]+', text):
        if any(k in m.lower() for k in ["download", "ipa", "plist", "app", "token"]):
            links.append(urljoin(BASE, m))

    links = list(dict.fromkeys(links))[:5]
else:
    if r:
        r.close()

if not links:
    log("- 未从入口提取到链接")
else:
    log("| 链接 | 状态码 | Content-Type | Content-Length | 是否可直接访问 |")
    log("|---|---|---|---|---|")
    for link in links:
        r = head_or_range(link)
        if r:
            log(f"| {link[:90]}... | {r.status_code} | {r.headers.get('content-type','')} | {r.headers.get('content-length','')} | {r.status_code == 200} |")
            r.close()
        else:
            log(f"| {link[:90]}... | ERR | | | |")
        time.sleep(1)
log()

# 6. 轻量限流探测
log("## 6. 轻量限流探测")
codes = []
for _ in range(30):
    r = safe_get(TARGET)
    if r:
        codes.append(r.status_code)
        r.close()
    else:
        codes.append(0)
    time.sleep(0.2)

log(f"- 30 次请求状态码分布: {dict(Counter(codes))}")
log(f"- 是否出现 429: {429 in codes}")
log(f"- 是否出现 403: {403 in codes}")
log()

# 7. 结论提示
log("## 7. 红队结论提示")
log("- 如果 `/.git/HEAD`、`/install/`、`/admin` 返回 200/301/302，说明敏感路径暴露。")
log("- 如果 `/appstore` 直接返回应用列表，说明软件源列表未做鉴权。")
log("- 如果动态链接 HEAD 返回 200，说明链接可能可被直接访问。")
log("- 如果 30 次请求没有 429，说明限流可能缺失。")
log("- 如果下载链接不绑定设备、可重放，需要进一步做 token 重放测试。")

with open("report.md", "w", encoding="utf-8") as f:
    f.write("\n".join(report))

print("报告已生成: report.md")