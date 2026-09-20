"""Every EB_* variable the Helm chart emits must be a real setting (a typo would be silently ignored)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from eye_blink.config import Settings

CHART = Path(__file__).resolve().parent.parent / "deploy" / "helm" / "eye-blink"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render(*extra: str) -> list[dict[str, object]]:
    digest = "0" * 64
    out = subprocess.run(
        ["helm", "template", "eb", str(CHART), "--set", f"auth.apiKeyHashes={digest}", *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


@pytest.mark.skipif(not (CHART / "charts").exists(), reason="run `helm dependency build` first")
def test_chart_only_emits_known_settings() -> None:
    known = {f"EB_{name.upper()}" for name in Settings.model_fields}
    emitted: set[str] = set()
    for doc in render():
        if doc["kind"] == "ConfigMap" and "EB_LOG_LEVEL" in doc.get("data", {}):  # type: ignore[attr-defined]
            emitted |= set(doc["data"])  # type: ignore[call-overload]
        if doc["kind"] == "Deployment":
            for c in doc["spec"]["template"]["spec"]["containers"]:  # type: ignore[index]
                emitted |= {e["name"] for e in c.get("env", []) if e["name"].startswith("EB_")}
    assert emitted, "chart emitted no EB_* variables"
    assert emitted <= known, f"unknown settings in chart: {sorted(emitted - known)}"


@pytest.mark.skipif(not (CHART / "charts").exists(), reason="run `helm dependency build` first")
def test_chart_defaults_are_valid_settings() -> None:
    """The chart's default config values must be accepted by the application's own validation."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())["config"]
    env = {}
    for doc in render():
        if doc["kind"] == "ConfigMap" and "EB_LOG_LEVEL" in doc.get("data", {}):  # type: ignore[attr-defined]
            env = dict(doc["data"])  # type: ignore[call-overload]
    assert env and values
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        **{k.removeprefix("EB_").lower(): v for k, v in env.items() if v != ""},
        environment="test",
    )
    assert settings.stream_max_sessions == values["streamMaxSessions"]
