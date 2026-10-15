from fastapi import FastAPI, Request, HTTPException
import httpx
import os
from typing import Any, Dict, List, Tuple

import boto3
from botocore.client import Config
from urllib.parse import urlparse
import os as _os
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime
from urllib.parse import parse_qs

BITRIX_OUTGOING_TOKEN = os.getenv("BITRIX_OUTGOING_TOKEN")
BITRIX_TASK_EVENTS_SINCE = os.getenv("BITRIX_TASK_EVENTS_SINCE")
LINEAR_JOAO_ASSIGNEE_ID = os.getenv("LINEAR_JOAO_ASSIGNEE_ID")

# -------------------------------------------------
#  Configuração
# -------------------------------------------------

app = FastAPI()

# Linear
LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID")

# Bitrix (webhook REST base, termina com '/')
# ex.: https://polo.bitrix24.com/rest/1/2cepjq7u3z7vp7p/
BITRIX_WEBHOOK_BASE = os.getenv("BITRIX_WEBHOOK_BASE")

# Cloudflare R2
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
# ex.: https://pub-xxxxxx.r2.dev
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL")

# Cliente S3 compatível com R2
r2_client = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


# -------------------------------------------------
#  Helpers Linear
# -------------------------------------------------

async def linear_request(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Envia um pedido GraphQL para o Linear."""
    if not LINEAR_API_KEY:
        raise HTTPException(500, "Falta LINEAR_API_KEY")

    headers = {
        # Linear quer a chave crua, sem "Bearer"
        "Authorization": LINEAR_API_KEY.strip(),
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            LINEAR_API_URL,
            json={"query": query, "variables": variables},
            headers=headers,
        )

    if not resp.is_success:
        raise HTTPException(502, "Falha HTTP do Linear")

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(
            502,
            detail=f"Erro na resposta do Linear (status {resp.status_code}): {resp.text[:500]}",
        )

    if not isinstance(data, dict):
        raise HTTPException(502, "Resposta Linear inválida")
    if data.get("errors"):
        raise HTTPException(502, detail={"linear_errors": data["errors"]})

    if not isinstance(data.get("data"), dict):
        raise HTTPException(502, "Resposta Linear inválida")
    return data["data"]


async def get_user_id_by_email(email: str | None) -> str | None:
    """Procura utilizador do Linear pelo email."""
    if not email:
        return None

    query = """
    {
      users(first: 200) {
        nodes { id email }
      }
    }
    """

    data = await linear_request(query, {})
    users = (data.get("users") or {}).get("nodes") or []

    email_lower = email.lower()
    for u in users:
        if (u.get("email") or "").lower() == email_lower:
            return u.get("id")

    return None


# -------------------------------------------------
#  Helpers Bitrix
# -------------------------------------------------

async def download_bitrix_file(file_id: int) -> Tuple[bytes, str, str]:
    """
    Faz download de um ficheiro do Bitrix usando disk.file.get + DOWNLOAD_URL.
    Devolve (bytes, content_type, filename).
    """
    if not BITRIX_WEBHOOK_BASE:
        raise HTTPException(500, "Falta BITRIX_WEBHOOK_BASE")

    base = BITRIX_WEBHOOK_BASE.rstrip("/")

    # 1) Buscar metadados do ficheiro
    url_meta = f"{base}/disk.file.get.json"
    params = {"id": file_id}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url_meta, params=params)
        resp.raise_for_status()
        data = resp.json()

    file_info = data.get("result") or {}
    filename = file_info.get("NAME") or f"bitrix_file_{file_id}"
    download_url = file_info.get("DOWNLOAD_URL")

    if not download_url:
        raise HTTPException(502, f"Bitrix não devolveu DOWNLOAD_URL para o ficheiro {file_id}")

    # 2) Download real do ficheiro (URL já autenticada pelo webhook)
    async with httpx.AsyncClient(timeout=60.0) as client:
        file_resp = await client.get(download_url)
        file_resp.raise_for_status()
        file_bytes = file_resp.content
        content_type = file_resp.headers.get("content-type", "application/octet-stream")

    return file_bytes, content_type, filename


# -------------------------------------------------
#  Helper R2
# -------------------------------------------------

def upload_to_r2(file_bytes: bytes, filename: str, content_type: str | None = None) -> str:
    """
    Envia ficheiro para o bucket R2 e devolve URL público.
    """
    if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL]):
        raise HTTPException(500, "Configuração R2 incompleta.")

    # pasta /attachments dentro do bucket
    key = f"attachments/{filename}"

    r2_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=content_type or "application/octet-stream",
    )

    # Public Development URL, ex.: https://pub-xxxx.r2.dev/attachments/ficheiro.pdf
    return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"


# -------------------------------------------------
#  Endpoints
# -------------------------------------------------

async def bitrix_request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Use only the configured REST endpoint, never URLs or credentials from events."""
    if not BITRIX_WEBHOOK_BASE:
        raise HTTPException(503, "Falta BITRIX_WEBHOOK_BASE")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{BITRIX_WEBHOOK_BASE.rstrip('/')}/{method}.json", json=params
            )
            response.raise_for_status()
            data = response.json()
        if (not isinstance(data, dict) or data.get("error")
                or not isinstance(data.get("result"), dict)):
            raise ValueError("Invalid Bitrix result")
        return data
    except (httpx.HTTPError, ValueError):
        # HTTP exceptions can contain the secret REST webhook URL.
        raise HTTPException(502, "Falha ao consultar Bitrix24") from None


def event_time(value: Any) -> int:
    """Parse a timezone-qualified history/config date without host timezone assumptions."""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return int(parsed.timestamp())


async def task_assignment_change(task_id: int, since: int, timestamp: int) -> bool:
    """Match a real reassignment to the event's Unix timestamp (second precision).

    An older reassignment is not evidence that a later edit changed the owner.
    No local snapshot is needed, including after a Free instance restarts.
    """
    start = 0
    seen = set()
    while True:
        data = await bitrix_request("tasks.task.history.list", {
            "taskId": task_id, "filter": {"FIELD": "RESPONSIBLE_ID"},
            "order": {"createdDate": "DESC"}, "start": start,
        })
        entries = (data.get("result") or {}).get("list")
        if not isinstance(entries, list):
            raise HTTPException(502, "Histórico Bitrix24 inválido")
        for entry in entries:
            try:
                changed_at = event_time(entry["createdDate"])
                values = entry["value"]
                previous = int(values["from"])
                current = int(values["to"])
            except (KeyError, ValueError, TypeError):
                continue
            if (entry.get("field") == "RESPONSIBLE_ID"
                    and since <= changed_at == timestamp
                    and previous > 0 and previous != 266 and current == 266):
                return True
        next_page = data.get("next")
        if next_page is None:
            return False
        if not isinstance(next_page, int) or next_page in seen or next_page <= start:
            raise HTTPException(502, "Paginação Bitrix24 inválida")
        seen.add(next_page)
        start = next_page


def task_issue_id(portal: str, team_id: str, task_id: int) -> str:
    """Stable identity, with the UUID v4 format required by Linear's create input.

    Hash a versioned, unambiguous tuple; version/variant bits only adapt the wire
    format. This is deterministic, not a randomly generated UUID v4.
    """
    identity = json.dumps(["bitrix-linear-task-v1", portal.strip().lower(),
                           team_id.strip().lower(), int(task_id)], separators=(",", ":"))
    return str(uuid.UUID(bytes=hashlib.sha256(identity.encode("utf-8")).digest()[:16], version=4))


async def deliver_task_issue(issue_input: Dict[str, Any]) -> Dict[str, Any]:
    """Create directly: no issue lookup, local state, or status synchronization."""
    try:
        result = await linear_request("""
            mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success issue { id identifier title url }
              }
            }
        """, {"input": issue_input})
    except httpx.HTTPError:
        raise HTTPException(502, "Falha ao criar issue; resultado remoto não confirmado") from None
    created = result.get("issueCreate")
    if not isinstance(created, dict):
        raise HTTPException(502, "Resposta Linear inválida")
    issue = created.get("issue")
    if (created.get("success") is not True or not isinstance(issue, dict)
            or issue.get("id") != issue_input["id"]):
        raise HTTPException(502, "Linear não confirmou a criação da issue")
    return {"ok": True, "issue": issue}


@app.post("/bitrix-task-event")
async def bitrix_task_event(request: Request):
    if not BITRIX_OUTGOING_TOKEN:
        raise HTTPException(503, "Falta BITRIX_OUTGOING_TOKEN")
    content_type = request.headers.get("content-type", "").split(";")[0].lower()
    try:
        if content_type == "application/json":
            payload = await request.json()
        elif content_type == "application/x-www-form-urlencoded":
            form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
            def single(name):
                values = form.get(name, [])
                if len(values) > 1:
                    raise ValueError("Repeated field")
                return values[0] if values else None
            payload = {
                "event": single("event"), "ts": single("ts"),
                "auth": {"application_token": single("auth[application_token]")},
                "data": {section: {"ID": single(f"data[{section}][ID]")}
                         for section in ("FIELDS_AFTER", "FIELDS_BEFORE")},
            }
        else:
            raise HTTPException(415, "Formato de evento não suportado")
        token = (payload.get("auth") or {}).get("application_token")
        if not isinstance(token, str) or not hmac.compare_digest(
                token.encode(), BITRIX_OUTGOING_TOKEN.encode()):
            raise HTTPException(403, "Token inválido")
        if str(payload.get("event", "")).upper() != "ONTASKUPDATE":
            return {"ok": True, "skipped": "event"}
        data = payload.get("data") or {}
        after = data.get("FIELDS_AFTER") or {}
        before = data.get("FIELDS_BEFORE") or {}
        task_id = int(after.get("ID") or before.get("ID"))
        timestamp = int(payload.get("ts"))
        if task_id <= 0 or timestamp <= 0:
            raise ValueError("Invalid event")
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Evento Bitrix24 inválido") from None
    try:
        since = event_time(BITRIX_TASK_EVENTS_SINCE)
    except (ValueError, TypeError):
        raise HTTPException(503, "Configure BITRIX_TASK_EVENTS_SINCE com fuso horário") from None
    if not all([LINEAR_API_KEY, LINEAR_TEAM_ID, BITRIX_WEBHOOK_BASE]):
        raise HTTPException(503, "Configuração Bitrix24/Linear incompleta")
    portal = urlparse(BITRIX_WEBHOOK_BASE).netloc.lower()
    if timestamp < since:
        return {"ok": True, "skipped": "before_activation"}
    task_result = await bitrix_request("tasks.task.get", {
        "taskId": task_id, "select": ["ID", "TITLE", "RESPONSIBLE_ID", "UF_CRM_TASK"],
    })
    task = (task_result.get("result") or {}).get("task")
    if not isinstance(task, dict) or str(task.get("id")) != str(task_id):
        raise HTTPException(502, "Tarefa Bitrix24 inválida")
    if str(task.get("responsibleId")) != "266":
        return {"ok": True, "skipped": "responsible"}
    if not await task_assignment_change(task_id, since, timestamp):
        return {"ok": True, "skipped": "no_assignment_change"}
    links = task.get("ufCrmTask") or []
    if isinstance(links, str):
        links = [links]
    if not isinstance(links, list):
        raise HTTPException(502, "Associação CRM Bitrix24 inválida")
    deal_ids = sorted({int(str(link)[2:]) for link in links
                       if re.fullmatch(r"D_[1-9][0-9]*", str(link))})
    if len(deal_ids) != 1:
        return {"ok": True, "skipped": "missing_or_ambiguous_deal"}
    deal_id = deal_ids[0]
    deal = (await bitrix_request("crm.deal.get", {"id": deal_id}))["result"]
    if not isinstance(deal, dict) or str(deal.get("ID")) != str(deal_id):
        raise HTTPException(502, "Negócio Bitrix24 inválido")
    description = "\n".join([
        str(deal.get("COMMENTS") or "Criado automaticamente pelo Bitrix24."), "",
        f"Negócio: https://{portal}/crm/deal/details/{deal_id}/",
        f"Tarefa Bitrix24: {task_id} — {task.get('title', '')}",
        "Responsável da tarefa: João Cruz (266)",
        *[f"{label}: {deal.get(field) or '—'}" for field, label in (
            ("STAGE_ID", "Fase"), ("OPPORTUNITY", "Valor"), ("CURRENCY_ID", "Moeda"),
            ("COMPANY_ID", "Empresa"), ("CONTACT_ID", "Contacto"),
            ("ASSIGNED_BY_ID", "Responsável do negócio"))],
    ])
    issue_input = {
        "id": task_issue_id(portal, LINEAR_TEAM_ID, task_id),
        "teamId": LINEAR_TEAM_ID, "title": deal.get("TITLE") or f"Negócio Bitrix24 #{deal_id}",
        "description": description,
    }
    if LINEAR_JOAO_ASSIGNEE_ID:
        issue_input["assigneeId"] = LINEAR_JOAO_ASSIGNEE_ID
    return await deliver_task_issue(issue_input)

@app.get("/healthz")
async def healthz():
    return {
        "ok": bool(LINEAR_API_KEY and LINEAR_TEAM_ID),
        "has_api_key": bool(LINEAR_API_KEY),
        "has_team_id": bool(LINEAR_TEAM_ID),
        "r2_configured": bool(R2_ACCESS_KEY_ID and R2_BUCKET_NAME and R2_PUBLIC_BASE_URL),
        "bitrix_configured": bool(BITRIX_WEBHOOK_BASE),
    }


@app.post("/bitrix-linear")
async def bitrix_linear(request: Request):
    """
    Webhook para receber eventos do Bitrix24 (ex.: onCrmDealAdd / onCrmDealUpdate)
    e criar issue no Linear com:
      - Título            -> FIELDS.TITLE (ou outro fallback)
      - Descrição         -> FIELDS.COMMENTS
      - Responsável       -> FIELDS.ASSIGNEE_EMAIL (procura no Linear)
      - Anexos            -> FIELDS.ATTACHMENT_FILE_IDS (lista de IDs do Disk)
    Os ficheiros são copiados para o R2 e os links são adicionados na descrição da issue.
    """
    payload = await request.json()
    data = payload.get("data") or {}
    fields = data.get("FIELDS") or {}

    # Título / descrição
    original_description = fields.get("COMMENTS") or "Criado automaticamente pelo Bitrix24."
    title = (
        fields.get("TITLE")
        or fields.get("SUBJECT")
        or payload.get("title")
        or "Item do Bitrix24"
    )
    description = original_description

    # Responsável (email do Linear)
    assignee_email = fields.get("ASSIGNEE_EMAIL")
    assignee_id = await get_user_id_by_email(assignee_email) if assignee_email else None

    # IDs de ficheiros do Bitrix (Disk) enviados pelo Bitrix
    attachment_file_ids = fields.get("ATTACHMENT_FILE_IDS") or []

    # normalizar para lista de ints
    if isinstance(attachment_file_ids, (str, int)):
        attachment_file_ids = [attachment_file_ids]

    normalized_ids: List[int] = []
    for v in attachment_file_ids:
        s = str(v).strip()
        if s.isdigit():
            normalized_ids.append(int(s))

    # 1) Criar issue no Linear
    mutation_create = """
    mutation($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id identifier title url }
      }
    }
    """

    issue_input: Dict[str, Any] = {
        "title": title,
        "description": description,
        "teamId": LINEAR_TEAM_ID,
    }
    if assignee_id:
        issue_input["assigneeId"] = assignee_id

    data_create = await linear_request(mutation_create, {"input": issue_input})
    issue_create = data_create["issueCreate"]
    issue = issue_create["issue"]
    issue_id = issue["id"]

    # 2) Copiar ficheiros do Bitrix -> R2
    created_attachments: List[str] = []

    if normalized_ids:
        for file_id in normalized_ids:
            try:
                file_bytes, content_type, filename = await download_bitrix_file(file_id)
            except Exception as e:
                print(f"[WARN] Falha ao descarregar ficheiro {file_id} do Bitrix: {e}")
                continue

            public_url = upload_to_r2(file_bytes, filename, content_type)
            created_attachments.append(public_url)

    # 3) Se houver anexos, atualizar descrição da issue com os links
    if created_attachments:
        anexos_txt = "\n".join(f"- {url}" for url in created_attachments)

        new_description = (
            f"{original_description}\n\n"
            f"---\n"
            f"Anexos (R2):\n"
            f"{anexos_txt}"
        )

        mutation_update = """
        mutation($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
            issue { id }
          }
        }
        """

        await linear_request(
            mutation_update,
            {
                "id": issue_id,
                "input": {
                    "description": new_description,
                },
            },
        )

        # também devolvemos a descrição final na resposta
        issue["description"] = new_description

    return {
        "ok": True,
        "issue": issue,
        "assigneeEmail": assignee_email,
        "assigneeId": assignee_id,
        "attachment_file_ids_received": normalized_ids,
        "attachments": created_attachments,
    }
