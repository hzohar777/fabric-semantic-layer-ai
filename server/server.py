"""
TeraSky Fabric Semantic Layer AI — Remote MCP Server
=====================================================
A FastMCP server that connects Claude AI to a Microsoft Fabric semantic layer
via three structured tools: schema retrieval (RAG), DAX execution, and dataset discovery.

Architecture:
  Claude AI → Remote MCP Server (this file) → Azure AI Search (schema RAG)
                                             → Power BI REST API (DAX execution)
  Auth: OAuth 2.0 + Azure Entra ID + OBO flow + JWT validation per request

Author: TeraSky — https://www.terasky.com
"""

import os
import json
import logging
import httpx
import jwt
from jwt import PyJWKClient
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Configuration (from environment) ─────────────────────────────────────────
AZURE_TENANT_ID        = os.environ["AZURE_TENANT_ID"]
AZURE_CLIENT_ID        = os.environ["AZURE_CLIENT_ID"]
AZURE_CLIENT_SECRET    = os.environ["AZURE_CLIENT_SECRET"]

SEARCH_ENDPOINT        = os.environ["AZURE_SEARCH_ENDPOINT"]      # e.g. https://xxx.search.windows.net
SEARCH_API_KEY         = os.environ["AZURE_SEARCH_API_KEY"]
SEARCH_INDEX_NAME      = os.environ.get("AZURE_SEARCH_INDEX_NAME", "fabric-schema")

OPENAI_ENDPOINT        = os.environ["AZURE_OPENAI_ENDPOINT"]
OPENAI_API_KEY         = os.environ["AZURE_OPENAI_API_KEY"]
OPENAI_EMBEDDING_MODEL = os.environ.get("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

POWERBI_ACCESS_TOKEN   = os.environ.get("POWERBI_ACCESS_TOKEN", "")  # fallback token (PoC)
POWERBI_API_BASE       = "https://api.powerbi.com/v1.0/myorg"

JWKS_URI = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/discovery/v2.0/keys"
jwks_client = PyJWKClient(JWKS_URI, cache_keys=True, lifespan=3600)

# ── JWT Validation Middleware ─────────────────────────────────────────────────
class JWTAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "Missing Bearer token"}, status_code=401)
        token = auth.split(" ", 1)[1]
        try:
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=AZURE_CLIENT_ID,
                issuer=f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/v2.0",
            )
        except Exception as e:
            log.warning("JWT validation failed: %s", e)
            return JSONResponse({"error": "Invalid or expired token"}, status_code=401)
        request.state.bearer_token = token
        return await call_next(request)

# ── FastMCP Server ────────────────────────────────────────────────────────────
mcp = FastMCP(
    name="Fabric Semantic Layer AI",
    description="Natural language access to Microsoft Fabric semantic models via RAG + DAX.",
)
mcp.add_middleware(JWTAuthMiddleware)

# ── Helper: Get embedding vector ──────────────────────────────────────────────
async def get_embedding(text: str) -> list[float]:
    url = f"{OPENAI_ENDPOINT}/openai/deployments/{OPENAI_EMBEDDING_MODEL}/embeddings?api-version=2024-02-01"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            headers={"api-key": OPENAI_API_KEY, "Content-Type": "application/json"},
            json={"input": text},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

# ── Helper: Get Power BI token ────────────────────────────────────────────────
async def get_powerbi_token(user_bearer_token: str | None = None) -> str:
    """Returns OBO token if user token is provided, else falls back to env token."""
    if user_bearer_token and user_bearer_token != POWERBI_ACCESS_TOKEN:
        try:
            url = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "client_id": AZURE_CLIENT_ID,
                    "client_secret": AZURE_CLIENT_SECRET,
                    "assertion": user_bearer_token,
                    "scope": "https://analysis.windows.net/powerbi/api/.default",
                    "requested_token_use": "on_behalf_of",
                })
                if resp.status_code == 200:
                    return resp.json()["access_token"]
        except Exception as e:
            log.warning("OBO flow failed, falling back to env token: %s", e)
    return POWERBI_ACCESS_TOKEN

# ── Tool 1: search_schema ─────────────────────────────────────────────────────
@mcp.tool()
async def search_schema(query: str) -> str:
    """
    Retrieve relevant schema context from the Fabric semantic model using vector search.

    Call this FIRST before generating any DAX query. It returns the exact table names,
    column names, measure definitions, and DAX patterns needed to write accurate queries.

    Args:
        query: The user's natural language question or topic to search schema for.

    Returns:
        Relevant schema chunks: table names, column definitions, measure expressions.
    """
    log.info("search_schema called: %s", query[:80])
    try:
        vector = await get_embedding(query)
    except Exception as e:
        return f"[search_schema error - embedding failed: {e}]"

    search_url = f"{SEARCH_ENDPOINT}/indexes/{SEARCH_INDEX_NAME}/docs/search?api-version=2024-05-01-preview"
    payload = {
        "search": query,
        "vectorQueries": [{
            "kind": "vector",
            "vector": vector,
            "fields": "content_vector",
            "k": 8,
        }],
        "queryType": "semantic",
        "semanticConfiguration": "default",
        "select": "content,title,chunk_type",
        "top": 8,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            search_url,
            headers={"api-key": SEARCH_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        resp.raise_for_status()
        results = resp.json().get("value", [])

    if not results:
        return "No schema context found. The index may be empty or the query returned no matches."

    chunks = []
    for r in results:
        title = r.get("title", "")
        content = r.get("content", "")
        chunks.append(f"### {title}\n{content}")

    return "\n\n---\n\n".join(chunks)


# ── Tool 2: list_datasets ─────────────────────────────────────────────────────
@mcp.tool()
async def list_datasets(workspace_id: str = "") -> str:
    """
    List available Power BI datasets in the Fabric workspace.

    Use this for discovery — to find the dataset ID required for execute_dax.

    Args:
        workspace_id: Optional. The Power BI workspace (group) ID. Omit to list
                      datasets across all accessible workspaces.

    Returns:
        JSON list of datasets with id, name, and workspace.
    """
    log.info("list_datasets called (workspace_id=%s)", workspace_id or "all")
    token = POWERBI_ACCESS_TOKEN
    if not token:
        return "Error: No Power BI access token configured."

    url = (
        f"{POWERBI_API_BASE}/groups/{workspace_id}/datasets"
        if workspace_id
        else f"{POWERBI_API_BASE}/datasets"
    )
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code != 200:
            return f"Power BI API error {resp.status_code}: {resp.text}"
        datasets = resp.json().get("value", [])

    if not datasets:
        return "No datasets found. Check token permissions or workspace access."

    result = [
        {"id": d["id"], "name": d["name"], "configuredBy": d.get("configuredBy", "")}
        for d in datasets
    ]
    return json.dumps(result, indent=2)


# ── Tool 3: execute_dax ───────────────────────────────────────────────────────
@mcp.tool()
async def execute_dax(
    dataset_id: str,
    dax_query: str,
    workspace_id: str = "",
) -> str:
    """
    Execute a DAX query against a Power BI / Fabric semantic model.

    IMPORTANT: Always call search_schema first to get exact column and measure names.
    DAX queries must use the exact names returned by search_schema — do not guess.

    Args:
        dataset_id:   The Power BI dataset GUID (from list_datasets).
        dax_query:    A valid DAX query string (e.g. EVALUATE TOPN(...)).
        workspace_id: Optional. The Power BI workspace (group) ID.

    Returns:
        Query results as formatted JSON, or an error message.
    """
    log.info("execute_dax called: dataset=%s, query=%s", dataset_id, dax_query[:80])
    token = POWERBI_ACCESS_TOKEN
    if not token:
        return "Error: No Power BI access token configured."

    url = (
        f"{POWERBI_API_BASE}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
        if workspace_id
        else f"{POWERBI_API_BASE}/datasets/{dataset_id}/executeQueries"
    )
    payload = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            return f"Power BI API error {resp.status_code}: {resp.text}"

        data = resp.json()

    if "error" in data:
        err = data["error"]
        code = err.get("code", "")
        msg = err.get("pbi.error", {}).get("details", [{}])[0].get("detail", {}).get("value", err.get("message", ""))
        return f"DAX error [{code}]: {msg}"

    try:
        rows = data["results"][0]["tables"][0].get("rows", [])
    except (KeyError, IndexError):
        return "Query executed but returned no rows."

    if not rows:
        return "Query returned 0 rows."

    return json.dumps(rows, indent=2, ensure_ascii=False)


# ── Health endpoint ───────────────────────────────────────────────────────────
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"status": "ok", "server": "fabric-semantic-layer-ai"})


# ── Entrypoint ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Fabric Semantic Layer AI MCP Server on port %d", port)
    uvicorn.run(mcp.http_app(), host="0.0.0.0", port=port)
