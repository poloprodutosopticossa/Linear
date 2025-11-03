from fastapi import FastAPI, Request, HTTPException
import httpx
import os

app = FastAPI()

LINEAR_API_URL = "https://api.linear.app/graphql"
LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
LINEAR_TEAM_ID = os.getenv("LINEAR_TEAM_ID")  # Define no Render Dashboard

@app.get("/healthz")
async def healthz():
    ok = bool(LINEAR_API_KEY and LINEAR_TEAM_ID)
    return {"ok": ok, "has_api_key": bool(LINEAR_API_KEY), "has_team_id": bool(LINEAR_TEAM_ID)}

@app.post("/bitrix-linear")
async def bitrix_linear(request: Request):
    payload = await request.json()
    fields = (payload or {}).get("data", {}).get("FIELDS", {})
    title = fields.get("TITLE") or fields.get("SUBJECT") or payload.get("title") or "Item do Bitrix24"
    description = fields.get("COMMENTS") or "Criado automaticamente pelo webhook do Bitrix24."

    mutation = """
    mutation($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id title url identifier }
      }
    }
    """

    variables = {"input": {"title": title, "description": description, "teamId": LINEAR_TEAM_ID}}
    headers = {"Authorization": f"Bearer {LINEAR_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(LINEAR_API_URL, json={"query": mutation, "variables": variables}, headers=headers)
    return r.json()
