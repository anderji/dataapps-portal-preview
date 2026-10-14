"""StorageClient against the in-memory fake Storage API (tests/fake_storage_api.py)."""

from __future__ import annotations

from urllib.parse import unquote

import httpx
import pytest

from kbc_access import keboola_storage
from kbc_access.keboola_storage import StorageClient, StorageError, StorageSettings, _abs_url
from tests.fake_storage_api import ABS_ENDPOINT, ABS_SAS, GCS_HOST, STACK_URL, TOKEN, FakeStorageApi

TABLE = "in.c-acl.users"
COLUMNS = ["email", "display_name", "active"]


@pytest.fixture
def api() -> FakeStorageApi:
    fake = FakeStorageApi()
    fake.add_table(TABLE, COLUMNS, ["email"])
    return fake


@pytest.fixture
def client(api: FakeStorageApi) -> StorageClient:
    return api.client()


def _row(email: str, name: str = "N", active: str = "1") -> dict[str, str]:
    return {"email": email, "display_name": name, "active": active}


# ---- read_table -------------------------------------------------------------


def test_read_table_parses_quotes_commas_newlines_and_unicode(api: FakeStorageApi, client: StorageClient) -> None:
    tricky = [
        _row("a@x.example", 'He said "hi", then left'),
        _row("b@x.example", "Žluťoučký kůň, Praha"),
        _row("c@x.example", "line one\nline two"),
        _row("d@x.example", ""),
    ]
    api.tables[TABLE].rows = [dict(r) for r in tricky]
    assert client.read_table(TABLE) == tricky


def test_read_table_empty_table_returns_no_rows(client: StorageClient) -> None:
    assert client.read_table(TABLE) == []


def test_read_table_sends_limit_and_token(api: FakeStorageApi, client: StorageClient) -> None:
    client.read_table(TABLE)
    request = api.storage_requests("GET", "data-preview")[0]
    assert request.url.params["limit"] == "1000"
    assert request.headers["X-StorageApi-Token"] == TOKEN
    # The table id is URL-encoded as one path segment.
    assert "in.c-acl.users" in unquote(request.url.path)


def test_read_table_refuses_a_table_at_the_preview_cap(api: FakeStorageApi) -> None:
    client = api.client(preview_row_limit=3)
    api.tables[TABLE].rows = [_row(f"u{i}@x.example") for i in range(2)]
    assert len(client.read_table(TABLE)) == 2  # below the cap: fine
    api.tables[TABLE].rows.append(_row("u2@x.example"))
    with pytest.raises(StorageError, match="3\\+ rows"):
        client.read_table(TABLE)  # exactly at the cap: may be truncated, so refused
    api.tables[TABLE].rows += [_row(f"v{i}@x.example") for i in range(5)]
    with pytest.raises(StorageError):
        client.read_table(TABLE)


def test_read_missing_table_is_storage_error(client: StorageClient) -> None:
    with pytest.raises(StorageError, match="HTTP 404"):
        client.read_table("in.c-acl.nope")


# ---- upsert / delete ----------------------------------------------------------


def test_upsert_creates_and_replaces_rows_by_primary_key(api: FakeStorageApi, client: StorageClient) -> None:
    client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example", "A"), _row("b@x.example", "B")])
    assert api.rows(TABLE) == [_row("a@x.example", "A"), _row("b@x.example", "B")]
    client.upsert_rows(TABLE, COLUMNS, [_row("b@x.example", "B2", "0"), _row("c@x.example", "C, \"quoted\"")])
    assert api.rows(TABLE) == [_row("a@x.example", "A"), _row("b@x.example", "B2", "0"), _row("c@x.example", 'C, "quoted"')]
    assert client.read_table(TABLE) == api.rows(TABLE)
    # Each upsert is one file + one import job.
    assert [n for _, n in api.imports] == [2, 2]
    form = api.storage_requests("POST", "import-async")[0].content.decode()
    assert "incremental=1" in form


def test_upsert_nothing_makes_no_request(api: FakeStorageApi, client: StorageClient) -> None:
    client.upsert_rows(TABLE, COLUMNS, [])
    assert api.requests == []


def test_upsert_missing_columns_filled_blank(api: FakeStorageApi, client: StorageClient) -> None:
    client.upsert_rows(TABLE, COLUMNS, [{"email": "a@x.example"}])
    assert api.rows(TABLE) == [{"email": "a@x.example", "display_name": "", "active": ""}]


def test_upsert_refuses_keys_outside_declared_columns(api: FakeStorageApi, client: StorageClient) -> None:
    # A key outside `columns` is a caller bug; it must not be silently dropped.
    with pytest.raises(ValueError, match="surprise"):
        client.upsert_rows(TABLE, COLUMNS, [{**_row("a@x.example"), "surprise": "x"}])
    assert api.rows(TABLE) == []


def test_delete_rows_by_key_several_values_one_job(api: FakeStorageApi, client: StorageClient) -> None:
    api.tables[TABLE].rows = [_row(f"{c}@x.example") for c in "abcd"]
    client.delete_rows(TABLE, "email", ["a@x.example", "c@x.example", "a@x.example", "zz@x.example"])
    assert [r["email"] for r in api.rows(TABLE)] == ["b@x.example", "d@x.example"]
    assert len(api.deletes) == 1
    request = api.storage_requests("DELETE")[0]
    assert request.url.params["whereColumn"] == "email"
    assert request.url.params.get_list("whereValues[]") == ["a@x.example", "c@x.example", "zz@x.example"]


def test_delete_nothing_makes_no_request(api: FakeStorageApi, client: StorageClient) -> None:
    # Important: the real endpoint truncates the table when no filter is sent.
    api.tables[TABLE].rows = [_row("a@x.example")]
    client.delete_rows(TABLE, "email", [])
    assert api.requests == [] and len(api.rows(TABLE)) == 1


# ---- schema -----------------------------------------------------------------------


def test_create_table_and_list_tables(api: FakeStorageApi, client: StorageClient) -> None:
    client.create_table("in.c-acl", "app_grants", ["row_key", "app_id", "subject_id"], ["row_key"])
    created = api.tables["in.c-acl.app_grants"]
    assert created.columns == ["row_key", "app_id", "subject_id"]
    assert created.primary_key == ["row_key"]
    assert created.rows == []
    assert sorted(client.list_tables("in.c-acl")) == ["app_grants", "users"]


def test_create_existing_table_fails_via_job(client: StorageClient) -> None:
    with pytest.raises(StorageError, match="already exists"):
        client.create_table("in.c-acl", "users", COLUMNS, ["email"])


def test_bucket_exists(api: FakeStorageApi, client: StorageClient) -> None:
    assert client.bucket_exists("in.c-acl") is True
    assert client.bucket_exists("in.c-missing") is False


def test_bucket_exists_other_errors_propagate(api: FakeStorageApi, client: StorageClient) -> None:
    api.fail_http("GET", "buckets/in.c-acl", status=500)
    with pytest.raises(StorageError, match="HTTP 500"):
        client.bucket_exists("in.c-acl")


# ---- jobs ------------------------------------------------------------------------------


def test_job_is_polled_until_success(api: FakeStorageApi) -> None:
    sleeps: list[float] = []
    client = api.client(sleep=sleeps.append, job_poll_seconds=0.25)
    client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    # waiting -> (sleep, poll) processing -> (sleep, poll) success
    assert sleeps == [0.25, 0.25]
    assert len(api.storage_requests("GET", "/jobs/")) == 2
    assert len(api.rows(TABLE)) == 1


def test_job_error_is_storage_error_and_nothing_applied(api: FakeStorageApi, client: StorageClient) -> None:
    api.fail_job("import", message="Table is locked")
    with pytest.raises(StorageError, match="Table is locked"):
        client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    assert api.rows(TABLE) == []


def test_delete_job_error_is_storage_error(api: FakeStorageApi, client: StorageClient) -> None:
    api.tables[TABLE].rows = [_row("a@x.example")]
    api.fail_job("delete")
    with pytest.raises(StorageError, match="Injected job failure"):
        client.delete_rows(TABLE, "email", ["a@x.example"])
    assert len(api.rows(TABLE)) == 1


def test_job_timeout_is_storage_error(api: FakeStorageApi, monkeypatch: pytest.MonkeyPatch) -> None:
    api.jobs_never_finish = True
    clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(keboola_storage.time, "monotonic", lambda: clock[0])
    client = api.client(sleep=fake_sleep, job_timeout_seconds=1.0, job_poll_seconds=0.3)
    with pytest.raises(StorageError, match="did not finish in 1.0s"):
        client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    polls = api.storage_requests("GET", "/jobs/")
    assert 1 <= len(polls) <= 5  # bounded by timeout / poll interval, not endless
    assert api.rows(TABLE) == []


# ---- errors and secrets -------------------------------------------------------------------


def test_http_error_message_has_status_and_body_but_never_the_token(api: FakeStorageApi, client: StorageClient) -> None:
    api.fail_http("GET", "data-preview", status=500, body="Backend exploded")
    with pytest.raises(StorageError) as info:
        client.read_table(TABLE)
    message = str(info.value)
    assert "HTTP 500" in message and "Backend exploded" in message
    assert TOKEN not in message


def test_transport_error_message_never_contains_the_token() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect, headers={dict(request.headers)}", request=request)

    settings = StorageSettings(STACK_URL, TOKEN, 5.0, 5.0, 0.01, 100)
    client = StorageClient(settings, transport=httpx.MockTransport(boom), sleep=lambda _s: None)
    with pytest.raises(StorageError) as info:
        client.read_table(TABLE)
    assert TOKEN not in str(info.value)
    assert "ConnectError" in str(info.value)


def test_wrong_token_is_rejected_by_api(api: FakeStorageApi) -> None:
    client = api.client(token="another-token-0123456789abcdef")
    with pytest.raises(StorageError, match="HTTP 401"):
        client.read_table(TABLE)


def test_every_storage_request_carries_the_token(api: FakeStorageApi, client: StorageClient) -> None:
    client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    client.delete_rows(TABLE, "email", ["a@x.example"])
    client.create_table("in.c-acl", "groups", ["group_id"], ["group_id"])
    client.list_tables("in.c-acl")
    client.bucket_exists("in.c-acl")
    storage = api.storage_requests()
    assert storage and all(r.headers.get("X-StorageApi-Token") == TOKEN for r in storage)


def test_gcs_upload_uses_bearer_and_never_the_storage_token(api: FakeStorageApi, client: StorageClient) -> None:
    client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    client.create_table("in.c-acl", "groups", ["group_id"], ["group_id"])
    assert len(api.cloud_requests) == 2
    for request in api.cloud_requests:
        assert request.url.host == GCS_HOST
        assert request.headers["Authorization"].startswith("Bearer ya29.fake-gcs-token-")
        assert "x-storageapi-token" not in {k.lower() for k in request.headers}
        assert TOKEN not in str(request.url) and TOKEN.encode() not in request.content


def test_cloud_upload_failure_reports_status_only(api: FakeStorageApi, client: StorageClient) -> None:
    api.cloud_put_status = 403
    with pytest.raises(StorageError) as info:
        client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    assert "HTTP 403" in str(info.value)
    assert "signed-url-secret" not in str(info.value)
    assert api.storage_requests("POST", "import-async") == []  # no import of a missing file
    assert api.rows(TABLE) == []


@pytest.mark.parametrize("url", ["http://connection.keboola.com", "connection.keboola.com", "ftp://x"])
def test_non_https_stack_url_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        StorageClient(StorageSettings(url, TOKEN, 5.0, 5.0, 0.01, 100))


def test_trailing_slash_in_stack_url_is_fine(api: FakeStorageApi) -> None:
    client = api.client(stack_url=STACK_URL + "/")
    assert client.bucket_exists("in.c-acl")


# ---- other clouds ---------------------------------------------------------------------------


def test_abs_url_keeps_equals_signs_inside_the_sas() -> None:
    params = {
        "blobName": "exp-15/1/f.csv",
        "container": "files",
        "absCredentials": {"SASConnectionString": f"BlobEndpoint={ABS_ENDPOINT}/;SharedAccessSignature={ABS_SAS}"},
    }
    assert _abs_url(params) == f"{ABS_ENDPOINT}/files/exp-15/1/f.csv?{ABS_SAS}"


def test_azure_upload_end_to_end() -> None:
    api = FakeStorageApi(cloud="abs")
    api.add_table(TABLE, COLUMNS, ["email"])
    client = api.client()
    client.upsert_rows(TABLE, COLUMNS, [_row("a@x.example", "Ádám")])
    assert api.rows(TABLE) == [_row("a@x.example", "Ádám")]
    put = api.cloud_requests[0]
    assert str(put.url).startswith(f"{ABS_ENDPOINT}/exp-15-files/") and str(put.url).endswith(f"?{ABS_SAS}")
    assert put.headers["x-ms-blob-type"] == "BlockBlob"
    assert "x-storageapi-token" not in {k.lower() for k in put.headers}


def test_presigned_url_upload() -> None:
    api = FakeStorageApi(cloud="url")
    api.add_table(TABLE, COLUMNS, ["email"])
    api.client().upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    assert len(api.rows(TABLE)) == 1
    assert "x-storageapi-token" not in {k.lower() for k in api.cloud_requests[0].headers}


def test_aws_federated_credentials_fail_clearly_before_any_upload() -> None:
    api = FakeStorageApi(cloud="aws")
    api.add_table(TABLE, COLUMNS, ["email"])
    with pytest.raises(StorageError, match="AWS"):
        api.client().upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
    assert api.cloud_requests == []
    assert api.storage_requests("POST", "import-async") == []
    assert api.rows(TABLE) == []


def test_prepare_without_upload_target_fails() -> None:
    api = FakeStorageApi(cloud="none")
    api.add_table(TABLE, COLUMNS, ["email"])
    with pytest.raises(StorageError, match="no upload target"):
        api.client().upsert_rows(TABLE, COLUMNS, [_row("a@x.example")])
