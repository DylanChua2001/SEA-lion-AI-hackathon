from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from pymongo import MongoClient, ASCENDING
from hashlib import sha256
import os
from openai import OpenAI

# ---- Config ----
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("MONGODB_DB", "scraper")
COL_NAME = os.getenv("MONGODB_COLLECTION", "pages")
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))
API_KEY = os.getenv("OPENAI_API_KEY")
INGEST_KEY = os.getenv("INGEST_API_KEY")  # optional simple key
SOURCE_TAG = os.getenv("SOURCE_TAG", "chrome-extension")

client = MongoClient(MONGO_URI)
col = client[DB_NAME][COL_NAME]
oai = OpenAI(api_key=API_KEY) if API_KEY else None

# Ensure a lightweight uniqueness index (avoid dup chunks for same url+hash)
col.create_index([("url", ASCENDING), ("chunk_hash", ASCENDING)], unique=True, background=True)

app = FastAPI(title="scrape_ingest API", version="1.0.0")

# ---- Schemas ----
class Link(BaseModel):
    text: str = ""
    href: str

class PageData(BaseModel):
    url: str
    title: str = ""
    bodyText: str = ""
    links: List[Link] = Field(default_factory=list)
    images: List[str] = Field(default_factory=list)

# ---- Helpers ----
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
    if not oai:
        raise RuntimeError("OPENAI_API_KEY not set")
    res = oai.embeddings.create(model=EMBED_MODEL, input=texts)
    vecs = [d.embedding for d in res.data]
    # Atlas expects list[float]; already good
    if len(vecs[0]) != EMBED_DIM:
        raise RuntimeError(f"Embedding dim mismatch: got {len(vecs[0])}, expected {EMBED_DIM}")
    return vecs

def doc_hash(url: str, title: str, content: str) -> str:
    return sha256(f"{url}|{title}|{content}".encode("utf-8")).hexdigest()

# ---- Routes ----
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/scrape")
def scrape(page: PageData, x_api_key: Optional[str] = Header(default=None)):
    if INGEST_KEY and x_api_key != INGEST_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")

    # 1) Chunk
    chunks = chunk_text(page.bodyText) or [page.title or page.url]

    # 2) Embed
    vectors = embed_texts(chunks)

    # 3) Upsert (dedupe by url+chunk_hash)
    docs = []
    for i, (content, vec) in enumerate(zip(chunks, vectors)):
        ch = doc_hash(page.url, page.title, content)
        docs.append({
            "url": page.url,
            "title": page.title,
            "chunk_index": i,
            "chunk_hash": ch,
            "content": content,
            "links": [l.model_dump() for l in page.links][:200],
            "images": page.images[:200],
            "embedding": vec,          # <-- vector field used by Atlas vector index
            "meta": {"source": SOURCE_TAG},
        })

    # Ordered bulk upsert
    from pymongo import UpdateOne
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
