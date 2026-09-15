#!/usr/bin/env python3
"""iOS Manifest Sync — Link-only mirror for GitHub Pages."""

import json, os, plistlib, sys, urllib.parse, urllib.request
from pathlib import Path

UPSTREAM_MANIFEST = os.environ.get("UPSTREAM_MANIFEST", "")
PAGES_BASE_URL   = os.environ.get("PAGES_BASE_URL", "").rstrip("/")
REWRITE_MODE     = os.environ.get("REWRITE_MODE", "proxy").lower()
PROXY_BASE_URL   = os.environ.get("PROXY_BASE_URL", "").rstrip("/")
VERIFY_LINKS     = os.environ.get("VERIFY_LINKS", "true").lower() == "true"
MANIFEST_LOCAL   = Path("manifest.plist")
INDEX_LOCAL      = Path("apps.json")
UA               = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) ipa-manifest-sync/1.0"
TIMEOUT_HEAD     = 20


def fetch_upstream() -> bytes:
    req = urllib.request.Request(UPSTREAM_MANIFEST, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse_manifest(raw: bytes) -> list[dict]:
    plist = plistlib.loads(raw)
    apps = []
    for item in plist.get("items", []):
        meta = item.get("metadata", {})
        bundle_id = meta.get("bundle-identifier", "").strip()
        title     = meta.get("title", "Unknown").strip()
        version   = meta.get("bundle-version", "").strip()
        src_url   = next(
            (a["url"].strip() for a in item.get("assets", [])
             if a.get("kind") == "software-package" and a.get("url")),
            "",
        )
        if bundle_id and src_url:
            apps.append({"title": title, "bundle_id": bundle_id,
                         "version": version, "source_url": src_url})
        else:
            print(f"  [~] Skip: {title}")
    print(f"[*] Parsed {len(apps)} app(s).")
    return apps


def rewrite_url(app: dict) -> str:
    if REWRITE_MODE == "passthrough":
        return app["source_url"]
    if not PROXY_BASE_URL:
        raise RuntimeError("REWRITE_MODE=proxy 但 PROXY_BASE_URL 未设置")
    return f"{PROXY_BASE_URL}/ipa/{urllib.parse.quote(app['bundle_id'] + '.ipa')}"


def url_alive(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT_HEAD) as r:
            return 200 <= r.status < 400
    except Exception as e:
        print(f"    [!] HEAD failed: {e}")
        return False


def build_manifest(apps: list[dict]) -> bytes:
    items = [
        {
            "assets": [{"kind": "software-package", "url": a["public_url"]}],
            "metadata": {
                "bundle-identifier": a["bundle_id"],
                "bundle-version": a["version"],
                "kind": "software",
                "title": a["title"],
            },
        }
        for a in apps
    ]
    return plistlib.dumps({"items": items}, fmt=plistlib.FMT_XML)


def build_index(apps: list[dict]) -> bytes:
    install_url = ("itms-services://?action=download-manifest&url="
                   + urllib.parse.quote(f"{PAGES_BASE_URL}/manifest.plist"))
    return json.dumps({
        "generated_by": "ios-manifest-sync",
        "manifest_url": f"{PAGES_BASE_URL}/manifest.plist",
        "install_url": install_url,
        "rewrite_mode": REWRITE_MODE,
        "apps": [{
            "title": a["title"], "bundle_id": a["bundle_id"],
            "version": a["version"], "ipa_url": a["public_url"],
        } for a in apps],
    }, ensure_ascii=False, indent=2).encode("utf-8")


def main() -> int:
    try:
        raw = fetch_upstream()
    except Exception as e:
        print(f"[FATAL] Fetch upstream failed: {e}"); return 1

    apps = parse_manifest(raw)
    if not apps:
        print("[WARN] No apps parsed."); return 0

    for app in apps:
        app["public_url"] = rewrite_url(app)

    if VERIFY_LINKS:
        print("[*] Verifying source URLs ...")
        apps = [a for a in apps
                if (print(f"  [{'OK' if (ok := url_alive(a['source_url'])) else 'X'}] "
                          f"{a['title']}") or ok)]
        print(f"[*] {len(apps)} app(s) passed.")

    MANIFEST_LOCAL.write_bytes(build_manifest(apps))
    INDEX_LOCAL.write_bytes(build_index(apps))
    print(f"[OK] Wrote {MANIFEST_LOCAL} and {INDEX_LOCAL}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())