"""ProxyEmpire connectivity probe — independent of Naver.

Usage:
    PROXYEMPIRE_USERNAME=... PROXYEMPIRE_PASSWORD=... \
        uv run python scripts/probe_proxy.py [examples/prod.yaml]

Loads the proxy config from a manifest, mints three sticky sessions, and
hits ``ipinfo.io/json`` through each — confirming three things before any
Naver flow runs:

1. **Auth** — Playwright doesn't immediately error on the proxy CONNECT.
2. **Geo** — every IP comes back as ``country=KR``.
3. **Rotation** — three SIDs yield three different upstream IPs.

Failure modes are reported inline so a misconfigured account doesn't bleed
into a long Naver run.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from ranker.manifest import load_manifest
from ranker.proxy import build_proxy_arg, new_session_id, require_credentials


_IP_CHECK_URL = "https://ipinfo.io/json"
_PROBE_COUNT = 3


async def _check_one(browser, proxy_arg) -> dict:
    """Open a one-shot BrowserContext through the given proxy and return
    whatever ipinfo.io reports. Errors are captured rather than raised so
    the caller can still summarize the other probes."""
    context = await browser.new_context(proxy=proxy_arg.to_playwright())
    try:
        page = await context.new_page()
        try:
            response = await page.goto(_IP_CHECK_URL, timeout=20_000)
            if response is None:
                return {"error": "no response"}
            text = await response.text()
            data = json.loads(text)
            return data
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
    finally:
        await context.close()


async def probe(manifest_path: Path) -> int:
    manifest = load_manifest(manifest_path)
    if manifest.proxy is None:
        print(
            f"ERROR: {manifest_path} has no 'proxy:' block — nothing to probe.",
            file=sys.stderr,
        )
        return 2

    proxy = manifest.proxy
    account_stem, password = require_credentials(proxy.provider)

    print(f"→ Provider: {proxy.provider.value}")
    print(f"→ Endpoint: {proxy.host}:{proxy.port}")
    print(f"→ Country : {proxy.country}, sticky TTL {proxy.session_ttl_minutes}m")
    print(f"→ Probing {_PROBE_COUNT} sessions against {_IP_CHECK_URL}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            results = []
            for i in range(_PROBE_COUNT):
                sid = new_session_id()
                arg = build_proxy_arg(proxy, account_stem, password, sid)
                print(f"  [{i + 1}/{_PROBE_COUNT}] sid={sid} ...", end=" ", flush=True)
                info = await _check_one(browser, arg)
                results.append((sid, info))
                if "error" in info:
                    print(f"ERROR — {info['error']}")
                else:
                    ip = info.get("ip", "?")
                    country = info.get("country", "?")
                    region = info.get("region", "?")
                    city = info.get("city", "?")
                    org = info.get("org", "?")
                    print(f"ip={ip}  {country}/{region}/{city}  {org}")
        finally:
            await browser.close()

    # Summary checks.
    print("\n=== Summary ===")
    errors = [(sid, info) for sid, info in results if "error" in info]
    successes = [(sid, info) for sid, info in results if "error" not in info]

    if errors:
        print(f"  ✗ {len(errors)} probe(s) failed:")
        for sid, info in errors:
            print(f"      sid={sid}: {info['error']}")

    if not successes:
        print("  ✗ No successful probes — check credentials and endpoint.")
        return 1

    countries = {info.get("country") for _, info in successes}
    expected = proxy.country.upper()
    if countries == {expected}:
        print(f"  ✓ All IPs reported country={expected}")
    else:
        print(f"  ✗ Expected country={expected}, got {countries}")

    ips = [info.get("ip") for _, info in successes]
    unique_ips = set(ips)
    if len(unique_ips) == len(ips):
        print(f"  ✓ All {len(ips)} sessions got distinct IPs (rotation working)")
    elif len(unique_ips) == 1:
        print(f"  ✗ All sessions got the SAME IP {ips[0]} — rotation not working")
        print("    (could be NAT-shared mobile IP; or sticky binding wrong)")
    else:
        print(f"  ⚠ {len(ips)} sessions yielded {len(unique_ips)} unique IPs")

    return 0 if successes and not errors else 1


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print("usage: probe_proxy.py [manifest.yaml]", file=sys.stderr)
        print("       (default: examples/prod.yaml)", file=sys.stderr)
        return 2
    manifest_path = Path(args[0]) if args else Path("examples/prod.yaml")
    if not manifest_path.exists():
        print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    # Env-var check is delegated to require_credentials() inside probe(),
    # which only fires when the manifest actually carries a proxy block.
    return asyncio.run(probe(manifest_path))


if __name__ == "__main__":
    raise SystemExit(main())
