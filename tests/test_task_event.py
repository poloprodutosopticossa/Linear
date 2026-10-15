import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

# Avoid AWS credential discovery and all live services during import/tests.
os.environ.update(AWS_EC2_METADATA_DISABLED="true", R2_ACCESS_KEY_ID="test",
                  R2_SECRET_ACCESS_KEY="test", R2_ACCOUNT_ID="test")
from app import main

REAL_BITRIX_REQUEST = main.bitrix_request
REAL_LINEAR_REQUEST = main.linear_request
ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def setup(monkeypatch, tmp_path):
    for name, value in {
        "BITRIX_OUTGOING_TOKEN": "secret", "BITRIX_TASK_STATE_DB": str(tmp_path / "state.sqlite3"),
        "BITRIX_TASK_EVENTS_SINCE": "2026-10-15T00:00:00Z",
        "BITRIX_WEBHOOK_BASE": "https://example.bitrix24.com/rest/1/private/",
        "LINEAR_API_KEY": "test", "LINEAR_TEAM_ID": "team",
        "LINEAR_JOAO_ASSIGNEE_ID": "joao-linear",
    }.items():
        monkeypatch.setattr(main, name, value)
    history = [{"id": 1, "createdDate": "2026-10-15T10:00:00Z",
                "field": "RESPONSIBLE_ID", "value": {"from": "12", "to": "266"}}]
    task = {"id": "42", "title": "Preparar proposta", "responsibleId": "266",
            "ufCrmTask": ["C_99", "D_7"]}
    deal = {"ID": "7", "TITLE": "Óculos", "COMMENTS": "Detalhes do cliente",
            "OPPORTUNITY": "150", "CURRENCY_ID": "EUR"}
    async def bitrix(method, params):
        if method == "tasks.task.get":
            assert "UF_CRM_TASK" in params["select"]
            return {"result": {"task": task}}
        if method == "tasks.task.history.list":
            return {"result": {"list": history}}
        assert method == "crm.deal.get" and params == {"id": 7}
        return {"result": deal}
    remote = {}
    async def linear(query, variables):
        if "issueCreate" in query:
            item = variables["input"]
            issue = {"id": item["id"], "title": item["title"], "url": "https://linear.app/test"}
            assert item["id"] not in remote, "Duplicate mutation"
            remote[item["id"]] = issue
            return {"issueCreate": {"success": True, "issue": issue}}
        return {"issues": {"nodes": list(remote.values())}}
    bitrix_mock = AsyncMock(side_effect=bitrix)
    linear_mock = AsyncMock(side_effect=linear)
    monkeypatch.setattr(main, "bitrix_request", bitrix_mock)
    monkeypatch.setattr(main, "linear_request", linear_mock)
    payload = {"event": "ONTASKUPDATE", "ts": str(main.event_time("2026-10-15T10:00:00Z")),
               "auth": {"application_token": "secret"},
               "data": {"FIELDS_AFTER": {"ID": "42"}}}
    return TestClient(main.app), payload, task, history, bitrix_mock, linear_mock, remote


def test_create_and_persistent_duplicate(setup):
    client, event, _, _, bitrix, linear, remote = setup
    first = client.post("/bitrix-task-event", json=event)
    assert first.status_code == 200
    item = linear.call_args_list[-1].args[1]["input"]
    assert item["title"] == "Óculos" and item["assigneeId"] == "joao-linear"
    assert "150" in item["description"] and "/crm/deal/details/7/" in item["description"]
    # A new client/process has no in-memory deduplication state.
    second = TestClient(main.app).post("/bitrix-task-event", json=event)
    assert second.json()["duplicate"] is True
    assert len(remote) == 1 and bitrix.await_count == 3


@pytest.mark.parametrize("token", [None, "", "invalid"])
def test_reject_token_before_external_calls(setup, token):
    client, event, _, _, bitrix, linear, _ = setup
    event["auth"]["application_token"] = token
    assert client.post("/bitrix-task-event", json=event).status_code == 403
    bitrix.assert_not_called()
    linear.assert_not_called()


def test_form_payload(setup):
    client, event, *_ = setup
    response = client.post("/bitrix-task-event", data={
        "event": "OnTaskUpdate", "ts": event["ts"],
        "auth[application_token]": "secret", "data[FIELDS_AFTER][ID]": "42",
    })
    assert response.status_code == 200 and response.json()["issue"]


@pytest.mark.parametrize("change", ["other_owner", "empty_history", "same_owner", "old", "future", "creation"])
def test_only_proven_changes(setup, change):
    client, event, task, history, _, linear, _ = setup
    if change == "other_owner":
        task["responsibleId"] = "12"
    elif change == "empty_history":
        history.clear()
    elif change == "same_owner":
        history[0]["value"]["from"] = "266"
    elif change == "creation":
        history[0]["value"]["from"] = None
    else:
        history[0]["createdDate"] = "2026-10-14T10:00:00Z" if change == "old" else "2026-10-16T10:00:00Z"
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200 and "skipped" in response.json()
    linear.assert_not_called()


@pytest.mark.parametrize("links", [[], ["C_7"], ["D_7", "D_8"], ["D_bad"]])
def test_missing_or_ambiguous_deal(setup, links):
    client, event, task, _, _, linear, _ = setup
    task["ufCrmTask"] = links
    assert client.post("/bitrix-task-event", json=event).json()["skipped"] == "missing_or_ambiguous_deal"
    linear.assert_not_called()


def test_timeout_after_remote_commit_reconciles(setup, monkeypatch):
    client, event, _, _, _, linear, remote = setup
    original = linear.side_effect
    async def timeout(query, variables):
        result = await original(query, variables)
        if "issueCreate" in query:
            raise httpx.ReadTimeout("uncertain remote commit")
        return result
    linear.side_effect = timeout
    assert client.post("/bitrix-task-event", json=event).status_code == 502
    linear.side_effect = original
    assert client.post("/bitrix-task-event", json=event).status_code == 200
    assert len(remote) == 1
    assert sum("issueCreate" in call.args[0] for call in linear.call_args_list) == 1


def test_timeout_before_commit_reuses_saved_id(setup):
    client, event, _, _, _, linear, remote = setup
    original = linear.side_effect
    async def timeout(query, variables):
        if "issueCreate" in query:
            raise httpx.ConnectTimeout("not sent")
        return await original(query, variables)
    linear.side_effect = timeout
    assert client.post("/bitrix-task-event", json=event).status_code == 502
    saved_id = linear.call_args.args[1]["input"]["id"]
    linear.side_effect = original
    assert client.post("/bitrix-task-event", json=event).status_code == 200
    assert list(remote) == [saved_id]


def test_atomic_reservation_across_workers(setup):
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda index: main.task_state("same-task", {"id": str(index)}), range(20)))
    assert len({state["input"]["id"] for state in states}) == 1


def test_simultaneous_deliveries_create_one_issue(setup):
    _, event, _, _, _, linear, remote = setup
    lock = threading.Lock()
    original = linear.side_effect
    ids = []
    async def unique_linear(query, variables):
        with lock:
            if "issueCreate" in query:
                issue_id = variables["input"]["id"]
                ids.append(issue_id)
                if issue_id in remote:
                    raise main.HTTPException(502, "ID already exists")
            return await original(query, variables)
    linear.side_effect = unique_linear
    def send(_):
        return TestClient(main.app).post("/bitrix-task-event", json=event).status_code
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(send, range(6)))
    assert all(status in (200, 502) for status in statuses)
    assert len(remote) == 1 and len(set(ids)) == 1
    assert send(None) == 200


def test_database_confirmation_failure_recovers(setup, monkeypatch):
    client, event, _, _, _, linear, remote = setup
    original_state = main.task_state
    def fail_confirmation(key, issue_input=None, issue=None):
        if issue is not None:
            raise main.HTTPException(503, "Disk unavailable")
        return original_state(key, issue_input)
    monkeypatch.setattr(main, "task_state", fail_confirmation)
    assert client.post("/bitrix-task-event", json=event).status_code == 503
    monkeypatch.setattr(main, "task_state", original_state)
    assert client.post("/bitrix-task-event", json=event).status_code == 200
    assert len(remote) == 1
    assert sum("issueCreate" in call.args[0] for call in linear.call_args_list) == 1


def test_history_failure_does_not_create_issue(setup):
    client, event, _, _, bitrix, linear, _ = setup
    original = bitrix.side_effect
    async def failed(method, params):
        if method == "tasks.task.history.list":
            raise main.HTTPException(502, "History unavailable")
        return await original(method, params)
    bitrix.side_effect = failed
    assert client.post("/bitrix-task-event", json=event).status_code == 502
    linear.assert_not_called()


def test_paginated_history(setup):
    client, event, _, history, bitrix, _, _ = setup
    original = bitrix.side_effect
    async def paginated(method, params):
        if method == "tasks.task.history.list" and params["start"] == 0:
            return {"result": {"list": []}, "next": 50}
        return await original(method, params)
    bitrix.side_effect = paginated
    assert client.post("/bitrix-task-event", json=event).status_code == 200
    assert bitrix.await_count == 4


def test_config_and_malformed_event(setup, monkeypatch):
    client, event, *_ = setup
    assert client.post("/bitrix-task-event", json=[]).status_code == 400
    event["data"]["FIELDS_AFTER"]["ID"] = "bad"
    assert client.post("/bitrix-task-event", json=event).status_code == 400
    event["data"]["FIELDS_AFTER"]["ID"] = "42"
    monkeypatch.setattr(main, "BITRIX_TASK_STATE_DB", None)
    assert client.post("/bitrix-task-event", json=event).status_code == 503


def test_other_event_and_missing_configuration(setup, monkeypatch):
    client, event, _, _, bitrix, linear, _ = setup
    event["event"] = "ONTASKADD"
    assert client.post("/bitrix-task-event", json=event).json()["skipped"] == "event"
    monkeypatch.setattr(main, "BITRIX_OUTGOING_TOKEN", None)
    assert client.post("/bitrix-task-event", json=event).status_code == 503
    bitrix.assert_not_called()
    linear.assert_not_called()


def test_failed_creation_is_not_marked_complete(setup):
    client, event, _, _, _, linear, remote = setup
    original = linear.side_effect
    async def failed(query, variables):
        if "issueCreate" in query:
            return {"issueCreate": {"success": False, "issue": None}}
        return await original(query, variables)
    linear.side_effect = failed
    assert client.post("/bitrix-task-event", json=event).status_code == 502
    linear.side_effect = original
    assert client.post("/bitrix-task-event", json=event).status_code == 200
    assert len(remote) == 1


@pytest.mark.parametrize("status,body", [(403, {"error": "denied"}), (200, {"error": "denied"}), (200, {})])
def test_bitrix_api_failures_redact_rest_secret(setup, monkeypatch, status, body):
    def handler(request):
        assert request.url.path.endswith("/tasks.task.get.json")
        return httpx.Response(status, json=body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: ASYNC_CLIENT(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(REAL_BITRIX_REQUEST("tasks.task.get", {"taskId": 42}))
    assert caught.value.status_code == 502
    assert "private" not in caught.value.detail


@pytest.mark.parametrize("status,body", [(401, {}), (200, {"errors": [{"message": "denied"}]}), (200, {"data": None})])
def test_linear_http_and_graphql_errors(setup, monkeypatch, status, body):
    def handler(request):
        assert request.headers["authorization"] == "test"
        return httpx.Response(status, json=body)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: ASYNC_CLIENT(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(main.HTTPException) as caught:
        asyncio.run(REAL_LINEAR_REQUEST("query { viewer { id } }", {}))
    assert caught.value.status_code == 502


def test_legacy_route_still_copies_r2_attachments(setup, monkeypatch):
    client, *_ = setup
    linear = AsyncMock(side_effect=[
        {"issueCreate": {"success": True, "issue": {"id": "legacy"}}},
        {"issueUpdate": {"success": True, "issue": {"id": "legacy"}}},
    ])
    monkeypatch.setattr(main, "linear_request", linear)
    monkeypatch.setattr(main, "download_bitrix_file", AsyncMock(return_value=(b"file", "application/pdf", "a.pdf")))
    upload = Mock(return_value="https://r2.example/attachments/a.pdf")
    monkeypatch.setattr(main, "upload_to_r2", upload)
    response = client.post("/bitrix-linear", json={"data": {"FIELDS": {
        "TITLE": "Legacy", "COMMENTS": "Original", "ATTACHMENT_FILE_IDS": ["17"],
    }}})
    assert response.status_code == 200
    upload.assert_called_once_with(b"file", "a.pdf", "application/pdf")
    assert response.json()["attachments"] == ["https://r2.example/attachments/a.pdf"]
    assert "Anexos (R2)" in linear.call_args.args[1]["input"]["description"]
