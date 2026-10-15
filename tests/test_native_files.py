import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from test_task_event import setup, ASYNC_CLIENT, REAL_BITRIX_REQUEST, REAL_LINEAR_REQUEST
from app import main, native_files as files


@pytest.fixture
def native(setup, monkeypatch):
    client, event, *_ = setup
    # UF_CRM_99999 is synthetic test metadata, not the live portal's field code.
    state = {"fields": {"UF_CRM_99999": {"title": "Assunto", "type": "string", "isMultiple": False}},
             "deal": {"ID": "7", "TITLE": "Ticket_14220", "UF_CRM_99999": "ECL 2026 15450 - Mensagem na aplicação"},
             "files": [], "requests": [], "issues": set(), "uploads": 0,
             "attached": [], "download_status": {}, "put_status": 200,
             "body": b"%PDF-1.7\nfile", "mime": "application/pdf", "headers": {}}
    def handler(request):
        state["requests"].append(request)
        # Exercise protection of both URL info logging and HTTP transport debug logs.
        logging.getLogger("httpcore.http11").debug("headers=%s", dict(request.headers))
        if request.url.host == "api.linear.app":
            body = json.loads(request.content)
            query, variables = body["query"], body["variables"]
            if "issueCreate" in query:
                issue = variables["input"]
                if issue["id"] in state["issues"]:
                    return httpx.Response(200, json={"errors": [{"message": "duplicate id"}]})
                state["issues"].add(issue["id"])
                return httpx.Response(200, json={"data": {"issueCreate": {
                    "success": True, "issue": {"id": issue["id"], "title": issue["title"],
                                                "url": "https://linear.app/issue/TEST-1"}}}})
            if "fileUpload" in query:
                state["uploads"] += 1
                assert variables["contentType"] == state["mime"]
                assert variables["size"] == len(state["body"])
                return httpx.Response(200, json={"data": {"fileUpload": {
                    "success": True, "uploadFile": {
                        "uploadUrl": "https://upload.linear.app/temporary?sig=UPLOAD_SECRET",
                        "assetUrl": f"https://uploads.linear.app/private/{state['uploads']}.pdf",
                        "headers": [{"key": "x-upload-token", "value": "SIGNED_HEADER_SECRET"}],
                    }}}})
            assert "attachmentCreate" in query, "No Linear lookup or state sync"
            state["attached"].append(variables["input"])
            return httpx.Response(200, json={"data": {"attachmentCreate": {
                "success": True, "attachment": {"id": variables["input"]["id"]}}}})
        if request.method == "PUT":
            assert request.headers["x-upload-token"] == "SIGNED_HEADER_SECRET"
            assert request.headers["content-type"] == state["mime"]
            assert request.content == state["body"]
            assert "authorization" not in request.headers
            status = state["put_status"]
            if isinstance(status, dict):
                status = status.get(state["uploads"], 200)
            return httpx.Response(status, text="UPLOAD_SECRET")
        if request.method == "GET":
            assert request.url.host == "example.bitrix24.com"
            assert "authorization" not in request.headers
            status = state["download_status"].get(request.url.path, 200)
            return httpx.Response(status, content=state["body"], headers={
                "content-type": state["mime"],
                "content-disposition": "attachment; filename*=UTF-8''proposta%20%C3%B3culos.pdf",
                **state["headers"],
            })
        method = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        params = json.loads(request.content)
        if method == "tasks.task.get":
            result = {"task": {"id": "42", "responsibleId": "266", "ufCrmTask": ["D_7"]}}
        elif method == "tasks.task.history.list":
            result = {"list": [{"field": "RESPONSIBLE_ID", "createdDate": "2026-10-15T10:00:00Z",
                                 "value": {"from": "12", "to": "266"}}]}
        elif method == "crm.deal.get":
            assert params == {"id": 7}
            result = state["deal"]
        elif method == "crm.deal.fields":
            result = state["fields"]
        else:
            assert method == "crm.item.get" and params == {"entityTypeId": 2, "id": 7}
            assert state["issues"], "Must create the issue before fetching files"
            result = {"item": {"id": 7, files.CRM_FILE_FIELD: state["files"]}}
        return httpx.Response(200, json={"result": result})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: ASYNC_CLIENT(
        transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(main, "bitrix_request", REAL_BITRIX_REQUEST)
    monkeypatch.setattr(main, "linear_request", REAL_LINEAR_REQUEST)
    return client, event, state


def file(file_id=529120):
    return {"id": file_id, "urlMachine": f"https://example.bitrix24.com/crm/{file_id}?token=CRM_SECRET"}


def test_title_with_subject(native):
    client, event, state = native
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200
    assert response.json()["issue"]["title"] == "Ticket_14220 - ECL 2026 15450 - Mensagem na aplicação"
    assert response.json()["attachments"] == {"attached": 0, "failed": 0, "skipped": 0}
    assert state["uploads"] == 0


@pytest.mark.parametrize("subject", [None, "", "  "])
def test_title_without_subject(native, subject):
    client, event, state = native
    state["deal"]["UF_CRM_99999"] = subject
    assert client.post("/bitrix-task-event", json=event).json()["issue"]["title"] == "Ticket_14220"


def test_subject_already_in_title(native):
    client, event, state = native
    title = "Ticket_14220 - " + state["deal"]["UF_CRM_99999"]
    state["deal"]["TITLE"] = title
    assert client.post("/bitrix-task-event", json=event).json()["issue"]["title"] == title


@pytest.mark.parametrize("mode", ["missing", "ambiguous", "wrong_type"])
def test_unverified_subject_falls_back_without_guessing(native, mode):
    client, event, state = native
    if mode == "missing":
        state["fields"] = {}
    elif mode == "ambiguous":
        state["fields"]["UF_CRM_OTHER"] = state["fields"]["UF_CRM_99999"]
    else:
        state["fields"]["UF_CRM_99999"]["type"] = "file"
    state["deal"].update(SUBJECT="Never use this", searchContent="Or this")
    assert client.post("/bitrix-task-event", json=event).json()["issue"]["title"] == "Ticket_14220"


@pytest.mark.parametrize("count", [1, 3])
def test_native_download_upload_and_attachment(native, count):
    client, event, state = native
    state["files"] = [file(529120 + index) for index in range(count)]
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200
    assert response.json()["attachments"] == {"attached": count, "failed": 0, "skipped": 0}
    assert len(state["attached"]) == count
    for attachment in state["attached"]:
        assert attachment["title"] == "proposta óculos.pdf"
        assert attachment["issueId"] in state["issues"]
        assert attachment["url"].startswith("https://uploads.linear.app/private/")
        assert "temporary" not in attachment["url"] and "?" not in attachment["url"]
    assert all("disk.file.get" not in str(request.url) for request in state["requests"])


def test_download_failure_continues_next_file(native, caplog):
    client, event, state = native
    state["files"] = [file(), file(529121)]
    state["download_status"]["/crm/529120"] = 403
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200
    assert response.json()["attachments"] == {"attached": 1, "failed": 1, "skipped": 0}
    assert "CRM attachment failed" in caplog.text


def test_upload_failure_preserves_issue(native):
    client, event, state = native
    state["files"] = [file(), file(529121)]
    state["put_status"] = 500
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200 and state["issues"]
    assert response.json()["attachments"]["failed"] == 2
    assert state["uploads"] == 2 and not state["attached"]


def test_upload_failure_continues_with_remaining_file(native):
    client, event, state = native
    state.update(files=[file(), file(529121)], put_status={1: 500})
    response = client.post("/bitrix-task-event", json=event)
    assert response.json()["attachments"] == {"attached": 1, "failed": 1, "skipped": 0}
    assert len(state["attached"]) == 1


@pytest.mark.parametrize("url", [
    "https://evil.test/file?token=CRM_SECRET", "http://example.bitrix24.com/file",
    "https://example.bitrix24.com.evil.test/file", "https://u:p@example.bitrix24.com/file",
    "https://example.bitrix24.com:8443/file", "https://127.0.0.1/file",
])
def test_unauthorized_download_never_requested(native, url):
    client, event, state = native
    state["files"] = [{"id": 1, "urlMachine": url}]
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200 and response.json()["attachments"]["failed"] == 1
    assert not any(request.method == "GET" for request in state["requests"])


def test_redirect_not_followed(native):
    client, event, state = native
    state["files"] = [file()]
    state["download_status"]["/crm/529120"] = 302
    state["headers"]["location"] = "https://evil.test/CRM_SECRET"
    assert client.post("/bitrix-task-event", json=event).json()["attachments"]["failed"] == 1
    assert sum(request.method == "GET" for request in state["requests"]) == 1


@pytest.mark.parametrize("header", [True, False])
def test_size_limit_with_and_without_length(native, monkeypatch, header):
    client, event, state = native
    state["files"] = [file()]
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 4)
    # MockTransport adds content-length; force streaming-style headers when needed.
    if not header:
        state["headers"]["content-length"] = ""
    assert client.post("/bitrix-task-event", json=event).json()["attachments"]["failed"] == 1
    assert state["uploads"] == 0


@pytest.mark.parametrize("mime,body", [
    ("text/html", b"<html>Error CRM_SECRET</html>"),
    ("application/json", b'{"error":"CRM_SECRET"}'),
    ("application/pdf", b"   <html>Error</html>"),
    ("application/octet-stream", b'   {"error":"CRM_SECRET"}'),
])
def test_error_content_rejected(native, mime, body):
    client, event, state = native
    state.update(files=[file()], mime=mime, body=body)
    assert client.post("/bitrix-task-event", json=event).json()["attachments"]["failed"] == 1
    assert state["uploads"] == 0


def test_secrets_absent_from_debug_logs_and_http_response(native, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    client, event, state = native
    state["files"] = [file()]
    state["put_status"] = 403
    monkeypatch.setattr(main, "LINEAR_API_KEY", "LINEAR_API_SECRET")
    monkeypatch.setattr(main, "BITRIX_OUTGOING_TOKEN", "OUTGOING_SECRET")
    event["auth"]["application_token"] = "OUTGOING_SECRET"
    response = client.post("/bitrix-task-event", json=event)
    combined = caplog.text + response.text
    for secret in ("CRM_SECRET", "UPLOAD_SECRET", "SIGNED_HEADER_SECRET", "/rest/1/private/", "urlMachine", "LINEAR_API_SECRET", "OUTGOING_SECRET"):
        assert secret not in combined
    assert "CRM attachment failed" in combined and response.status_code == 200


def test_duplicate_file_ids_and_webhook_replay_do_not_reupload(native):
    client, event, state = native
    state["files"] = [file(), file()]
    assert client.post("/bitrix-task-event", json=event).json()["attachments"]["attached"] == 1
    assert client.post("/bitrix-task-event", json=event).status_code == 502
    assert state["uploads"] == 1 and len(state["attached"]) == 1


def test_ignored_event_never_fetches_files(native):
    client, event, state = native
    event["ts"] = str(int(event["ts"]) + 60)
    assert client.post("/bitrix-task-event", json=event).json()["skipped"] == "no_assignment_change"
    assert all("crm.item.get" not in str(request.url) for request in state["requests"])
    assert not state["issues"]


def test_file_count_and_aggregate_limits(native, monkeypatch):
    client, event, state = native
    state["files"] = [file(1), file(2), file(3)]
    monkeypatch.setattr(files, "MAX_FILES", 1)
    response = client.post("/bitrix-task-event", json=event)
    assert response.json()["attachments"] == {"attached": 1, "failed": 0, "skipped": 2}


def test_aggregate_byte_limit(native, monkeypatch):
    client, event, state = native
    state["files"] = [file(1), file(2)]
    monkeypatch.setattr(files, "MAX_TOTAL_BYTES", len(state["body"]))
    response = client.post("/bitrix-task-event", json=event)
    assert response.json()["attachments"] == {"attached": 1, "failed": 0, "skipped": 1}


@pytest.mark.parametrize("method", ["crm.deal.fields", "crm.item.get"])
def test_optional_metadata_failure_never_prevents_issue(setup, monkeypatch, method):
    client, event, *_ = setup
    original = main.bitrix_request
    async def failed(name, params):
        if name == method:
            raise RuntimeError("CRM_SECRET https://portal/rest/private/")
        return await original(name, params)
    monkeypatch.setattr(main, "bitrix_request", failed)
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200 and response.json()["issue"]
    assert "CRM_SECRET" not in response.text


def test_graphql_attachment_failure_is_sanitized(setup, monkeypatch, caplog):
    client, event, *_ = setup
    bitrix = main.bitrix_request
    linear = main.linear_request
    async def data(name, params):
        if name == "crm.item.get":
            return {"result": {"item": {"id": 7, files.CRM_FILE_FIELD: [file()]}}}
        return await bitrix(name, params)
    async def error(query, variables):
        if "fileUpload" in query:
            raise main.HTTPException(502, "UPLOAD_SECRET")
        return await linear(query, variables)
    monkeypatch.setattr(main, "bitrix_request", data)
    monkeypatch.setattr(main, "linear_request", error)
    monkeypatch.setattr(files, "download_crm_file", AsyncMock(
        return_value=(b"%PDF-file", "application/pdf", "file.pdf")))
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200 and response.json()["attachments"]["failed"] == 1
    assert "UPLOAD_SECRET" not in caplog.text + response.text


def test_wall_clock_budget_preserves_issue(setup, monkeypatch):
    client, event, *_ = setup
    original = main.bitrix_request
    async def slow(method, params):
        if method == "crm.item.get":
            await asyncio.sleep(1)
        return await original(method, params)
    monkeypatch.setattr(main, "bitrix_request", slow)
    monkeypatch.setattr(files, "TRANSFER_SECONDS", 0.01)
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200 and response.json()["attachments"]["failed"] == 1
