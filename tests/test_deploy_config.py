"""deploy/cloudflare/deploy_config.py: the Worker's public name comes from settings.local.json."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "deploy" / "cloudflare"


def run(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / "deploy_config.py"), *args], cwd=folder, capture_output=True, text=True)


def folder_with(tmp_path: Path, settings: str | None) -> Path:
    shutil.copy(HERE / "wrangler.jsonc", tmp_path / "wrangler.jsonc")
    if settings is not None:
        (tmp_path / "settings.local.json").write_text(settings)
    return tmp_path


def test_the_public_name_is_the_host_of_the_online_address(tmp_path):
    folder = folder_with(tmp_path, json.dumps({"RECALL_ONLINE_URL": "https://recall.name.example/mcp", "VOYAGE_MODEL": "voyage-4"}))
    done = run(folder, "0123abcd", "sha256:feed", "abc123def456")
    assert done.returncode == 0, done.stderr
    cfg = json.loads((folder / "wrangler.deploy.jsonc").read_text())
    assert cfg["routes"] == [{"pattern": "recall.name.example", "custom_domain": True}]
    assert cfg["vars"]["RELEASE"] == "abc123def456"
    assert cfg["containers"][0]["images"]["base"] == {"image": "registry.cloudflare.com/0123abcd/recall-online@sha256:feed"}
    assert "$schema" not in cfg and cfg["name"] == "recall-online"
    assert "example.com" not in json.dumps(cfg)          # the placeholder never reaches a deploy


def test_settings_may_be_key_value_lines(tmp_path):
    folder = folder_with(tmp_path, '# settings\nVOYAGE_MODEL=voyage-4\nRECALL_ONLINE_URL="https://recall.name.example/mcp"\n')
    done = run(folder, "--check")
    assert (done.returncode, done.stdout.strip()) == (0, "public name: recall.name.example")
    assert not (folder / "wrangler.deploy.jsonc").exists()   # a check writes nothing


def test_a_deploy_without_an_address_stops_with_what_is_missing(tmp_path):
    for settings, words in ((None, "settings.local.json is missing"), ("{}", "needs RECALL_ONLINE_URL"),
                            (json.dumps({"RECALL_ONLINE_URL": "http://recall.name.example/mcp"}), "needs RECALL_ONLINE_URL")):
        target = tmp_path / f"case-{len(list(tmp_path.iterdir()))}"
        target.mkdir()
        done = run(folder_with(target, settings), "0123abcd", "sha256:feed", "abc123def456")
        assert done.returncode != 0 and words in done.stderr
        assert not (target / "wrangler.deploy.jsonc").exists()
