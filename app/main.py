from fastapi import FastAPI, Request, HTTPException
import httpx
import os
import boto3
from botocore.client import Config


app = FastAPI()

# ---------- Config Linear ----------
LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID")

# ---------- Config R2 (Cloudflare) ----------
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_BASE_URL = os.getenv("R2_PUBLIC_BASE_URL")  # ex: https://...r2.cloudflarestorage.com/bitrix-linear-files

if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL]):
    print("[WARN] Variáveis de ambiente do R2 não estão todas definidas.")

# Cliente S3 compatível com R2
r2_client = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    config=Config(signature_version="s3v4"),
    region_name="auto",
)


# ---------- Helpers Linear ----------

async def linear_request(query: str, variables: dict):
    """Faz um pedido à API do Linear e trata erros básicos."""
    if not LINEAR_API_KEY:
        raise HTTPException(status_code=500, detail="Falta a env LINEAR_API_KEY")
    headers = {
        # IMPORTANTE: sem 'Bearer', o Linear quer a chave crua
        "Authorization": str(LINEAR_API_KEY).strip(),
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(LINEAR_API_URL, json={"query": query, "variables": variables}, headers=headers)

    try:
        data = resp.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"Erro do Linear: status={resp.status_code}, body={resp.text[:500]}",
        )

    if data.get("errors"):
        # Passamos os erros do Linear para facilitar debug
        raise HTTPException(status_code=502, detail={"linear_errors": data["errors"]})

    return data["data"]


async def get_user_id_by_email(email: str | None) -> str | None:
    """Procura utilizador do Linear pelo email e devolve o id (ou None)."""
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
    users = data.get("users", {}).get("nodes", []) or []

    email_lower = email.lower()
    for u in users:
        if (u.get("email") or "").lower() == email_lower:
            return u.get("id")

    return None


# ---------- Helper R2 ----------

def upload_to_r2(file_bytes: bytes, filename: str) -> str:
    """
    Envia um ficheiro para o bucket R2 e devolve o URL público.
    """
    if not all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME, R2_PUBLIC_BASE_URL]):
        raise HTTPException(status_code=500, detail="Configuração R2 incompleta nas variáveis de ambiente.")

    # podes ajustar o caminho se quiseres outra estrutura
    key = f"attachments/{filename}"

    r2_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType="application/octet-stream",
    )

    # URL público final
    return f"{R2_PUBLIC_BASE_URL.rstrip('/')}/{key}"


# ---------- Endpoints ----------

@app.get("/healthz")
async def healthz():
    return {
        "ok": bool(LINEAR_API_KEY and LINEAR_TEAM_ID),
        "has_api_key": bool(LINEAR_API_KEY),
        "has_team_id": bool(LINEAR_TEAM_ID),
        "r2_configured": bool(R2_ACCESS_KEY_ID and R2_BUCKET_NAME),
    }


@app.post("/bitrix-linear")
async def bitrix_linear(request: Request):
    """
    Recebe payload do Bitrix24 e cria uma issue no Linear com:
    - título (TITLE)
    - descrição (COMMENTS)
    - responsável (ASSIGNEE_EMAIL -> assigneeId)
    - anexos:
        ATTACHMENT_URLS = lista de URLs (idealmente do Bitrix) que fazemos download
        e reenviamos para o R2, usando depois o URL público como attachment no Linear.
    """
    payload = await request.json()
    fields = (payload or {}).get("data", {}).get("FIELDS", {}) or {}

    # Título / descrição
    title = (
        fields.get("TITLE")
        or fields.get("SUBJECT")
        or payload.get("title")
        or "Item do Bitrix24"
    )
    description = fields.get("COMMENTS") or "Criado automaticamente pelo webhook do Bitrix24."

    # Campos extra do Bitrix (tens de os enviar no webhook)
    assignee_email = fields.get("ASSIGNEE_EMAIL")  # ex.: "user@empresa.pt"
    attachment_urls = fields.get("ATTACHMENT_URLS") or []  # lista de URLs (Bitrix, Drive, etc.)

    # 1) Resolver assigneeId
    assignee_id = await get_user_id_by_email(assignee_email) if assignee_email else None

    # 2) Criar issue no Linear
    mutation_create = """
    mutation($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id title url identifier }
      }
    }
    """

    issue_input: dict = {
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

    # 3) Fazer upload dos anexos para o R2 e criar attachments no Linear
    created_attachments: list[str] = []

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

            # Download simples; se o Bitrix exigir auth, aqui terás de adicionar headers com token
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp_file = await client.get(url)
                resp_file.raise_for_status()
                file_bytes = resp_file.content

            filename = url.split("/")[-1] or "ficheiro"

            public_url = upload_to_r2(file_bytes, filename)
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
        "attachments": created_attachments,
    }
