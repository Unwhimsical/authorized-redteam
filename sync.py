#!/usr/bin/env python3
"""
iOS Manifest Sync — Link-only mirror for GitHub Pages.

上游是 JSON 源（非标准 plist），本脚本负责：
  1. 拉取上游 JSON
  2. 解析 apps 数组，提取 title / version / downloadURL / iconURL
  3. 按 REWRITE_MODE 重写下载 URL（passthrough 或 proxy）
  4. 输出下游标准 iOS manifest.plist + apps.json 索引
"""

import json
import os
import plistlib
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
UPSTREAM_MANIFEST = os.environ.get(
    "UPSTREAM_MANIFEST", "http://app.ioskuka.com/appstore/manifest.plist"
)
PAGES_BASE_URL   = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
REWRITE_MODE     = os.environ.get("REWRITE_MODE", "proxy").lower()
PROXY_BASE_URL   = os.environ.get("PROXY_BASE_URL", "").rstrip("/")
VERIFY_LINKS     = os.environ.get("VERIFY_LINKS", "true").lower() == "true"

MANIFEST_LOCAL = Path("manifest.plist")
INDEX_LOCAL    = Path("apps.json")

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ipa-manifest-sync/1.0"
TIMEOUT_FETCH = 60
TIMEOUT_HEAD  = 20


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_upstream() -> bytes:
    print(f"[*] Fetching upstream: {UPSTREAM_MANIFEST}")
    req = urllib.request.Request(UPSTREAM_MANIFEST, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_FETCH) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Parse upstream (JSON)
# ---------------------------------------------------------------------------
def _pick(d: dict, *keys: str, default: str = "") -> str:
    """按顺序尝试多个可能的 key，返回第一个非空字符串。"""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return default


def parse_upstream(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[FATAL] 上游内容不是有效 JSON: {e}")
        print(f"[DEBUG] 前 300 字节: {raw[:300]!r}")
        return []

    # 上游 JSON 顶层可能直接是 list，也可能包在 apps/items/data 字段里
    if isinstance(data, list):
        apps_raw = data
    elif isinstance(data, dict):
        apps_raw = (
            data.get("apps")
            or data.get("items")
            or data.get("data")
            or data.get("list")
            or []
        )
    else:
        print(f"[FATAL] 未知的 JSON 顶层类型: {type(data).__name__}")
        return []

    if not isinstance(apps_raw, list):
        print("[FATAL] apps 字段不是数组")
        return []

    apps: list[dict] = []
    skipped_no_url = 0
    skipped_announce = 0

    for item in apps_raw:
        if not isinstance(item, dict):
            continue

        # 跳过公告/纯文本条目（type=0 且无下载链接）
        item_type = item.get("type")
        title     = _pick(item, "name", "title", "appName")
        version   = _pick(item, "version", "bundle-version", "bundleVersion")
        src_url   = _pick(item, "downloadURL", "download_url", "downloadUrl",
                          "url", "ipa", "ipaURL")
        icon_url  = _pick(item, "iconURL", "icon_url", "icon", "iconUrl")
        bundle_id = _pick(item, "bundleId", "bundle-identifier",
                          "bundleIdentifier", "identifier", "id")

        if not title:
            continue

        if not src_url:
            skipped_no_url += 1
            print(f"  [~] 无 downloadURL，跳过: {title} {version}")
            continue

        if item_type in (0, "0", "notice", "announcement"):
            skipped_announce += 1
            continue

        # 生成稳定 bundle-id（若上游未提供）
        if not bundle_id or "/" in bundle_id or " " in bundle_id:
            slug = f"{title}-{version}".replace(" ", "_").replace("/", "_")
            bundle_id = f"com.mirror.{slug}"

        apps.append({
            "title":      title,
            "bundle_id":  bundle_id,
            "version":    version,
            "source_url": src_url,
            "icon_url":   icon_url,
        })

    print(f"[*] Parsed {len(apps)} app(s) with download URLs "
          f"(skipped: {skipped_no_url} no-url, {skipped_announce} announce).")
    return apps


# ---------------------------------------------------------------------------
# URL rewrite
# ---------------------------------------------------------------------------
def rewrite_url(app: dict) -> str:
    if REWRITE_MODE == "passthrough":
        return app["source_url"]

    if REWRITE_MODE == "proxy":
        if not PROXY_BASE_URL:
            raise RuntimeError("REWRITE_MODE=proxy 但 PROXY_BASE_URL 未设置")
        filename = f"{app['bundle_id']}.ipa"
        return f"{PROXY_BASE_URL}/ipa/{urllib.parse.quote(filename, safe='')}"

    raise ValueError(f"Unknown REWRITE_MODE: {REWRITE_MODE}")


# ---------------------------------------------------------------------------
# Link health check
# ---------------------------------------------------------------------------
def url_alive(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT_HEAD) as r:
            return 200 <= r.status < 400
    except Exception as e:
        print(f"    [!] HEAD failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Emit downstream artifacts
# ---------------------------------------------------------------------------
def build_manifest(apps: list[dict]) -> bytes:
    items = []
    for a in apps:
        assets = [{"kind": "software-package", "url": a["public_url"]}]
        if a.get("icon_url"):
            assets.append({"kind": "display-image", "url": a["icon_url"]})
            assets.append({"kind": "full-size-image", "url": a["icon_url"]})

        items.append({
            "assets": assets,
            "metadata": {
                "bundle-identifier": a["bundle_id"],
                "bundle-version":    a["version"] or "1.0",
                "kind":              "software",
                "title":             a["title"],
            },
        })

    return plistlib.dumps({"items": items}, fmt=plistlib.FMT_XML)


def build_index(apps: list[dict]) -> bytes:
    manifest_url = f"{PAGES_BASE_URL}/manifest.plist"
    install_url  = ("itms-services://?action=download-manifest&url="
                    + urllib.parse.quote(manifest_url, safe=""))
    return json.dumps({
        "generated_by": "ios-manifest-sync",
        "upstream":     UPSTREAM_MANIFEST,
        "manifest_url": manifest_url,
        "install_url":  install_url,
        "rewrite_mode": REWRITE_MODE,
        "count":        len(apps),
        "apps": [{
            "title":     a["title"],
            "bundle_id": a["bundle_id"],
            "version":   a["version"],
            "ipa_url":   a["public_url"],
            "icon_url":  a.get("icon_url", ""),
        } for a in apps],
    }, ensure_ascii=False, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    try:
        raw = fetch_upstream()
    except Exception as e:
        print(f"[FATAL] Fetch upstream failed: {e}")
        return 1

    apps = parse_upstream(raw)
    if not apps:
        print("[WARN] 没有解析到任何带下载链接的应用。")
        print("       可能原因：上游 downloadURL 字段为空 / 需要解锁 / 字段名变更。")
        # 仍然写一份空的 apps.json，方便调试
        INDEX_LOCAL.write_bytes(build_index([]))
        return 0

    # 重写 URL
    for a in apps:
        a["public_url"] = rewrite_url(a)

    # 可选存活校验
    if VERIFY_LINKS:
        print("[*] Verifying source URLs ...")
        verified = []
        for a in apps:
            ok = url_alive(a["source_url"])
            print(f"  [{'OK' if ok else 'X'}] {a['title']} {a['version']}")
            if ok:
                verified.append(a)
        apps = verified
        print(f"[*] {len(apps)} app(s) passed health check.")

    MANIFEST_LOCAL.write_bytes(build_manifest(apps))
    INDEX_LOCAL.write_bytes(build_index(apps))
    print(f"[OK] Wrote {MANIFEST_LOCAL} and {INDEX_LOCAL} ({len(apps)} apps).")
    return 0


if __name__ == "__main__":
    sys.exit(main())