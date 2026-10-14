"""Frontend build wiring: bundle setup.sh, what bundles and the skill ship, and scripts/dev.py.

The admin app's React SPA is built inside the Keboola container by setup.sh, so a
bundle must ship the npm sources and lockfile but never node_modules/ or dist/.
Bundle tests run against a temporary copy of the repo (bundle.ROOT monkeypatched),
so no junk is ever created in the working tree.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STACK = "https://connection.europe-west3.gcp.keboola.com"
URLS = {"admin": "https://access-admin-11.hub.europe-west3.gcp.keboola.com/",
        "launcher": "https://app-launcher-12.hub.europe-west3.gcp.keboola.com/"}
FRONTEND = "apps/admin/frontend"
# setup.sh of an app without a frontend — exactly what every app shipped before the SPA.
PLAIN_SETUP = "#!/bin/bash\nset -Eeuo pipefail\ncd /app && uv sync\nmkdir -p /app/logs /app/data\n"
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", "dist")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle():
    return _load("build_app_bundle_frontend", ROOT / "scripts" / "build_app_bundle.py")


@pytest.fixture(scope="module")
def dev():
    return _load("dev_frontend", ROOT / "scripts" / "dev.py")


@pytest.fixture
def fake_root(bundle, tmp_path, monkeypatch) -> Path:
    """A copy of what the admin and launcher bundles need, used as the builder's repo root."""
    root = tmp_path / "repo"
    for rel in ["src/kbc_access", "apps/admin", "apps/launcher", "config/policies"]:
        shutil.copytree(ROOT / rel, root / rel, ignore=COPY_IGNORE)
    shutil.copy2(ROOT / "apps" / "__init__.py", root / "apps" / "__init__.py")
    monkeypatch.setattr(bundle, "ROOT", root)
    return root


def _build(bundle, out: Path, app_id: str = "admin") -> Path:
    return bundle.build(app_id=app_id, out=out, stack_url=STACK, bucket_id="in.c-x", app_urls=URLS, roles_separator=",")


def test_admin_setup_checks_node_then_builds_the_frontend(bundle, fake_root, tmp_path):
    setup = (_build(bundle, tmp_path / "b") / "keboola-config" / "setup.sh").read_text()
    minimum = bundle.load_manifest()["node_min_version"]
    assert setup.startswith(PLAIN_SETUP)  # set -Eeuo pipefail, uv sync and dirs come first, unchanged
    assert f'required_node="{minimum}"' in setup
    assert "node --version" in setup and "too old" in setup
    cd = setup.index(f"cd /app/{FRONTEND}\n")
    ci = setup.index("npm ci --include=dev --no-audit --no-fund\n")
    build = setup.index("npm run build\n")
    assert setup.index("node --version") < cd < ci < build
    assert subprocess.run(["bash", "-n", "-c", setup], check=False).returncode == 0


def test_admin_bundle_ships_the_policy_files_for_its_data_catalogue(bundle, fake_root, tmp_path):
    out = _build(bundle, tmp_path / "b")
    deployed = (out / "config" / "deployed.toml").read_text()
    assert '[policy]\nfiles = ["config/policies/pharma.yaml"]' in deployed
    assert "[data]" not in deployed  # the admin has no warehouse
    assert (out / "config" / "policies" / "pharma.yaml").is_file()


def test_launcher_bundle_has_no_policy_section(bundle, fake_root, tmp_path):
    out = _build(bundle, tmp_path / "b", "launcher")
    assert "[policy]" not in (out / "config" / "deployed.toml").read_text()


def test_launcher_setup_is_unchanged(bundle, fake_root, tmp_path):
    setup = (_build(bundle, tmp_path / "b", "launcher") / "keboola-config" / "setup.sh").read_text()
    assert setup == PLAIN_SETUP


def test_bundle_ships_frontend_sources_but_not_node_modules_or_dist(bundle, fake_root, tmp_path):
    frontend = fake_root / FRONTEND
    (frontend / "node_modules" / "left-pad").mkdir(parents=True)
    (frontend / "node_modules" / "left-pad" / "index.js").write_text("x")
    (frontend / "dist" / "assets").mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("<html></html>")
    out = _build(bundle, tmp_path / "b") / FRONTEND
    assert (out / "package.json").is_file() and (out / "package-lock.json").is_file()
    assert not (out / "node_modules").exists()
    assert not (out / "dist").exists()


def test_bundle_refuses_a_frontend_without_a_lockfile(bundle, fake_root, tmp_path):
    (fake_root / FRONTEND / "package-lock.json").unlink()
    with pytest.raises(ValueError, match="package-lock.json"):
        _build(bundle, tmp_path / "b")
    assert not (tmp_path / "b").exists()  # nothing half-built


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda m: m.pop("node_min_version"), "node_min_version is required"),
        (lambda m: m.update(node_min_version="22"), "node_min_version must look like"),
        (lambda m: m["apps"]["admin"].update(frontend="apps/launcher"), "not inside any of its paths"),
        (lambda m: m["apps"]["admin"].update(frontend="../elsewhere"), "relative path inside the repo"),
    ],
)
def test_bundle_refuses_a_bad_frontend_manifest(bundle, fake_root, tmp_path, monkeypatch, change, message):
    manifest = bundle.load_manifest()
    change(manifest)
    monkeypatch.setattr(bundle, "load_manifest", lambda: manifest)
    with pytest.raises(ValueError, match=message):
        _build(bundle, tmp_path / "b")


@pytest.mark.parametrize(("version", "ok"), [("v18.20.0", False), ("v20.18.9", False), ("v20.19.2", True), ("v24.1.0", True)])
def test_setup_node_check_compares_against_the_minimum(bundle, tmp_path, version, ok):
    step = bundle._frontend_block(bundle.load_manifest(), FRONTEND)
    check = step[: step.index("cd /app/")]  # everything before the actual build
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in {"node": f"echo {version}", "npm": "exit 0"}.items():
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    result = subprocess.run(["bash", "-c", "set -Eeuo pipefail\n" + check], env={"PATH": f"{stubs}:/usr/bin:/bin"},
                            capture_output=True, text=True, check=False)
    assert (result.returncode == 0) is ok, result.stderr
    if not ok:
        assert "too old" in result.stderr


def test_copy_filters_skip_node_modules_and_dist(bundle):
    build_skill = _load("build_skill_frontend", ROOT / "scripts" / "build_skill.py")
    names = ["node_modules", "dist", "src", "package.json"]
    assert bundle.IGNORED("x", names) >= {"node_modules", "dist"}
    assert build_skill.IGNORED("x", names) >= {"node_modules", "dist"}
    assert not bundle.IGNORED("x", ["src", "package.json"])


# ---- scripts/dev.py ---------------------------------------------------------------------


class _Runner:
    """Records subprocess.run calls; `node --version` answers `node_version`."""

    def __init__(self, node_version: str = "v20.19.2", fail: str | None = None) -> None:
        self.node_version, self.fail, self.calls = node_version, fail, []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs.get("cwd")))
        if cmd[1:] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.node_version}\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1 if self.fail and self.fail in cmd else 0)


@pytest.fixture
def dev_env(dev, tmp_path, monkeypatch):
    frontend = tmp_path / FRONTEND
    frontend.mkdir(parents=True)
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev.shutil, "which", lambda name: f"/stub/{name}")
    return frontend


def test_dev_installs_then_builds_when_node_modules_is_missing(dev, dev_env, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(dev.subprocess, "run", runner)
    assert dev.build_frontends(["admin", "launcher", "local-only"]) is None
    npm_calls = [(cmd[1:], cwd) for cmd, cwd in runner.calls if cmd[0] == "/stub/npm"]
    assert npm_calls == [(["ci", "--no-audit", "--no-fund"], dev_env), (["run", "build"], dev_env)]


def test_dev_only_builds_when_node_modules_exists(dev, dev_env, monkeypatch):
    (dev_env / "node_modules").mkdir()
    runner = _Runner()
    monkeypatch.setattr(dev.subprocess, "run", runner)
    assert dev.build_frontends(["admin"]) is None
    assert [cmd[1:] for cmd, _ in runner.calls if cmd[0] == "/stub/npm"] == [["run", "build"]]


def test_dev_skips_npm_for_apps_without_a_frontend(dev, dev_env, monkeypatch):
    runner = _Runner()
    monkeypatch.setattr(dev.subprocess, "run", runner)
    monkeypatch.setattr(dev.shutil, "which", lambda name: None)
    assert dev.build_frontends(["launcher", "sales"]) is None
    assert runner.calls == []


def test_dev_fails_fast_without_npm(dev, dev_env, monkeypatch):
    monkeypatch.setattr(dev.shutil, "which", lambda name: None if name == "npm" else f"/stub/{name}")
    assert "npm must be on PATH" in dev.build_frontends(["admin"])


def test_dev_refuses_an_old_node(dev, dev_env, monkeypatch):
    monkeypatch.setattr(dev.subprocess, "run", _Runner(node_version="v20.18.9"))
    assert "too old" in dev.build_frontends(["admin"])


def test_dev_reports_a_failed_build(dev, dev_env, monkeypatch):
    (dev_env / "node_modules").mkdir()
    monkeypatch.setattr(dev.subprocess, "run", _Runner(fail="build"))
    assert "npm run build' failed" in dev.build_frontends(["admin"])


def test_admin_gets_a_data_section_once_the_demo_data_exists(bundle, fake_root, tmp_path):
    out = bundle.build(app_id="admin", out=tmp_path / "b", stack_url=STACK, bucket_id="in.c-x", app_urls=URLS,
                       roles_separator=",", data_bucket_id="in.c-demo")
    deployed = (out / "config" / "deployed.toml").read_text()
    assert "[data]" in deployed and '"in.c-demo.drug_sales"' in deployed and "[policy]" in deployed
    manifest = bundle.load_manifest()
    assert bundle.reads_data(manifest, "admin", "in.c-demo") and not bundle.reads_data(manifest, "admin", None)
    assert bundle.reads_data(manifest, "sales", None) and not bundle.reads_data(manifest, "launcher", "in.c-demo")
