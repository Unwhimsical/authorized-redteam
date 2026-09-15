import os
import re
import sys
import time
import ssl
import json
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
TIMEOUT = 15
MAX_BODY = 64 * 1024  # 每个响应最多抓 64KB

s = requests.Session()
s.headers.update({
    "User-Agent": "AuthorizedSecuritySelfCheck/1.0",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
})

report = []
raw_files = []


def log(line=""):
    print(line)
    report.append(line)


def fetch(url, headers=None, save_as=None):
    try:
        r = s.get(
            url,
            headers=headers,
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        body = r.content[:MAX_BODY]
        if save_as:
            with open(save_as, "w", encoding="utf-8", errors="replace") as f:
                f.write(f"URL: {url}\n")
                f.write(f"STATUS: {r.status_code}\n")
                f.write(f"HEADERS: {json.dumps(dict(r.headers), indent=2, ensure_ascii=False)}\n")
                f.write("BODY:\n")
                f.write(body.decode("utf-8", "replace"))
            raw_files.append(save_as)
        return r, body
    except Exception as e:
        return None, str(e).encode()


def short(s, n=1200):
    s = s.replace("\r", "")
    if len(s) > n:
        return s[:n] + f"\n... [截断，共 {len(s)} 字节]"
    return s


log("# 授权红队自测报告 v2")
log()
log(f"- 目标入口: {TARGET}")
log(f"- 允许主机: {ALLOWED}")
log(f"- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
log()

# ===== 1. 入口响应全文 =====
log("## 1. 入口响应全文")
r, body = fetch(TARGET, save_as="raw_appstore.txt")
if r is None:
    log(f"- 请求失败: {body.decode('utf-8', 'replace')}")
else:
    text = body.decode("utf-8", "replace")
    log(f"- 状态码: {r.status_code}")
    log(f"- Content-Type: {r.headers.get('content-type', '')}")
    log(f"- Content-Length: {r.headers.get('content-length', '未声明')}")
    log(f"- 实际抓到: {len(body)} 字节")
    log()
    log("### 响应体（前 1200 字节）")
    log("```")
    log(short(text))
    log("```")

    # 尝试解析 JSON
    try:
        data = json.loads(text)
        log()
        log("### JSON 结构分析")
        if isinstance(data, dict):
            log(f"- 顶层 keys: {list(data.keys())}")
            for k, v in data.items():
                if isinstance(v, list):
                    log(f"  - `{k}`: list，长度 {len(v)}")
                    if v and isinstance(v[0], dict):
                        log(f"    - 元素字段: {list(v[0].keys())}")
                        log(f"    - 第一个元素示例: {json.dumps(v[0], ensure_ascii=False)[:300]}")
                elif isinstance(v, dict):
                    log(f"  - `{k}`: dict，keys {list(v.keys())}")
                else:
                    log(f"  - `{k}`: {type(v).__name__} = {str(v)[:100]}")
        elif isinstance(data, list):
            log(f"- 顶层是 list，长度 {len(data)}")
            if data and isinstance(data[0], dict):
                log(f"- 元素字段: {list(data[0].keys())}")
                log(f"- 第一个元素: {json.dumps(data[0], ensure_ascii=False)[:300]}")
    except Exception:
        log()
        log("- 响应不是 JSON")
log()

# ===== 2. robots.txt 全文 =====
log("## 2. robots.txt 全文")
r, body = fetch(urljoin(BASE + "/", "robots.txt"), save_as="raw_robots.txt")
if r:
    log(f"- 状态码: {r.status_code}")
    log("```")
    log(short(body.decode("utf-8", "replace"), 2000))
    log("```")
else:
    log("- 请求失败")
log()

# ===== 3. /api 根返回 =====
log("## 3. /api 根返回")
r, body = fetch(urljoin(BASE + "/", "api"), save_as="raw_api.txt")
if r:
    log(f"- 状态码: {r.status_code}")
    log(f"- Content-Type: {r.headers.get('content-type', '')}")
    log("```")
    log(short(body.decode("utf-8", "replace"), 800))
    log("```")
else:
    log("- 请求失败")
log()

# ===== 4. /api 子路由枚举 =====
log("## 4. /api 子路由探测")
sub = [
    "apps", "list", "applist", "app/list", "appstore",
    "user", "users", "admin", "config", "settings",
    "v1", "v1/apps", "v1/list", "v1/appstore",
    "source", "sources", "repo", "repos",
    "download", "downloads", "get", "fetch",
    "login", "auth", "token", "sign", "verify",
    "ping", "health", "status", "info", "version",
]
log("| 路径 | 状态码 | Content-Type | 长度 |")
log("|---|---|---|---|")
for p in sub:
    url = urljoin(BASE + "/", f"api/{p}")
    try:
        r = s.get(url, timeout=TIMEOUT, allow_redirects=False)
        log(f"| /api/{p} | {r.status_code} | {r.headers.get('content-type','')} | {r.headers.get('content-length','?')} |")
    except Exception:
        log(f"| /api/{p} | ERR | | |")
    time.sleep(0.3)
log()

# ===== 5. 后台入口详情 =====
log("## 5. 后台入口详情")
for p in ["/FRKToHDckx.php", "/admin", "/login", "/manage"]:
    url = urljoin(BASE + "/", p.lstrip("/"))
    r, body = fetch(url, save_as=f"raw_admin_{p.replace('/', '_')}.txt")
    if r:
        text = body.decode("utf-8", "replace")
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            title = m.group(1).strip()[:80]

        has_form = "<form" in text.lower()
        has_password = 'type="password"' in text.lower()
        has_captcha = any(k in text.lower() for k in ["captcha", "verify", "验证码", "geetest", "recaptcha"])
        has_2fa = any(k in text.lower() for k in ["2fa", "totp", "otp", "google authenticator"])

        log(f"### {p}")
        log(f"- 状态码: {r.status_code}")
        log(f"- Content-Type: {r.headers.get('content-type','')}")
        log(f"- 标题: {title or '无'}")
        log(f"- 有表单: {has_form}")
        log(f"- 有密码框: {has_password}")
        log(f"- 有验证码: {has_captcha}")
        log(f"- 有 2FA: {has_2fa}")
        log(f"- Server: {r.headers.get('server','')}")
        log(f"- Set-Cookie: {r.headers.get('set-cookie','')}")
        log()
    else:
        log(f"### {p}\n- 请求失败\n")
    time.sleep(0.3)
log()

# ===== 6. 动态链接提取与展示 =====
log("## 6. 动态链接提取")
r, body = fetch(TARGET)
links = []
if r and r.status_code == 200:
    text = body.decode("utf-8", "replace")
    # 提取 URL
    for m in re.findall(r'https?://[^\s"\'<>\\]+', text):
        links.append(m)
    # 提取疑似路径
    for m in re.findall(r'/[A-Za-z0-9_\-./?=&%]{3,}', text):
        if any(k in m.lower() for k in ["download", "ipa", "plist", "app", "token", "dl", "file"]):
            links.append(urljoin(BASE, m))

links = list(dict.fromkeys(links))

log(f"共提取到 {len(links)} 个候选链接")
log()
log("| # | 链接 | 状态码 | Content-Type | 是否可直接访问 |")
log("|---|---|---|---|---|")
for i, link in enumerate(links[:30], 1):
    try:
        r2 = s.head(link, timeout=TIMEOUT, allow_redirects=False)
        if r2.status_code >= 400:
            r2 = s.get(link, timeout=TIMEOUT, allow_redirects=False, stream=True, headers={"Range": "bytes=0-0"})
        log(f"| {i} | `{link[:80]}` | {r2.status_code} | {r2.headers.get('content-type','')} | {r2.status_code == 200} |")
        r2.close()
    except Exception as e:
        log(f"| {i} | `{link[:80]}` | ERR | | |")
    time.sleep(0.2)
log()

# ===== 7. 限流探测 =====
log("## 7. 限流探测")
codes = []
for _ in range(50):
    try:
        r = s.get(TARGET, timeout=TIMEOUT, allow_redirects=False)
        codes.append(r.status_code)
    except Exception:
        codes.append(0)
    time.sleep(0.15)

log(f"- 50 次请求状态码分布: {dict(Counter(codes))}")
log(f"- 出现 429: {429 in codes}")
log(f"- 出现 403: {403 in codes}")
log(f"- 出现 503: {503 in codes}")
log()

# ===== 8. Cookie 安全属性 =====
log("## 8. Cookie 安全属性")
r, _ = fetch(TARGET)
if r:
    sc = r.headers.get("set-cookie", "")
    if sc:
        log(f"- 原始 Set-Cookie: `{sc}`")
        log(f"- 有 Secure: {'secure' in sc.lower()}")
        log(f"- 有 HttpOnly: {'httponly' in sc.lower()}")
        log(f"- 有 SameSite: {'samesite' in sc.lower()}")
        m = re.search(r"samesite=(\w+)", sc, re.I)
        if m:
            log(f"- SameSite 值: {m.group(1)}")
    else:
        log("- 无 Set-Cookie")
log()

# ===== 9. 安全头 =====
log("## 9. 安全响应头")
r, _ = fetch(TARGET)
if r:
    for h in [
        "strict-transport-security",
        "content-security-policy",
        "x-frame-options",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
        "cross-origin-opener-policy",
    ]:
        log(f"- {h}: {r.headers.get(h, '缺失')}")
log()

# ===== 10. 子域与 CORS =====
log("## 10. CORS 探测")
for origin in [
    "https://evil.example.com",
    "null",
    f"https://{ALLOWED}",
]:
    try:
        r = s.get(TARGET, headers={"Origin": origin}, timeout=TIMEOUT, allow_redirects=False)
        acao = r.headers.get("access-control-allow-origin", "缺失")
        acac = r.headers.get("access-control-allow-credentials", "缺失")
        log(f"- Origin: {origin} → ACAO: {acao}, ACAC: {acac}")
    except Exception as e:
        log(f"- Origin: {origin} → 失败 {e}")
log()

# ===== 11. 结论 =====
log("## 11. 红队结论")
concl = []

if r is None:
    concl.append("- 入口请求失败，检查网络或目标是否存活")
else:
    # appstore 裸奔
    if r.status_code == 200 and "json" in r.headers.get("content-type", "").lower():
        concl.append("- 🔴 `/appstore` 直接返回 JSON，软件源列表裸奔")

    if 429 not in codes and 403 not in codes:
        concl.append("- 🔴 50 次请求无限流，红队可批量枚举")

    sc = r.headers.get("set-cookie", "")
    if sc and "secure" not in sc.lower():
        concl.append("- 🟠 Cookie 缺 Secure 标志")
    if sc and "samesite" not in sc.lower():
        concl.append("- 🟠 Cookie 缺 SameSite 标志")

    if not r.headers.get("content-security-policy"):
        concl.append("- 🟠 缺 CSP，XSS 无二次防线")
    if not r.headers.get("x-frame-options"):
        concl.append("- 🟠 缺 X-Frame-Options，后台可被点击劫持")

for line in concl:
    log(line)

log()
log("## 12. 附：原始响应文件")
log("已保存到 raw_*.txt，可在 Artifact 里下载查看全文。")

with open("report.md", "w", encoding="utf-8") as f:
    f.write("\n".join(report))

print("报告已生成: report.md")
print("原始响应:", raw_files)