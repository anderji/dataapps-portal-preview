"""Run the whole suite locally: every app plus a fake apps-proxy in front of each.

    .venv/bin/python scripts/dev.py --config config/local.toml

For each app in [apps] this starts two processes: the app itself on its
internal `port` (uvicorn --factory) and the dev gateway on its `public_port`.
Open the launcher's public URL and sign in as a persona. Ctrl+C stops all.

Apps that declare a `frontend` in deploy/suite.toml (the admin SPA) get it
built first — `npm ci` when node_modules/ is missing, then `npm run build` — so
the FastAPI app finds <frontend>/dist. This needs Node.js and npm on PATH.

Requires `scripts/init_secrets.py` and `scripts/seed.py` to have run once.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Iterable
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from build_app_bundle import frontend_dir, load_manifest, node_min_version  # noqa: E402
from kbc_access.config import CONFIG_ENV, load_settings  # noqa: E402

SECRETS_FILE = ROOT / "secrets.local.env"


def _pump(name: str, stream) -> None:  # type: ignore[no-untyped-def]
    for line in iter(stream.readline, ""):
        sys.stderr.write(f"[{name}] {line}")
    stream.close()


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.lstrip("v").split("-")[0].split("."))


def build_frontends(app_ids: Iterable[str]) -> str | None:
    """Build the frontend of every given app that declares one in deploy/suite.toml.

    Apps that exist only locally (not in the manifest) have no frontend. Returns an
    error message to show the user, or None when every frontend was built.
    """
    manifest = load_manifest()
    try:
        frontends = [rel for app_id in app_ids if app_id in manifest["apps"] and (rel := frontend_dir(manifest, app_id))]
        if not frontends:
            return None
        minimum = node_min_version(manifest)
    except ValueError as exc:
        return str(exc)
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        return f"node and npm must be on PATH to build {', '.join(frontends)} (Node.js >= {minimum})."
    current = subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if _version_tuple(current) < _version_tuple(minimum):
        return f"Node.js {current} is too old to build {', '.join(frontends)}; need >= {minimum}."
    for rel in frontends:
        cwd = ROOT / rel
        steps = [] if (cwd / "node_modules").is_dir() else [[npm, "ci", "--no-audit", "--no-fund"]]
        steps.append([npm, "run", "build"])
        for cmd in steps:
            print(f"[{rel}] {' '.join(cmd[1:])}", file=sys.stderr)
            if subprocess.run(cmd, cwd=cwd, check=False).returncode != 0:
                return f"'npm {' '.join(cmd[1:])}' failed in {rel}."
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()

    if not SECRETS_FILE.exists():
        print(f"{SECRETS_FILE.name} is missing — run scripts/init_secrets.py first.", file=sys.stderr)
        return 1
    env = {**{k: v for k, v in dotenv_values(SECRETS_FILE).items() if v is not None}, **os.environ}
    env[CONFIG_ENV] = str(args.config.resolve())
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    settings = load_settings(args.config, env)

    gateway = settings.require_gateway()
    data, _ = settings.require_data()
    if settings.store.backend != "sqlite" or settings.store.sqlite_path is None:
        print("scripts/dev.py runs the local suite and needs [store] backend = 'sqlite'.", file=sys.stderr)
        return 1
    for required in (settings.store.sqlite_path, data.duckdb_path):
        if not settings.path(required).exists():
            print(f"{required} is missing — run scripts/seed.py --config {args.config} first.", file=sys.stderr)
            return 1

    error = build_frontends(settings.apps)
    if error:
        print(error, file=sys.stderr)
        return 1

    python = sys.executable
    commands: list[tuple[str, list[str]]] = []
    for app_id, app in settings.apps.items():
        commands.append(
            (app_id, [python, "-m", "uvicorn", "--factory", app.factory, "--host", gateway.host,
                      "--port", str(app.port), "--log-level", "warning"])
        )
        commands.append((f"{app_id}:gw", [python, "-m", "apps.gateway.main", "--app", app_id]))

    processes: list[subprocess.Popen[str]] = []
    for name, cmd in commands:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        processes.append(proc)
        threading.Thread(target=_pump, args=(name, proc.stdout), daemon=True).start()

    print("\nLocal Data App suite is starting:", file=sys.stderr)
    for app_id, app in settings.apps.items():
        print(f"  {app.name:<26} {settings.app_url(app_id)}", file=sys.stderr)
    print(f"\nStart at the launcher: {settings.app_url('launcher')}  (Ctrl+C to stop)\n", file=sys.stderr)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(1):
            dead = [p for p in processes if p.poll() is not None]
            if dead:
                print(f"A process exited with code {dead[0].returncode}; stopping the suite.", file=sys.stderr)
                break
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
