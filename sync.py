#!/usr/bin/env python3
"""
iOS Manifest Sync — 输出全能签兼容的 JSON 源。
"""

import json, os, sys, urllib.parse, urllib.request
from pathlib import Path

UPSTREAM_MANIFEST = os.environ.get("UPSTREAM_MANIFEST", "")
PAGES_BASE_URL   = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
REWRITE_MODE     = os.environ.get("REWRITE_MODE", "proxy").lower()
PROXY_BASE_URL   = os.environ.get("PROXY_BASE_URL", "").rstrip("/")
VERIFY_LINKS     = os.environ.get("VERIFY_LINKS", "true").lower() == "true"

SOURCE_NAME      = os.environ.get("SOURCE_NAME", "iOS 镜像源")
SOURCE_JSON      = Path("app.json")          # 全能签识别的主文件
MANIFEST_PLIST   = Path("manifest.plist")    # 保留，供 Safari 直接安装用

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ipa-manifest-sync/1.0"
TIMEOUT_FETCH = 60
TIMEOUT_HEAD  = 20


def fetch_upstream() -> bytes:
    print(f"[*] Fetching upstream: {UPSTREAM_MANIFEST}")
    req = urllib.request.Request(UPSTREAM_MANIFEST, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_FETCH) as r:
        return r.read()


def _pick(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return default


def parse_upstream(raw: bytes) -> list:
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[FATAL] 上游不是有效 JSON: {e}")
        return []

    if isinstance(data, list):
        apps_raw = data
    elif isinstance(data, dict):
        apps_raw = data.get("apps") or data.get("items") or data.get("data") or []
    else:
        return []

    apps = []
    for item in apps_raw:
        if not isinstance(item, dict):
            continue
        title   = _pick(item, "name", "title", "appName")
        version = _pick(item, "version", "bundle-version", "bundleVersion")
        src_url = _pick(item, "downloadURL", "download_url", "downloadUrl", "url", "ipa")
        icon    = _pick(item, "iconURL", "icon_url", "icon", "iconUrl")
        bundle_id = _pick(item, "bundleId", "bundle-identifier", "bundleIdentifier", "identifier")

        if not title or not src_url:
            continue

        if not bundle_id or "/" in bundle_id or " " in bundle_id:
            slug = f"{title}-{version}".replace(" ", "_").replace("/", "_")
            bundle_id = f"com.mirror.{slug}"

        apps.append({
            "title": title,
            "bundle_id": bundle_id,
            "version": version,
            "source_url": src_url,
            "icon_url": icon,
        })

    print(f"[*] Parsed {len(apps)} app(s).")
    return apps


def rewrite_url(app):
    if REWRITE_MODE == "passthrough":
        return app["source_url"]
    if not PROXY_BASE_URL:
        raise RuntimeError("REWRITE_MODE=proxy 但 PROXY_BASE_URL 未设置")
    return f"{PROXY_BASE_URL}/ipa/{urllib.parse.quote(app['bundle_id'] + '.ipa', safe='')}"


def url_alive(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT_HEAD) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def build_source_json(apps):
    """生成全能签可识别的 JSON 软件源。"""
    return json.dumps({
        "name": SOURCE_NAME,
        "apps": [
            {
                "name": a["title"],
                "version": a["version"] or "1.0",
                "bundleId": a["bundle_id"],
                "downloadURL": a["public_url"],
                "iconURL": a.get("icon_url") or "",
            }
            for a in apps
        ],
    }, ensure_ascii=False, indent=2).encode("utf-8")


def build_manifest_plist(apps):
    """保留标准 manifest.plist，供 Safari 或其他工具直接安装。"""
    import plistlib
    items = []
    for a in apps:
        items.append({
            "assets": [{"kind": "software-package", "url": a["public_url"]}],
            "metadata": {
                "bundle-identifier": a["bundle_id"],
                "bundle-version": a["version"] or "1.0",
                "kind": "software",
                "title": a["title"],
            },
        })
    return plistlib.dumps({"items": items}, fmt=plistlib.FMT_XML)


def main() -> int:
    try:
        raw = fetch_upstream()
    except Exception as e:
        print(f"[FATAL] Fetch upstream failed: {e}")
        return 1

    apps = parse_upstream(raw)
    if not apps:
        print("[WARN] 没有解析到任何带下载链接的应用。")
        return 0

    for a in apps:
        a["public_url"] = rewrite_url(a)

    if VERIFY_LINKS:
        print("[*] Verifying source URLs ...")
        verified = []
        for a in apps:
            ok = url_alive(a["source_url"])
            print(f"  [{'OK' if ok else 'X'}] {a['title']} {a['version']}")
            if ok:
                verified.append(a)
        apps = verified

    SOURCE_JSON.write_bytes(build_source_json(apps))
    MANIFEST_PLIST.write_bytes(build_manifest_plist(apps))
    print(f"[OK] Wrote {SOURCE_JSON} and {MANIFEST_PLIST} ({len(apps)} apps).")
    return 0


if __name__ == "__main__":
    sys.exit(main())