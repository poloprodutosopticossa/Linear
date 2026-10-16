import json

import pytest

from test_task_event import setup
from test_native_files import native, file
from app import main


@pytest.mark.parametrize("value,expected", [
    (162, 1), ("162", 1),
    (106, 2), ("106", 2),
    (108, 3), ("108", 3),
    (110, 4), ("110", 4),
    (None, 0), ("", 0), ("  ", 0),
    (999, 0), ("unknown", 0),
])
def test_crm_priority_in_issue_create(native, value, expected):
    client, event, state = native
    state["deal"]["UF_CRM_DEAL_1662136383881"] = value
    response = client.post("/bitrix-task-event", json=event)
    assert response.status_code == 200
    mutations = [json.loads(request.content) for request in state["requests"]
                 if request.url.host == "api.linear.app"]
    assert len(mutations) == 1 and "issueCreate" in mutations[0]["query"]
    issue_input = mutations[0]["variables"]["input"]
    assert issue_input["priority"] == expected
    assert type(issue_input["priority"]) is int
    assert issue_input["id"] == main.task_issue_id("example.bitrix24.com", "team", 42)
    assert issue_input["title"] == "Ticket_14220 - ECL 2026 15450 - Mensagem na aplicação"
    assert issue_input["assigneeId"] == "joao-linear"


def test_missing_priority_does_not_use_other_list_or_task_priority(native, monkeypatch):
    client, event, state = native
    state["deal"].update(UF_CRM_DEAL_1675256812888=162, PRIORITY=162,
                         searchContent="Crítica", COMMENTS="Crítica")
    bitrix = main.bitrix_request
    async def task_priority(method, params):
        result = await bitrix(method, params)
        if method == "tasks.task.get":
            result["result"]["task"]["priority"] = "162"
        return result
    monkeypatch.setattr(main, "bitrix_request", task_priority)
    assert client.post("/bitrix-task-event", json=event).status_code == 200
    create = next(json.loads(request.content) for request in state["requests"]
                  if request.url.host == "api.linear.app")
    assert create["variables"]["input"]["priority"] == 0


def test_priority_preserves_native_attachments_and_has_no_update_sync(native):
    client, event, state = native
    state["deal"].update(UF_CRM_DEAL_1662136383881=108,
                         UF_CRM_DEAL_1675256812888=162)
    state["files"] = [file()]
    first = client.post("/bitrix-task-event", json=event)
    assert first.status_code == 200
    assert first.json()["attachments"] == {"attached": 1, "failed": 0, "skipped": 0}
    assert len(state["attached"]) == 1
    creates = [json.loads(request.content) for request in state["requests"]
               if request.url.host == "api.linear.app"
               and "issueCreate" in json.loads(request.content)["query"]]
    assert creates[0]["variables"]["input"]["priority"] == 3
    # A later CRM priority does not update an existing issue or reupload attachments.
    state["deal"]["UF_CRM_DEAL_1662136383881"] = 162
    assert client.post("/bitrix-task-event", json=event).status_code == 502
    assert len(state["attached"]) == 1 and state["uploads"] == 1
    assert all("issueUpdate" not in json.loads(request.content)["query"]
               for request in state["requests"] if request.url.host == "api.linear.app")
