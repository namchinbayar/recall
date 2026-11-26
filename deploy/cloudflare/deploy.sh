#!/bin/sh
# Build recall's image with podman, push it to Cloudflare's registry and deploy the Worker.
#
# wrangler can build and push by itself only with Docker: with podman the pushed image gets a
# different digest than the local one wrangler asks Cloudflare for. So this pushes the image
# itself and points the deploy at the digest the registry reports.
#
# Only a clean checkout of main is deployed, so what runs at recall.example.com is always a
# commit on main: one branch's deploy can no longer replace another's work. RELEASE is set
# to that commit, so the container restarts with the new image whenever the commit changes.
# RECALL_DEPLOY_ANY_BRANCH=1 skips the branch check (not the clean-tree one) in an emergency.
#
# Needs: podman, wrangler (signed in), settings.local.json next to this file (see README.md).
set -eu
cd "$(dirname "$0")"

branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != main ] && [ "${RECALL_DEPLOY_ANY_BRANCH:-}" != 1 ]; then
  echo "deploy from main, not $branch: merge first (RECALL_DEPLOY_ANY_BRANCH=1 overrides)" >&2; exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "uncommitted changes; commit them first so the deploy is a known commit" >&2; exit 1
fi
commit=$(git rev-parse --short=12 HEAD)
# After changing only a secret, RECALL_RESTART=1 restarts the container on the same commit.
release=$commit
[ "${RECALL_RESTART:-}" = 1 ] && release="$commit-$(date -u +%Y%m%d%H%M%S)"

account=${CLOUDFLARE_ACCOUNT_ID:-$(wrangler whoami 2>/dev/null | grep -o -E '\b[0-9a-f]{32}\b' | head -1)}
[ -n "$account" ] || { echo "could not find the Cloudflare account id; set CLOUDFLARE_ACCOUNT_ID" >&2; exit 1; }
image="registry.cloudflare.com/$account/recall-online:$commit"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

podman build --platform linux/amd64 -f Dockerfile -t "$image" ../..
wrangler containers registries credentials registry.cloudflare.com --push 2>/dev/null | tail -1 \
  | podman login --username v1 --password-stdin registry.cloudflare.com
podman push --format v2s2 --digestfile "$work/digest" "$image"

# The deploy config: wrangler.jsonc with the image replaced by the pushed one (not tracked).
python3 - "$account" "$(cat "$work/digest")" "$release" <<'PY'
import json, re, sys
account, digest, release = sys.argv[1:4]
cfg = json.loads(re.sub(r"^\s*//.*$", "", open("wrangler.jsonc").read(), flags=re.M))
cfg.pop("$schema", None)
cfg["vars"]["RELEASE"] = release
cfg["containers"][0]["images"]["base"] = {"image": f"registry.cloudflare.com/{account}/recall-online@{digest}"}
json.dump(cfg, open("wrangler.deploy.jsonc", "w"), indent="\t")
PY

wrangler deploy -c wrangler.deploy.jsonc --secrets-file settings.local.json \
  --tag "$commit" --message "$(git log -1 --format=%s | cut -c1-100)"
