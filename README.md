# Fabric Semantic Layer AI

> **Natural language access to Microsoft Fabric semantic models via Claude AI, Remote MCP, and Agentic RAG.**

[![TeraSky](https://img.shields.io/badge/by-TeraSky-f07a57?style=flat-square)](https://www.terasky.com)
[![Azure](https://img.shields.io/badge/Azure-Container_Apps-0078d4?style=flat-square&logo=microsoft-azure)](https://azure.microsoft.com)
[![MCP](https://img.shields.io/badge/protocol-MCP-7c3aed?style=flat-square)](https://modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

---

## What This Is

A cloud-hosted **Remote MCP Server** that bridges Claude AI (or any MCP-compatible client) to a Microsoft Fabric semantic layer. Business users ask questions in plain English; the system retrieves exact schema context via Agentic RAG, generates accurate DAX, and returns live results — all within the customer's Azure tenant and under their existing governance policies.

**No DAX required. No data replication. No schema changes.**

---

## Architecture

```
                        ┌─────────────────────────────────────────┐
                        │           Microsoft Azure Tenant         │
                        │                                          │
  ┌──────────────┐      │  ┌──────────────────────────────────┐   │
  │  Claude AI   │      │  │     Remote MCP Server            │   │
  │  claude.ai   │─────────│  Azure Container Apps            │   │
  │              │  MCP │  │  Python · FastMCP · HTTPS        │   │
  └──────────────┘      │  │                                  │   │
         │              │  │  ① search_schema  ──────────────────────► Azure AI Search
         │              │  │  ② execute_dax    ──────────────────────► Power BI REST API
         │              │  │  ③ list_datasets  ──────────────────────► Power BI REST API
         │              │  └──────────────────────────────────┘   │
         │              │                                          │
         │ OAuth 2.0    │  ┌──────────────┐  ┌────────────────┐   │
         └─────────────────► Azure Entra  │  │ Azure OpenAI   │   │
                        │  │ ID (JWT)     │  │ text-embedding │   │
                        │  └──────────────┘  └────────────────┘   │
                        │                                          │
                        │  ┌──────────────────────────────────┐   │
                        │  │  Microsoft Fabric Semantic Model  │   │
                        │  │  KPIs · Measures · Hierarchies   │   │
                        │  │  (used as-is, zero changes)      │   │
                        │  └──────────────────────────────────┘   │
                        └─────────────────────────────────────────┘
```

### Query Flow

| Step | What Happens |
|------|-------------|
| **1. User asks** | Natural language question typed in Claude (no DAX) |
| **2. Claude calls MCP** | Routes request to Remote MCP Server via HTTPS + Bearer token |
| **3. Schema RAG** | `search_schema` embeds the question → hybrid vector search → returns exact column names, measures, DAX patterns |
| **4. DAX generation** | Claude generates accurate DAX grounded in retrieved schema |
| **5. Execution** | `execute_dax` posts DAX to Power BI REST API → Fabric semantic model runs it |
| **6. Answer** | Claude formats results in plain English and returns to user |

---

## Components

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **MCP Server** | Python · FastMCP · Azure Container Apps | Exposes tools to Claude, JWT validation |
| **Schema RAG** | Azure AI Search · Azure OpenAI | Indexes Fabric schema, retrieves context before each query |
| **DAX Execution** | Power BI REST API (`executeQueries`) | Runs generated DAX against live Fabric semantic model |
| **Authentication** | Azure Entra ID · OAuth 2.0 · PKCE · OBO | Per-request JWT validation, delegated user permissions |
| **Container Registry** | Azure Container Registry | Stores and serves the MCP server Docker image |

---

## Quick Start

### Prerequisites

- Azure subscription with permissions to create Container Apps, AI Search, OpenAI
- Microsoft Fabric workspace with a published semantic model (Power BI dataset)
- Azure Entra ID app registration (see [docs/setup-guide.md](docs/setup-guide.md))
- Docker + Azure CLI

### 1. Deploy infrastructure

```bash
az group create --name rg-fabric-semantic-layer-ai --location westeurope

az deployment group create \
  --resource-group rg-fabric-semantic-layer-ai \
  --template-file infra/bicep/main.bicep \
  --parameters prefix=fabric-ai \
               tenantId=<your-tenant-id> \
               clientId=<your-app-client-id> \
               clientSecret=<your-app-client-secret> \
               mcpServerImage=<acr-login-server>/fabric-mcp-server:latest
```

### 2. Build and push the server

```bash
ACR=$(az deployment group show \
  --resource-group rg-fabric-semantic-layer-ai \
  --name main --query "properties.outputs.acrLoginServer.value" -o tsv)

az acr login --name $ACR
docker build -t $ACR/fabric-mcp-server:latest ./server
docker push $ACR/fabric-mcp-server:latest
```

### 3. Index your Fabric schema

Create a `schema.json` describing your semantic model (see `scripts/index_schema.py` for the format), then:

```bash
export AZURE_SEARCH_ENDPOINT=<from-deployment-output>
export AZURE_SEARCH_API_KEY=<your-search-admin-key>
export AZURE_OPENAI_ENDPOINT=<from-deployment-output>
export AZURE_OPENAI_API_KEY=<your-openai-key>

python scripts/index_schema.py --schema schema.json --recreate-index
```

### 4. Connect Claude

In Claude.ai → Settings → Connectors → Add custom connector:

```
Name: Fabric Semantic Layer
URL:  https://<your-aca-fqdn>/mcp
Auth: OAuth 2.0
```

Use the Entra ID app registration details (see [docs/setup-guide.md](docs/setup-guide.md)).

---

## MCP Tools

### `search_schema(query)`
Retrieves relevant schema context using hybrid vector + keyword search against the indexed Fabric semantic model. **Always call this before generating DAX.**

### `execute_dax(dataset_id, dax_query, workspace_id?)`
Executes a DAX query against a live Fabric semantic model via the Power BI REST API. Returns results as JSON.

### `list_datasets(workspace_id?)`
Lists available Power BI datasets the authenticated user has access to. Use for discovery.

---

## Security

- **OAuth 2.0 + PKCE** — users authenticate via Azure Entra ID
- **JWT validation** — every MCP request is validated against Azure AD JWKS
- **On-Behalf-Of (OBO) flow** — queries run under the signed-in user's identity
- **Row-Level Security** — automatically enforced by the Fabric semantic model
- **No standing credentials** — no shared service accounts or stored passwords
- **Tenant-bound** — all traffic stays within the customer's Azure tenant

See [docs/security.md](docs/security.md) for full details.

---

## Token Refresh (PoC mode)

In PoC mode, a pre-loaded Power BI access token is used. This token expires hourly. Refresh it with:

```bash
./scripts/refresh_token.sh
# or
RESOURCE_GROUP=my-rg APP_NAME=my-server ./scripts/refresh_token.sh
```

For production, configure the OBO flow so tokens are issued per signed-in user automatically.

---

## Repository Structure

```
fabric-semantic-layer-ai/
├── server/
│   ├── server.py           # FastMCP server (search_schema, execute_dax, list_datasets)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
├── scripts/
│   ├── index_schema.py     # Index Fabric schema into Azure AI Search
│   └── refresh_token.sh    # Refresh Power BI token on Azure Container Apps
├── infra/
│   └── bicep/
│       └── main.bicep      # Full Azure infrastructure (ACA, AI Search, OpenAI, ACR)
├── docs/
│   ├── setup-guide.md      # Step-by-step deployment guide
│   ├── security.md         # Authentication and security details
│   └── architecture.md     # Architecture deep-dive
├── .github/
│   └── workflows/
│       └── deploy.yml      # Build + deploy CI/CD pipeline
└── README.md
```

---

## Documentation

- [Setup Guide](docs/setup-guide.md) — end-to-end deployment walkthrough
- [Security](docs/security.md) — OAuth 2.0, JWT, OBO flow, RLS
- [Architecture](docs/architecture.md) — component deep-dive, design decisions

---

## About TeraSky

[TeraSky](https://www.terasky.com) is a Microsoft strategic partner specializing in Azure, AI, and data engineering. This project is part of the **TeraSky Fabric AI™** offering — a repeatable solution for connecting AI assistants to Microsoft Fabric semantic layers.

For inquiries: [info@terasky.com](mailto:info@terasky.com)
