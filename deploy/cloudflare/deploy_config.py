#!/usr/bin/env python3
"""Write wrangler.deploy.jsonc, the config one deploy uses (not tracked).

It is wrangler.jsonc with three things filled in: the image deploy.sh just pushed, RELEASE,
and the public name. The public name is not in this repository: it is the host of
RECALL_ONLINE_URL in settings.local.json, the same address clients connect to, so there is
one place to set it and wrangler.jsonc carries only a placeholder.

    deploy_config.py ACCOUNT_ID IMAGE_DIGEST RELEASE      (run from this folder)
    deploy_config.py --check                              (only say where a deploy would go)
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

SETTINGS = "settings.local.json"


def settings(text: str) -> dict:
    """The settings file, in either form wrangler takes: a JSON object or KEY=VALUE lines."""
    try:
        found = json.loads(text)
    except ValueError:
        found = {}
        for line in text.splitlines():
            name, eq, value = line.strip().partition("=")
            if eq and name and not name.startswith("#"):
                found[name.strip()] = value.strip().strip("\"'")
    if not isinstance(found, dict):
        raise SystemExit(f"{SETTINGS} must hold a JSON object or KEY=VALUE lines")
    return found


def public_host(found: dict) -> str:
    url = str(found.get("RECALL_ONLINE_URL") or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SystemExit(f"{SETTINGS} needs RECALL_ONLINE_URL (https://your.name/mcp); its host becomes the Worker's public name")
    return parts.hostname


def build(base: str, found: dict, account: str, digest: str, release: str) -> dict:
    cfg = json.loads(re.sub(r"^\s*//.*$", "", base, flags=re.M))
    cfg.pop("$schema", None)
    cfg["vars"]["RELEASE"] = release
    cfg["routes"] = [{"pattern": public_host(found), "custom_domain": True}]
    cfg["containers"][0]["images"]["base"] = {"image": f"registry.cloudflare.com/{account}/recall-online@{digest}"}
    return cfg


def main() -> None:
    try:
        found = settings(Path(SETTINGS).read_text())
    except FileNotFoundError:
        raise SystemExit(f"{SETTINGS} is missing (see README.md here)") from None
    if sys.argv[1:] == ["--check"]:         # before anything is built: can the settings say where to deploy?
        print(f"public name: {public_host(found)}")
        return
    if len(sys.argv) != 4:
        raise SystemExit("usage: deploy_config.py ACCOUNT_ID IMAGE_DIGEST RELEASE | deploy_config.py --check")
    account, digest, release = sys.argv[1:4]
    cfg = build(Path("wrangler.jsonc").read_text(), found, account, digest, release)
    Path("wrangler.deploy.jsonc").write_text(json.dumps(cfg, indent="\t"))
    print(f"deploying to {cfg['routes'][0]['pattern']}")


if __name__ == "__main__":
    main()
