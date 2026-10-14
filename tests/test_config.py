"""Settings loader: fail fast on missing secrets, missing file, unknown keys, weak secrets."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from kbc_access.config import Settings, load_settings

# Secrets the local config (config/local.toml, with a [gateway]) requires.
SECRET_ENV = ("GATEWAY_SESSION_SECRET", "MASK_SALT")

ROOT = Path(__file__).resolve().parent.parent
LOCAL_TOML = ROOT / "config" / "local.toml"
GOOD_SECRETS = {
    "GATEWAY_SESSION_SECRET": "test-session-secret-0123456789",
    "MASK_SALT": "test-mask-salt-0123456789abcdef",
}


def _copy_config(tmp_path: Path, extra: str = "") -> Path:
    """Copy local.toml into <tmp>/config/ (root resolves to <tmp>) and append `extra`."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "local.toml"
    shutil.copy(LOCAL_TOML, target)
    if extra:
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n" + extra + "\n")
    return target


def test_loads_the_shipped_config() -> None:
    settings = load_settings(LOCAL_TOML, GOOD_SECRETS)
    assert isinstance(settings, Settings)
    assert settings.root == ROOT
    assert settings.secrets.mask_salt == GOOD_SECRETS["MASK_SALT"]
    assert set(settings.providers) == {"entra", "okta"}
    assert settings.app_url("sales") == f"http://{settings.gateway.host}:{settings.apps['sales'].public_port}/"


@pytest.mark.parametrize("missing", SECRET_ENV)
def test_missing_secret_names_it(missing: str) -> None:
    env = {k: v for k, v in GOOD_SECRETS.items() if k != missing}
    with pytest.raises(ValueError) as info:
        load_settings(LOCAL_TOML, env)
    assert missing in str(info.value)


def test_all_secrets_missing_names_both() -> None:
    with pytest.raises(ValueError) as info:
        load_settings(LOCAL_TOML, {})
    for name in SECRET_ENV:
        assert name in str(info.value)


def test_empty_secret_counts_as_missing() -> None:
    with pytest.raises(ValueError, match="MASK_SALT"):
        load_settings(LOCAL_TOML, {**GOOD_SECRETS, "MASK_SALT": ""})


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="nope.toml"):
        load_settings(tmp_path / "nope.toml", GOOD_SECRETS)


def test_unknown_top_level_section_rejected(tmp_path: Path) -> None:
    path = _copy_config(tmp_path, "[surprise]\nkey = 1")
    with pytest.raises(ValidationError, match="surprise"):
        load_settings(path, GOOD_SECRETS)


def test_unknown_nested_key_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    text = LOCAL_TOML.read_text(encoding="utf-8").replace(
        "cache_ttl_seconds = 5", "cache_ttl_seconds = 5\ncache_ttl_typo = 7"
    )
    assert "cache_ttl_typo" in text
    (config_dir / "local.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError, match="cache_ttl_typo"):
        load_settings(config_dir / "local.toml", GOOD_SECRETS)


def test_missing_required_key_rejected(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    text = LOCAL_TOML.read_text(encoding="utf-8").replace("max_result_rows = 500\n", "")
    (config_dir / "local.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError, match="max_result_rows"):
        load_settings(config_dir / "local.toml", GOOD_SECRETS)


@pytest.mark.parametrize("name", SECRET_ENV)
def test_short_secret_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        load_settings(LOCAL_TOML, {**GOOD_SECRETS, name: "short"})


def test_settings_are_frozen() -> None:
    settings = load_settings(LOCAL_TOML, GOOD_SECRETS)
    with pytest.raises(ValidationError):
        settings.gate.cache_ttl_seconds = 99  # type: ignore[misc]


def test_relative_paths_resolve_against_root(tmp_path: Path) -> None:
    settings = load_settings(_copy_config(tmp_path), GOOD_SECRETS)
    assert settings.root == tmp_path
    assert settings.path(Path("data/x.db")) == tmp_path / "data" / "x.db"
    assert settings.path(Path("/abs/x.db")) == Path("/abs/x.db")
