"""
index_schema.py — Index Fabric Semantic Model Schema into Azure AI Search
=========================================================================
Reads a schema definition file (JSON or YAML), chunks it into searchable
documents, generates embeddings via Azure OpenAI, and uploads them to an
Azure AI Search index.

Usage:
    python index_schema.py --schema schema.json
    python index_schema.py --schema schema.json --recreate-index

Schema file format (JSON):
    {
      "tables": [
        {
          "name": "financials",
          "description": "Transaction-level financial data",
          "columns": [
            {"name": "Country", "type": "text", "description": "Country of the transaction"},
            {"name": " Sales",  "type": "decimal", "description": "Net sales amount (note: leading space in column name)"}
          ],
          "measures": [
            {"name": "Net Sales", "expression": "SUM(financials[ Sales])", "description": "Total net sales"}
          ]
        }
      ],
      "dax_patterns": [
        {
          "title": "Top N by measure",
          "description": "Return the top N rows by a measure value",
          "dax": "EVALUATE TOPN(N, SUMMARIZECOLUMNS(Table[Column], \"Measure\", [MeasureName]), [Measure], DESC)"
        }
      ]
    }
"""

import os
import json
import argparse
import hashlib
import httpx

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_ENDPOINT    = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_API_KEY     = os.environ["AZURE_SEARCH_API_KEY"]
INDEX_NAME         = os.environ.get("AZURE_SEARCH_INDEX_NAME", "fabric-schema")
OPENAI_ENDPOINT    = os.environ["AZURE_OPENAI_ENDPOINT"]
OPENAI_API_KEY     = os.environ["AZURE_OPENAI_API_KEY"]
EMBEDDING_MODEL    = os.environ.get("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMS     = 1536

# ── Embedding ──────────────────────────────────────────────────────────────────
def get_embedding(text: str) -> list[float]:
    url = f"{OPENAI_ENDPOINT}/openai/deployments/{EMBEDDING_MODEL}/embeddings?api-version=2024-02-01"
    resp = httpx.post(
        url,
        headers={"api-key": OPENAI_API_KEY, "Content-Type": "application/json"},
        json={"input": text},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]

# ── Index management ──────────────────────────────────────────────────────────
def create_index():
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}?api-version=2024-05-01-preview"
    schema = {
        "name": INDEX_NAME,
        "fields": [
            {"name": "id",             "type": "Edm.String", "key": True, "filterable": True},
            {"name": "title",          "type": "Edm.String", "searchable": True},
            {"name": "chunk_type",     "type": "Edm.String", "filterable": True},
            {"name": "content",        "type": "Edm.String", "searchable": True, "analyzer": "en.microsoft"},
            {"name": "content_vector", "type": "Collection(Edm.Single)",
             "searchable": True, "dimensions": EMBEDDING_DIMS, "vectorSearchProfile": "default-profile"},
        ],
        "vectorSearch": {
            "algorithms": [{"name": "default-hnsw", "kind": "hnsw"}],
            "profiles": [{"name": "default-profile", "algorithm": "default-hnsw"}],
        },
        "semantic": {
            "configurations": [{
                "name": "default",
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "contentFields": [{"fieldName": "content"}],
                },
            }]
        },
    }
    resp = httpx.put(url, headers={"api-key": SEARCH_API_KEY}, json=schema, timeout=30)
    resp.raise_for_status()
    print(f"✓ Index '{INDEX_NAME}' created/updated.")

def delete_index():
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}?api-version=2024-05-01-preview"
    resp = httpx.delete(url, headers={"api-key": SEARCH_API_KEY}, timeout=15)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()
    print(f"✓ Index '{INDEX_NAME}' deleted (if it existed).")

# ── Chunking ──────────────────────────────────────────────────────────────────
def chunk_schema(schema: dict) -> list[dict]:
    """Convert schema dict into searchable text chunks."""
    chunks = []

    for table in schema.get("tables", []):
        # One chunk per table (overview)
        tname = table["name"]
        tdesc = table.get("description", "")
        cols = "\n".join(
            f"  - {c['name']} ({c.get('type','')}) : {c.get('description','')}"
            for c in table.get("columns", [])
        )
        content = f"Table: {tname}\n{tdesc}\n\nColumns:\n{cols}"
        chunks.append({"title": f"Table: {tname}", "chunk_type": "table", "content": content})

        # One chunk per measure
        for m in table.get("measures", []):
            expr = m.get("expression", "")
            mdesc = m.get("description", "")
            content = (
                f"Measure: [{m['name']}] in table {tname}\n"
                f"Description: {mdesc}\n"
                f"DAX Expression: {expr}"
            )
            chunks.append({"title": f"Measure: {m['name']}", "chunk_type": "measure", "content": content})

    for pattern in schema.get("dax_patterns", []):
        content = (
            f"DAX Pattern: {pattern['title']}\n"
            f"Description: {pattern.get('description','')}\n\n"
            f"Example:\n{pattern.get('dax','')}"
        )
        chunks.append({"title": pattern["title"], "chunk_type": "dax_pattern", "content": content})

    return chunks

# ── Upload ────────────────────────────────────────────────────────────────────
def upload_chunks(chunks: list[dict]):
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/index?api-version=2024-05-01-preview"
    docs = []
    for i, chunk in enumerate(chunks):
        print(f"  [{i+1}/{len(chunks)}] Embedding: {chunk['title']}")
        vector = get_embedding(chunk["content"])
        doc_id = hashlib.md5(chunk["content"].encode()).hexdigest()
        docs.append({
            "@search.action": "mergeOrUpload",
            "id": doc_id,
            "title": chunk["title"],
            "chunk_type": chunk["chunk_type"],
            "content": chunk["content"],
            "content_vector": vector,
        })

    batch_size = 10
    for i in range(0, len(docs), batch_size):
        batch = docs[i:i+batch_size]
        resp = httpx.post(
            url,
            headers={"api-key": SEARCH_API_KEY, "Content-Type": "application/json"},
            json={"value": batch},
            timeout=60,
        )
        resp.raise_for_status()
    print(f"✓ Uploaded {len(docs)} chunks to '{INDEX_NAME}'.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Index Fabric schema into Azure AI Search.")
    parser.add_argument("--schema", required=True, help="Path to schema JSON file.")
    parser.add_argument("--recreate-index", action="store_true", help="Delete and recreate the index before uploading.")
    args = parser.parse_args()

    with open(args.schema) as f:
        schema = json.load(f)

    if args.recreate_index:
        delete_index()

    create_index()

    chunks = chunk_schema(schema)
    print(f"Generated {len(chunks)} chunks from schema.")
    upload_chunks(chunks)
    print("✓ Schema indexing complete.")

if __name__ == "__main__":
    main()
