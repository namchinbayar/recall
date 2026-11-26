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
# Fail now, before the build, if the settings cannot say where to deploy.
python3 deploy_config.py --check
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

# The deploy config: wrangler.jsonc with the pushed image, RELEASE and the public name (the host
# of RECALL_ONLINE_URL in settings.local.json) filled in. Not tracked.
python3 deploy_config.py "$account" "$(cat "$work/digest")" "$release"

wrangler deploy -c wrangler.deploy.jsonc --secrets-file settings.local.json \
  --tag "$commit" --message "$(git log -1 --format=%s | cut -c1-100)"
