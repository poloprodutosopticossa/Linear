from fastapi import FastAPI, Request, HTTPException
import httpx
import os

app = FastAPI()

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID")


# ---------- Helpers ----------

async def linear_request(query: str, variables: dict):
    """
    Faz um pedido à API do Linear com a API key das env vars.
    Lança HTTPException se o Linear devolver erros.
    """
    if not LINEAR_API_KEY:
        raise HTTPException(status_code=500, detail="Falta a env LINEAR_API_KEY")
    if not LINEAR_TEAM_ID:
        raise HTTPException(status_code=500, detail="Falta a env LINEAR_TEAM_ID")

    headers = {
        # IMPORTANTE: Linear quer a API key crua, sem 'Bearer'
        "Authorization": str(LINEAR_API_KEY).strip(),
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            LINEAR_API_URL,
            json={"query": query, "variables": variables},
            headers=headers,
        )

    try:
        data = r.json()
    except Exception:
        raise HTTPException(
            status_code=502,
            detail=f"Erro do Linear: status={r.status_code}, body={r.text[:500]}",
        )

    if data.get("errors"):
        # Deixar bem visível o erro do Linear
        raise HTTPException(status_code=502, detail={"linear_errors": data["errors"]})

    return data["data"]


async def get_user_id_by_email(email: str | None) -> str | None:
    """
    Procura o utilizador do Linear pelo email e devolve o id.
    Se não encontrar, devolve None.
    """
    if not email:
        return None

    query = """
    {
      users(first: 200) {
        nodes {
          id
          email
        }
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


# ---------- Endpoints ----------

@app.get("/healthz")
async def healthz():
    ok = bool(LINEAR_API_KEY and LINEAR_TEAM_ID)
    return {
        "ok": ok,
        "has_api_key": bool(LINEAR_API_KEY),
        "has_team_id": bool(LINEAR_TEAM_ID),
    }


@app.post("/bitrix-linear")
async def bitrix_linear(request: Request):
    """
    Recebe payload do Bitrix24 e cria uma issue no Linear com:
    - título (TITLE)
    - descrição (COMMENTS)
    - assignee (ASSIGNEE_EMAIL -> assigneeId)
    - anexos (ATTACHMENT_URLS -> attachmentCreate)
    """
    payload = await request.json()
    fields = (payload or {}).get("data", {}).get("FIELDS", {}) or {}

    # Título e descrição base
    title = (
        fields.get("TITLE")
        or fields.get("SUBJECT")
        or payload.get("title")
        or "Item do Bitrix24"
    )
    description = fields.get("COMMENTS") or "Criado automaticamente pelo webhook do Bitrix24."

    # Campos extra vindos do Bitrix24 (tu controlas isto no webhook/automação)
    assignee_email = fields.get("ASSIGNEE_EMAIL")  # ex.: "alguem@empresa.pt"
    attachment_urls = fields.get("ATTACHMENT_URLS") or []  # lista de URLs de ficheiros

    # 1) Resolver assigneeId a partir do email (se houver)
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

    # 3) Criar anexos se existirem URLs
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
                await linear_request(
                    mutation_attach,
                    {
                        "input": {
                            "issueId": issue_id,
                            "title": f"Anexo: {url.split('/')[-1]}",
                            "url": url,
                        }
                    },
                )
            except Exception:
                # Não queremos falhar a criação da issue só por causa de um anexo
                pass

    return {
        "ok": True,
        "issue": issue,
        "assigneeEmail": assignee_email,
        "assigneeId": assignee_id,
        "attachmentsCount": len(attachment_urls),
    }

