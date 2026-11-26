# recall's online MCP server as a container (built from the repository root as context).
# Pinned by digest (the image deployed since 2026-11-14), so two builds of one commit match.
# To update: podman pull python:3.12-slim, then copy its digest from `podman image ls --digests`.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Dependencies first, at the versions in uv.lock, so code changes rebuild only the last layers.
COPY pyproject.toml uv.lock README.md ./
RUN pip install uv==0.13.9 \
 && uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && pip uninstall -y uv && rm /tmp/requirements.txt

COPY src ./src
RUN pip install --no-deps .

# Listens on every interface inside the container; only the Worker in front can reach it.
ENV RECALL_ONLINE_HOST=0.0.0.0 RECALL_ONLINE_PORT=8791
# "yesterday" in a search, and the dates shown, are the user's days, not UTC's.
ENV TZ=America/Los_Angeles RECALL_TZ=America/Los_Angeles
EXPOSE 8791
USER 65534:65534
CMD ["recall", "mcp-online"]
