from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from pymongo import MongoClient, ASCENDING, UpdateOne
from hashlib import sha256
from urllib.parse import urlparse
from datetime import datetime, timezone
import os
import httpx

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="scrape_ingest API", version="1.0.0")

# dev-friendly; lock down origins in prod
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],              # TEMP: open everything
    allow_credentials=False,          # must be False when using "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGODB_DB", "scraper")
COL_NAME = os.getenv("MONGODB_COLLECTION", "pages")

SEA_LION_API_KEY = os.getenv("SEA_LION_API_KEY")
SEA_LION_BASE_URL = os.getenv("SEA_LION_BASE_URL", "https://api.sea-lion.ai/v1")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))  # must match your Atlas index

INGEST_KEY = os.getenv("INGEST_API_KEY")  # optional simple auth header
SOURCE_TAG = os.getenv("SOURCE_TAG", "chrome-extension")

# ── Clients ───────────────────────────────────────────────────────────────────
client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=8000,
    connectTimeoutMS=8000,
    socketTimeoutMS=20000,
    maxPoolSize=20,
)
col = client[DB_NAME][COL_NAME]

# one reusable HTTP client
_http = httpx.Client(timeout=20.0)

# Unique index to avoid duplicate chunks for same url+content
col.create_index([("url", ASCENDING), ("chunk_hash", ASCENDING)], unique=True, background=True)

# Schemas
class Link(BaseModel):
    text: str = ""
    href: str

class PageData(BaseModel):
    url: str
    title: str = ""
    bodyText: str = ""
    links: List[Link] = Field(default_factory=list)
    images: List[str] = Field(default_factory=list)

class SearchRequest(BaseModel):
    query: str
    k: int = 5
    filter: Dict[str, Any] | None = None  # e.g. {"domain":"example.com"}

# Helpers
def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    out, i, n = [], 0, len(text)
    while i < n:
        j = min(n, i + max_chars)
        out.append(text[i:j].strip())
        i = max(0, j - overlap)
        if i == j:  # safety
            break
    return [c for c in out if c]

def embed_texts(texts: List[str]) -> List[List[float]]:
    if not SEA_LION_API_KEY:
        raise HTTPException(status_code=500, detail="SEA_LION_API_KEY not set for embeddings")
    try:
        r = _http.post(
            f"{SEA_LION_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {SEA_LION_API_KEY}"},
            json={"model": EMBED_MODEL, "input": texts},
        )
        r.raise_for_status()
        payload = r.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"embedding error: {e.response.text}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"embedding error: {e}")

    try:
        vecs = [item["embedding"] for item in payload["data"]]
    except Exception:
        raise HTTPException(status_code=500, detail=f"unexpected embedding response: {payload}")

    if not vecs or len(vecs[0]) != EMBED_DIM:
        got = len(vecs[0]) if vecs else 0
        raise HTTPException(status_code=500, detail=f"Embedding dim mismatch: got {got}, expected {EMBED_DIM}")

    return vecs

def doc_hash(url: str, title: str, content: str) -> str:
    return sha256(f"{url}|{title}|{content}".encode("utf-8")).hexdigest()

# Routes
@app.get("/health")
def health():
    client.admin.command("ping")
    return {"ok": True}

@app.post("/scrape")
def scrape(page: PageData, x_api_key: Optional[str] = Header(default=None)):
    if INGEST_KEY and x_api_key != INGEST_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

    # 1) chunk
    chunks = chunk_text(page.bodyText) or [page.title or page.url]

    # 2) embed
    vectors = embed_texts(chunks)

    # 3) upsert
    domain = urlparse(page.url).netloc
    now = datetime.now(timezone.utc).isoformat()

    docs = []
    for i, (content, vec) in enumerate(zip(chunks, vectors)):
        ch = doc_hash(page.url, page.title, content)
        docs.append({
            "url": page.url,
            "domain": domain,
            "title": page.title,
            "chunk_index": i,
            "chunk_hash": ch,
            "content": content,
            "links": [l.model_dump() for l in page.links][:200],
            "images": page.images[:200],
            "embedding": vec,  # vector field used by Atlas Vector Search
            "meta": {"source": SOURCE_TAG},
            "created_at": now,
        })

    ops = [
        UpdateOne({"url": d["url"], "chunk_hash": d["chunk_hash"]}, {"$set": d}, upsert=True)
        for d in docs
    ]
    result = col.bulk_write(ops, ordered=False)

    return {
        "ok": True,
        "matched": result.matched_count,
        "modified": result.modified_count,
        "upserted": len(result.upserted_ids or {}),
        "total_chunks": len(docs),
    }

@app.post("/search")
def search(req: SearchRequest):
    qv = embed_texts([req.query])[0]
    pipeline: List[Dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": os.getenv("VECTOR_INDEX_NAME", "vector_index"),
                "path": "embedding",
                "queryVector": qv,
                "numCandidates": 200,
                "limit": max(1, min(req.k, 20)),
                **({"filter": req.filter} if req.filter else {})
            }
        },
        {
            "$project": {
                "_id": 0,
                "url": 1,
                "domain": 1,
                "title": 1,
                "chunk_index": 1,
                "content": 1,
                "score": {"$meta": "vectorSearchScore"},
            }
        }
    ]
    results = list(col.aggregate(pipeline))
    return {"matches": results}
