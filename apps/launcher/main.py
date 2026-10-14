"""App launcher ("rozcestník"): every app the signed-in user may open.

The launcher is the natural target of the Okta/Entra tile. It is NOT a
security boundary: it only lists links computed from the same ACL the gate
uses, and every target app runs the gate again on arrival. That is why the
launcher itself uses `access.identify` (who are you) rather than
`access.require` (may you open this app).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse

from kbc_access.identity import Principal
from kbc_access.web import Runtime, build_runtime, install

APP_ID = "launcher"
HERE = Path(__file__).parent


def create_app(runtime: Runtime) -> FastAPI:
    app = FastAPI(title="App launcher", docs_url=None, redoc_url=None, openapi_url=None)
    access = install(app, runtime, app_id=APP_ID, templates_dir=HERE / "templates")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, principal: Principal = Depends(access.identify)) -> HTMLResponse:
        snapshot = runtime.gate.snapshot()
        groups_known = [
            (external_id, [g.name for g in snapshot.groups_by_external_id.get(external_id, [])])
            for external_id in sorted(principal.groups)
        ]
        user = snapshot.users.get(principal.email)
        local_groups = [snapshot.local_groups[i] for i in sorted(principal.local_groups) if i in snapshot.local_groups]
        return access.render(
            request,
            "launcher.html",
            principal,
            apps=runtime.gate.accessible(principal),
            total_apps=sum(1 for a in snapshot.apps.values() if a.active),
            groups_known=groups_known,
            user=user,
            local_groups=local_groups,
        )

    return app


def create_default() -> FastAPI:
    return create_app(build_runtime())
