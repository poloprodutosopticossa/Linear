from fastapi import FastAPI, Request, HTTPException
import httpx
import os
from typing import Any, Dict, List

import boto3
from botocore.client import Config
from urllib.parse import urlparse
import os

# -------------------------------------------------
#  Configuração
# -------------------------------------------------

app = FastAPI()

# Linear
LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID")

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

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(
            502,
            detail=f"Erro na resposta do Linear (status {resp.status_code}): {resp.text[:500]}",
        )

    if data.get("errors"):
        raise HTTPException(502, detail={"linear_errors": data["errors"]})

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

@app.get("/healthz")
async def healthz():
    return {
        "ok": bool(LINEAR_API_KEY and LINEAR_TEAM_ID),
        "has_api_key": bool(LINEAR_API_KEY),
        "has_team_id": bool(LINEAR_TEAM_ID),
        "r2_configured": bool(R2_ACCESS_KEY_ID and R2_BUCKET_NAME and R2_PUBLIC_BASE_URL),
        # Bitrix_WEBHOOK_BASE já não é usado nesta versão
    }


@app.post("/bitrix-linear")
async def bitrix_linear(request: Request):
    """
    Webhook para receber eventos do Bitrix24 (ex.: onCrmDealAdd / onCrmDealUpdate)
    e criar issue no Linear com:
      - Título            -> FIELDS.TITLE (ou outro fallback)
      - Descrição         -> FIELDS.COMMENTS
      - Responsável       -> FIELDS.ASSIGNEE_EMAIL (procura no Linear)
      - Anexos            -> FIELDS.ATTACHMENT_URLS (lista de URLs para download)
    """
    payload = await request.json()
    data = payload.get("data") or {}
    fields = data.get("FIELDS") or {}

    # Título / descrição
    title = (
        fields.get("TITLE")
        or fields.get("SUBJECT")
        or payload.get("title")
        or "Item do Bitrix24"
    )
    description = fields.get("COMMENTS") or "Criado automaticamente pelo Bitrix24."

    # Responsável (email do Linear)
    assignee_email = fields.get("ASSIGNEE_EMAIL")
    assignee_id = await get_user_id_by_email(assignee_email) if assignee_email else None

    # URLs de anexos enviadas pelo Bitrix
    attachment_urls = fields.get("ATTACHMENT_URLS") or []

    # Se vier string única, transforma em lista
    if isinstance(attachment_urls, str):
        attachment_urls = [attachment_urls]

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

    # 2) Criar attachments com ficheiros do Bitrix (via R2)
    created_attachments: List[str] = []

    if attachment_urls:
        mutation_attach = """
        mutation($input: AttachmentCreateInput!) {
          attachmentCreate(input: $input) {
            success
            attachment { id title url }
          }
        }
        """

        for url in attachment_urls:
            if not url:
                continue

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp_file = await client.get(url)
                    resp_file.raise_for_status()
                    file_bytes = resp_file.content
                    content_type = resp_file.headers.get("content-type", "application/octet-stream")
            except Exception as e:
                print(f"[WARN] Falha ao descarregar anexo de {url}: {e}")
                continue

            # Derivar um nome de ficheiro simples a partir da URL (sem query string)
            parsed = urlparse(url)
            raw_name = os.path.basename(parsed.path)
            filename = raw_name or "ficheiro"

            public_url = upload_to_r2(file_bytes, filename, content_type)
            created_attachments.append(public_url)

            await linear_request(
                mutation_attach,
                {
                    "input": {
                        "issueId": issue_id,
                        "title": filename,
                        "url": public_url,
                    }
                },
            )

    return {
        "ok": True,
        "issue": issue,
        "assigneeEmail": assignee_email,
        "assigneeId": assignee_id,
        "attachment_urls_received": attachment_urls,
        "attachments": created_attachments,
    }
