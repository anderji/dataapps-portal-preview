"""The keboola-access-suite skill: bundle builder, skill packaging, installer logic.

kbagent is replaced by a recorder (tests may use doubles; the installer calls
the real CLI). What matters here is what the installer sends: the shape of the
OIDC block and git block, that secrets travel only in 0600 files, and that
re-runs reuse what exists.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from kbc_access.config import load_settings

ROOT = Path(__file__).resolve().parent.parent
STACK = "https://connection.europe-west3.gcp.keboola.com"
URLS = {"admin": "https://access-admin-11.hub.europe-west3.gcp.keboola.com/",
        "launcher": "https://app-launcher-12.hub.europe-west3.gcp.keboola.com/"}
ENTRA_GROUP = "3f2b8c1e-7d4a-4e5b-9a61-2c8d0f4e1b7a"
PROJECT = {"alias": "P", "project_name": "Proj", "project_id": 1234, "stack_url": STACK}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look their module up in sys.modules while the class is created.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_skill():
    return _load("build_skill", ROOT / "scripts" / "build_skill.py")


@pytest.fixture(scope="module")
def bundle():
    return _load("build_app_bundle_mod", ROOT / "scripts" / "build_app_bundle.py")


@pytest.fixture(scope="module")
def install(build_skill):
    build_skill.refresh_assets()
    return _load("kbc_install", ROOT / "skills" / "keboola-access-suite" / "scripts" / "install.py")


# ---- bundle builder ----------------------------------------------------------------


def test_bundle_has_keboola_layout_and_a_loadable_deployed_config(bundle, tmp_path):
    out = bundle.build(app_id="admin", out=tmp_path / "b", stack_url=STACK, bucket_id="in.c-apps-control-plane",
                       app_urls=URLS, roles_separator=",")
    for rel in ["keboola-config/nginx/sites/default.conf", "keboola-config/supervisord/services/app.conf",
                "keboola-config/setup.sh", "pyproject.toml", "config/deployed.toml", "apps/admin/main.py",
                "src/kbc_access/web.py"]:
        assert (out / rel).is_file(), rel
    assert (out / "keboola-config/setup.sh").stat().st_mode & stat.S_IXUSR
    assert "listen 8888" in (out / "keboola-config/nginx/sites/default.conf").read_text()
    assert "apps.admin.main:create_default" in (out / "keboola-config/supervisord/services/app.conf").read_text()
    assert not (out / "apps/gateway").exists() and not (out / "apps/sales").exists()
    settings = load_settings(out / "config" / "deployed.toml", {"MASK_SALT": "salt_0123456789abcdef", "ACCESS_STORAGE_TOKEN": "t" * 20})
    assert settings.store.backend == "keboola"
    assert settings.store.keboola.bucket_id == "in.c-apps-control-plane"
    assert settings.gateway is None and settings.secrets.gateway_session_secret is None
    assert settings.app_url("launcher") == URLS["launcher"]
    assert settings.ui.switch_user_path == "/_proxy/sign_out"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"app_id": "nope"}, "Unknown app"),
        ({"app_urls": {"admin": URLS["admin"]}}, "launcher"),
        ({"app_urls": {**URLS, "admin": "http://x/"}}, "https"),
        ({"app_urls": {**URLS, "ghost": "https://g/"}}, "unknown app"),
    ],
)
def test_bundle_refuses_bad_input(bundle, tmp_path, kwargs, message):
    args = {"app_id": "admin", "out": tmp_path / "b", "stack_url": STACK, "bucket_id": "in.c-x",
            "app_urls": URLS, "roles_separator": ","} | kwargs
    with pytest.raises(ValueError, match=message):
        bundle.build(**args)


def test_dashboard_bundle_reads_through_direct_storage_access(bundle, tmp_path):
    urls = {"sales": "https://drug-sales-5.hub.europe-west3.gcp.keboola.com/", "launcher": URLS["launcher"]}
    with pytest.raises(ValueError, match="data-bucket-id"):
        bundle.build(app_id="sales", out=tmp_path / "x", stack_url=STACK, bucket_id="in.c-cp", app_urls=urls, roles_separator=",")
    out = bundle.build(app_id="sales", out=tmp_path / "s", stack_url=STACK, bucket_id="in.c-cp", app_urls=urls,
                       roles_separator=",", data_bucket_id="in.c-demo")
    settings = load_settings(out / "config" / "deployed.toml", {"MASK_SALT": "salt_0123456789abcdef", "ACCESS_STORAGE_TOKEN": "t" * 20})
    assert settings.data.storage_access_tables == [f"in.c-demo.{t}" for t in ("companies", "stores", "drug_sales", "supply_chain")]
    assert settings.data.refresh_seconds and settings.policy.files
    assert "input_tables" not in (out / "keboola-config/setup.sh").read_text()  # no input mapping step
    assert "keboola-query-service" in (out / "pyproject.toml").read_text()
    assert (out / "apps/dashboard_common").is_dir() and (out / "config/policies/pharma.yaml").is_file()


def test_bundle_refuses_a_non_empty_output(bundle, tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        bundle.build(app_id="admin", out=tmp_path / "b", stack_url=STACK, bucket_id="in.c-x", app_urls=URLS, roles_separator=",")


# ---- skill packaging ---------------------------------------------------------------


def test_skill_assets_and_agnes_zip(build_skill, tmp_path):
    build_skill.refresh_assets()
    suite = ROOT / "skills" / "keboola-access-suite" / "assets" / "suite"
    assert (suite / "src" / "kbc_access" / "keboola_store.py").is_file()
    assert (suite / "scripts" / "build_app_bundle.py").is_file()
    assert not list(suite.rglob("__pycache__"))
    assert not list(suite.rglob("node_modules")) and not list(suite.rglob("dist"))  # local frontend build state
    assert (suite / "apps" / "admin" / "frontend" / "package-lock.json").is_file()
    target = tmp_path / "skill.zip"
    build_skill.write_zip(target)
    names = zipfile.ZipFile(target).namelist()
    assert "SKILL.md" in names and "scripts/install.py" in names and "references/idp-setup.md" in names
    assert not any(n.startswith(".claude-plugin") for n in names)


def test_skill_md_description_meets_agnes_rules():
    text = (ROOT / "skills" / "keboola-access-suite" / "SKILL.md").read_text()
    front = text.split("---")[1]
    description = next(line for line in front.splitlines() if line.startswith("description:"))
    value = description.removeprefix("description:").strip()
    assert len(value) >= 60 and len(set(value.split())) >= 5
    assert ": " not in value and not value.startswith(">")


# ---- installer -----------------------------------------------------------------------


class FakeKbagent:
    """Records kbagent calls and answers from a small in-memory project."""

    def __init__(self, apps: list[dict] | None = None, stored_secret: str = "KBC::ProjectSecure::abc") -> None:
        # stored_secret: what Storage holds for each #secret after secrets-set (encrypted or not).
        self.calls: list[list[str]] = []
        self.files: dict[str, str] = {}
        self.apps = apps or []
        self.config = {"parameters": {"dataApp": {"slug": "x"}}, "authorization": {"app_proxy": {"auth_providers": [{"id": "simpleAuth", "type": "password"}], "auth_rules": []}}}
        self.stored_secret = stored_secret
        self.created = 0

    def __call__(self, *args: str, secret_values: tuple[str, ...] = ()) -> dict:
        self.calls.append(list(args))
        for flag in ("--secrets-file", "--configuration-file"):
            if flag in args:
                path = Path(args[args.index(flag) + 1])
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, "secrets must go through 0600 files"
                self.files[flag] = path.read_text()
        head = tuple(args[:2])
        if head == ("project", "list"):
            return [{"alias": "P", "project_name": "Proj", "project_id": 1234, "stack_url": STACK}]
        if head == ("data-app", "list"):
            return {"apps": self.apps}
        if head == ("data-app", "create"):
            self.created += 1
            slug = args[args.index("--slug") + 1]
            return {"app_id": f"9{self.created}", "config_id": f"cfg{self.created}", "url": f"https://{slug}-9{self.created}.hub.example.keboola.com"}
        if head == ("config", "detail"):
            return {"configuration": json.loads(json.dumps(self.config))}
        if head == ("data-app", "git-credentials-create"):
            return {"credential": {"secret": "clone-secret"}}
        if head == ("data-app", "secrets-set"):
            secrets = json.loads(self.files["--secrets-file"])
            self.config["parameters"]["dataApp"]["secrets"] = {k: self.stored_secret for k in secrets}
            return {}
        if head == ("config", "update"):
            self.config = json.loads(self.files["--configuration-file"])
            git = self.config.get("parameters", {}).get("dataApp", {}).get("git", {})
            if "#password" in git:  # kbagent encrypts #keys on write
                git["#password"] = self.stored_secret
            return {}
        return {}


@pytest.fixture
def plan(install, tmp_path):
    return install.Plan(
        project="P", state_file=tmp_path / "state.json", bucket_stage="in", bucket_name="apps-control-plane",
        auth="oidc", admin_email="a@x.example",
        admin_group_provider="entra", admin_group_id=ENTRA_GROUP, admin_group_name="admins", slugs={"admin": "access-admin", "launcher": "app-launcher"},
        names={"admin": "Access admin", "launcher": "App launcher"}, size="tiny", auto_suspend_seconds=3600, roles_separator=",",
    )


def test_validate_accepts_a_good_plan(install, plan):
    install.validate(plan)
    assert plan.bucket_id == "in.c-apps-control-plane"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"bucket_name": "bad name"}, "Bucket name"),
        ({"admin_group_id": "not-a-guid"}, "not a valid"),
        ({"admin_group_name": None}, "go together"),
        ({"admin_email": "not-an-email"}, "not an email"),
        ({"auth": "saml"}, "--auth"),
        ({"slugs": {"admin": "Bad_Slug", "launcher": "app-launcher"}}, "Slug"),
    ],
)
def test_validate_refuses(install, plan, change, message):
    from dataclasses import replace

    with pytest.raises(install.InstallError, match=message):
        install.validate(replace(plan, **change))


def test_admin_group_is_optional(install, plan):
    from dataclasses import replace

    install.validate(replace(plan, admin_group_provider=None, admin_group_id=None, admin_group_name=None))


@pytest.mark.parametrize(("auth", "create_with", "identity"), [
    ("oidc", "password", True), ("github", "password", True), ("gitlab", "password", True),
    ("password", "password", False), ("public", "public", False),
])
def test_every_keboola_login_type_is_offered(install, auth, create_with, identity):
    # UI-configured providers start behind a closed password placeholder, never public.
    assert install.create_auth(auth) == create_with
    summary = install.login_summary(auth)
    assert ("lets nobody in" in summary) is (not identity)


def test_next_steps_point_to_the_keboola_ui_for_provider_logins(install, plan):
    apps = {a: {"url": URLS[a]} for a in ("admin", "launcher")}
    steps = " ".join(install.next_steps(plan, apps))
    assert "Authentication" in steps and f"{URLS['admin']}_proxy/callback" in steps
    from dataclasses import replace

    public = " ".join(install.next_steps(replace(plan, auth="public"), apps))
    assert "no user identity" in public and "_proxy/callback" not in public


def test_kbagent_wrapper_strips_update_noise_and_scrubs_secrets(install, monkeypatch):
    noisy = "Updating kbagent...\nAuto-update failed\n" + json.dumps({"status": "ok", "data": {"x": 1}})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, noisy, ""))
    assert install.kbagent("storage", "buckets") == {"x": 1}
    failing = json.dumps({"status": "error", "error": {"message": "bad token tok-123"}})
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, failing, ""))
    with pytest.raises(install.InstallError) as exc:
        install.kbagent("token", "create", secret_values=("tok-123",))
    assert "tok-123" not in str(exc.value)


def test_ensure_apps_creates_both_then_reuses_them(install, plan, monkeypatch):
    fake = FakeKbagent()
    monkeypatch.setattr(install, "kbagent", fake)
    state: dict = {}
    first = install.ensure_apps(plan, PROJECT, state)
    assert fake.created == 2 and first["admin"]["url"].endswith("/")
    create = next(c for c in fake.calls if c[:2] == ["data-app", "create"])
    assert "--use-managed-git-repo" in create
    assert create[create.index("--auth") + 1] == "password"  # oidc: closed placeholder until set in the UI
    fake.apps = [{"app_id": v["app_id"], "config_id": v["config_id"], "url": v["url"]} for v in first.values()]
    again = install.ensure_apps(plan, PROJECT, install.load_state(plan.state_file))
    assert fake.created == 2 and again == first


def test_ensure_apps_finds_existing_apps_by_slug_without_state(install, plan, monkeypatch):
    fake = FakeKbagent(apps=[
        {"app_id": "71", "config_id": "c71", "url": "https://access-admin-71.hub.x.keboola.com"},
        {"app_id": "72", "config_id": "c72", "url": "https://app-launcher-72.hub.x.keboola.com/"},
    ])
    monkeypatch.setattr(install, "kbagent", fake)
    apps = install.ensure_apps(plan, PROJECT, {})
    assert fake.created == 0 and apps["admin"]["app_id"] == "71" and apps["launcher"]["app_id"] == "72"


def test_configure_app_writes_git_and_secrets_and_leaves_auth_alone(install, plan, monkeypatch):
    fake = FakeKbagent()
    before_auth = json.loads(json.dumps(fake.config["authorization"]))
    monkeypatch.setattr(install, "kbagent", fake)
    app = {"app_id": "91", "config_id": "cfg1", "url": URLS["admin"]}
    install.configure_app(plan, "admin", app, "https://git.example/keboola/app-91.git", "storage-token-value", "salt-value")
    # Secrets went through a 0600 file, never through argv.
    assert json.loads(fake.files["--secrets-file"]) == {"#ACCESS_STORAGE_TOKEN": "storage-token-value", "#MASK_SALT": "salt-value"}
    assert not any("storage-token-value" in arg or "salt-value" in arg for call in fake.calls for arg in call)
    written = json.loads(fake.files["--configuration-file"])
    assert written["authorization"] == before_auth  # the user sets the login in the Keboola UI
    # Every app clones with its own read-only credential (the platform's one expired live).
    assert written["parameters"]["dataApp"]["git"] == {
        "repository": "https://git.example/keboola/app-91.git", "branch": "main", "private": True,
        "username": install.CLONE_USERNAME, "#password": "clone-secret",
    }
    assert written["parameters"]["dataApp"]["slug"] == "x"  # sibling keys preserved


def test_configure_app_aborts_when_secrets_are_not_encrypted(install, plan, monkeypatch):
    monkeypatch.setattr(install, "kbagent", FakeKbagent(stored_secret="storage-token-value"))
    with pytest.raises(install.InstallError, match="not stored encrypted"):
        install.configure_app(plan, "admin", {"app_id": "91", "config_id": "c", "url": URLS["admin"]},
                              "https://git.example/r.git", "storage-token-value", "salt")


def test_mint_token_rotates_and_remembers_the_old_one(install, plan, monkeypatch):
    tokens = iter([{"id": 1, "token": "tok-1"}, {"id": 2, "token": "tok-2"}])
    monkeypatch.setattr(install, "kbagent", lambda *a, **k: next(tokens))
    state: dict = {}
    assert install.mint_token(plan, state) == "tok-1" and state["token_id"] == "1"
    assert install.mint_token(plan, state) == "tok-2"
    assert state["token_id"] == "2" and state["revoke_token_ids"] == ["1"]
    # Persisted immediately: a run that dies before the end still revokes it next time.
    assert install.load_state(plan.state_file)["revoke_token_ids"] == ["1"]
    assert "tok-" not in plan.state_file.read_text()


def test_expected_url_matches_the_live_pattern(install):
    # Live: https://rls-demo-deck-10000003.hub.europe-west3.gcp.keboola.com (project 1235).
    assert install.expected_url(STACK, "rls-demo-deck", "10000003") == "https://rls-demo-deck-10000003.hub.europe-west3.gcp.keboola.com/"


def test_ensure_apps_derives_the_url_of_a_not_yet_deployed_app(install, plan, monkeypatch):
    fake = FakeKbagent()
    fake_create = fake.__call__

    def no_url(*args, **kwargs):
        result = fake_create(*args, **kwargs)
        if tuple(args[:2]) == ("data-app", "create"):
            result["url"] = None
        return result

    monkeypatch.setattr(install, "kbagent", no_url)
    apps = install.ensure_apps(plan, PROJECT, {})
    assert apps["admin"]["url"] == f"https://access-admin-{apps['admin']['app_id']}.hub.europe-west3.gcp.keboola.com/"


def test_ensure_apps_reuses_an_undeployed_app_by_name(install, plan, monkeypatch):
    fake = FakeKbagent(apps=[{"app_id": "10000001", "config_id": "c1", "name": "Access admin", "url": None}])
    monkeypatch.setattr(install, "kbagent", fake)
    apps = install.ensure_apps(plan, PROJECT, {})
    assert apps["admin"] == {"app_id": "10000001", "config_id": "c1",
                             "url": "https://access-admin-10000001.hub.europe-west3.gcp.keboola.com/"}
    assert fake.created == 1  # only the launcher is new


# ---- phase 2 -----------------------------------------------------------------------


@pytest.fixture
def plan2(install, tmp_path):
    return install.Phase2Plan(
        project="P", state_file=tmp_path / "state.json", bucket_id="in.c-apps-control-plane",
        demo_bucket_stage="in", demo_bucket_name="apps-control-plane-demo",
        grant_group_provider="entra", grant_group_id=ENTRA_GROUP, grant_group_name="g",
        slugs={"sales": "drug-sales", "supply": "supply-chain"},
        names={"sales": "Drug sales dashboard", "supply": "Supply chain dashboard"},
        size="tiny", auto_suspend_seconds=3600, roles_separator=",",
    )


PHASE1_STATE = {"apps": {"admin": {"app_id": "1", "config_id": "c-admin", "url": URLS["admin"]},
                         "launcher": {"app_id": "2", "config_id": "c-l", "url": URLS["launcher"]}}}


def test_phase2_requires_phase1(install, plan2):
    with pytest.raises(install.InstallError, match="phase1 first"):
        install.validate_phase2(plan2, {})
    install.validate_phase2(plan2, PHASE1_STATE)


@pytest.mark.parametrize(("change", "message"), [
    ({"demo_bucket_name": "apps-control-plane"}, "control-plane bucket"),
    ({"grant_group_name": None}, "go together"),
    ({"grant_group_id": "nope"}, "not a valid"),
])
def test_phase2_validation(install, plan2, change, message):
    from dataclasses import replace

    with pytest.raises(install.InstallError, match=message):
        install.validate_phase2(replace(plan2, **change), PHASE1_STATE)


def test_admin_authorization_is_copied_only_when_it_is_a_real_provider(install, plan2, monkeypatch):
    oidc = {"app_proxy": {"auth_providers": [{"id": "oidc-1", "type": "oidc", "#client_secret": "KBC::ProjectSecure::x"}],
                          "auth_rules": [{"type": "pathPrefix", "value": "/", "auth_required": True, "auth": ["oidc-1"]}]}}
    password = {"app_proxy": {"auth_providers": [{"id": "simpleAuth", "type": "password"}], "auth_rules": []}}
    for auth, expected in ((oidc, oidc), (password, None)):
        monkeypatch.setattr(install, "kbagent", lambda *a, _auth=auth, **k: {"configuration": {"authorization": _auth}})
        assert install.admin_authorization(plan2, PHASE1_STATE) == expected
    config_scoped = json.loads(json.dumps(oidc))
    config_scoped["app_proxy"]["auth_providers"][0]["#client_secret"] = "KBC::ConfigSecure::x"
    monkeypatch.setattr(install, "kbagent", lambda *a, **k: {"configuration": {"authorization": config_scoped}})
    with pytest.raises(install.InstallError, match="project-scoped"):
        install.admin_authorization(plan2, PHASE1_STATE)


def test_configure_app_writes_storage_access_grants_and_copied_login(install, plan2, monkeypatch):
    fake = FakeKbagent()
    monkeypatch.setattr(install, "kbagent", fake)
    auth = {"app_proxy": {"auth_providers": [{"id": "oidc-1", "type": "oidc"}], "auth_rules": []}}
    fake.config["storage"] = {"input": {"tables": [{"source": "old"}]}}  # a leftover input mapping
    fake.config["runtime"] = {"backend": {"size": "tiny"}}  # as on apps created before kbagent 0.87
    grants = [{"destination": "in.c-demo.drug_sales", "unload_strategy": "direct-grant"}]
    install.configure_app(plan2, "sales", {"app_id": "93", "config_id": "c3", "url": URLS["admin"]},
                          "https://git.example/r.git", "tok", "salt", storage_grants=grants, authorization=auth)
    written = json.loads(fake.files["--configuration-file"])
    assert written["storage"]["output"]["tables"] == grants
    assert "input" not in written["storage"]  # never input mapping
    assert written["runtime"]["workspace"] == {"enabled": True}  # the grants need a Storage workspace
    assert written["runtime"]["backend"] == {"size": "tiny"}  # existing runtime settings kept
    assert written["authorization"] == auth
    # With input mapping the clone gets its own read-only credential in the git block.
    git = written["parameters"]["dataApp"]["git"]
    assert git["username"] == install.CLONE_USERNAME and git["#password"] == "clone-secret"
    create = next(c for c in fake.calls if c[:2] == ["data-app", "git-credentials-create"])
    assert create[create.index("--permissions") + 1] == "readOnly"


def test_storage_access_grants_cover_every_demo_table(install):
    import tomllib

    manifest = tomllib.loads(install.MANIFEST.read_text())
    grants = install.storage_access_grants(manifest, "in.c-apps-control-plane-demo")
    assert [g["destination"] for g in grants] == [f"in.c-apps-control-plane-demo.{t}" for t in install.DEMO_TABLES]
    assert {g["unload_strategy"] for g in grants} == {"direct-grant"}


def test_control_plane_grants_cover_every_control_plane_table(install):
    from kbc_access.store import TABLE_COLUMNS

    grants = install.control_plane_grants("in.c-apps-control-plane")
    assert [g["destination"] for g in grants] == [f"in.c-apps-control-plane.{t}" for t in TABLE_COLUMNS]
    assert {g["unload_strategy"] for g in grants} == {"direct-grant"}


# ---- redeploy: push the current code, touch nothing else ----------------------------

REDEPLOY_STATE = {
    "bucket_id": "in.c-apps-control-plane",
    "demo_bucket_id": "in.c-apps-control-plane-demo",
    "apps": {
        **PHASE1_STATE["apps"],
        "sales": {"app_id": "3", "config_id": "c-s", "url": "https://drug-sales-3.hub.x.keboola.com/"},
    },
}


@pytest.fixture
def redeploy_plan(install, tmp_path):
    def make(apps: tuple[str, ...], demo: str | None = "in.c-apps-control-plane-demo"):
        return install.RedeployPlan(
            project="P", state_file=tmp_path / "state.json", bucket_id="in.c-apps-control-plane",
            demo_bucket_id=demo, app_ids=apps, roles_separator=",", names=install.APP_NAMES,
        )

    return make


def test_redeploy_validation(install, redeploy_plan):
    install.validate_redeploy(redeploy_plan(("admin", "sales")), REDEPLOY_STATE)
    with pytest.raises(install.InstallError, match="Unknown app"):
        install.validate_redeploy(redeploy_plan(("nope",)), REDEPLOY_STATE)
    with pytest.raises(install.InstallError, match="Not installed"):
        install.validate_redeploy(redeploy_plan(("supply",)), REDEPLOY_STATE)
    with pytest.raises(install.InstallError, match="demo bucket"):
        install.validate_redeploy(redeploy_plan(("sales",), demo=None), REDEPLOY_STATE)


def test_redeploy_pushes_and_deploys_only_the_chosen_apps(install, redeploy_plan, monkeypatch):
    install.save_state(redeploy_plan(("admin",)).state_file, REDEPLOY_STATE)
    pushed: list[tuple[str, str | None]] = []
    deployed: list[str] = []
    monkeypatch.setattr(install, "resolve_project", lambda alias: PROJECT)
    monkeypatch.setattr(
        install, "push_bundle",
        lambda plan, project, app_id, apps, data_bucket_id=None: pushed.append((app_id, data_bucket_id)) or "https://git/x.git",
    )
    monkeypatch.setattr(install, "deploy", lambda plan, app_id, app: deployed.append(app_id))
    synced: list[tuple[str, int]] = []
    monkeypatch.setattr(install, "sync_storage_access",
                        lambda alias, app, grants: synced.append((app["config_id"], len(grants))) or True)
    result = install.run_redeploy(redeploy_plan(("admin", "sales")))
    # The demo bucket goes to every bundle (only data readers use it); no token or secret call is made.
    assert pushed == [("admin", "in.c-apps-control-plane-demo"), ("sales", "in.c-apps-control-plane-demo")]
    assert deployed == ["admin", "sales"]
    tables = len(install.TABLE_COLUMNS)
    demo = len(install.DEMO_TABLES)
    assert synced == [("c-admin", tables + demo), ("c-s", demo)]
    assert result == {"redeployed": {"admin": URLS["admin"], "sales": REDEPLOY_STATE["apps"]["sales"]["url"]},
                      "storage_access_updated": ["admin", "sales"]}


def test_redeploy_command_prints_a_plan_without_yes(install, tmp_path, monkeypatch, capsys):
    state_file = tmp_path / "state.json"
    install.save_state(state_file, REDEPLOY_STATE)
    monkeypatch.setattr(install, "resolve_project", lambda alias: PROJECT)
    monkeypatch.setattr(install, "push_bundle", lambda *a, **k: pytest.fail("a plan must not push"))
    assert install.main(["redeploy", "--project", "P", "--state-file", str(state_file), "--apps", "admin"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execute_with"] == "--yes" and list(plan["plan"]["apps"]) == ["admin"]


def test_app_storage_grants(install):
    demo = "in.c-apps-control-plane-demo"
    admin = install.app_storage_grants("admin", "in.c-apps-control-plane", demo)
    assert [g["destination"] for g in admin] == (
        [f"in.c-apps-control-plane.{t}" for t in install.TABLE_COLUMNS] + [f"{demo}.{t}" for t in install.DEMO_TABLES]
    )
    before_phase2 = install.app_storage_grants("admin", "in.c-apps-control-plane", None)
    assert [g["destination"] for g in before_phase2] == [f"in.c-apps-control-plane.{t}" for t in install.TABLE_COLUMNS]
    assert [g["destination"] for g in install.app_storage_grants("sales", "in.c-x", demo)] == [f"{demo}.{t}" for t in install.DEMO_TABLES]
    assert install.app_storage_grants("launcher", "in.c-x", demo) is None


def test_sync_storage_access_writes_only_storage_and_workspace(install, monkeypatch):
    fake = FakeKbagent()
    fake.config["parameters"]["dataApp"]["secrets"] = {"#MASK_SALT": "KBC::ProjectSecure::x"}
    fake.config["storage"] = {"input": {"tables": [{"source": "in.c-old.t"}]}}
    monkeypatch.setattr(install, "kbagent", fake)
    grants = [{"destination": "in.c-demo.drug_sales", "unload_strategy": "direct-grant"}]
    app = {"app_id": "1", "config_id": "c-admin", "url": URLS["admin"]}
    assert install.sync_storage_access("P", app, grants) is True
    written = json.loads(fake.files["--configuration-file"])
    assert written["storage"] == {"output": {"tables": grants}}
    assert written["runtime"] == {"workspace": {"enabled": True}}
    assert written["authorization"] == fake.config["authorization"]
    assert written["parameters"]["dataApp"]["secrets"] == {"#MASK_SALT": "KBC::ProjectSecure::x"}
    updates = len([c for c in fake.calls if c[:2] == ["config", "update"]])
    assert install.sync_storage_access("P", app, grants) is False  # already in place: no second write
    assert len([c for c in fake.calls if c[:2] == ["config", "update"]]) == updates
