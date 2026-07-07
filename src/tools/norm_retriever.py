"""团队规范知识库检索（ES 父子分块 + DashScope 向量检索）。

索引结构：
  team-norms  — 子块（≈150 token），含 dense_vector embedding + parent_id
  parent_doc  — 父块（≈800 token），含完整上下文正文
"""

import logging
from typing import Any

import httpx
from elasticsearch import AsyncElasticsearch

from src.config import settings

logger = logging.getLogger(__name__)

_es_client: AsyncElasticsearch | None = None

# ES 索引名
IDX_CHILD  = "team-norms"
IDX_PARENT = "parent_doc"


def get_es_client() -> AsyncElasticsearch:
    global _es_client
    if _es_client is None:
        _es_client = AsyncElasticsearch(settings.ES_URL, request_timeout=30)
    return _es_client


# ── ES 索引 DDL ──────────────────────────────────────────────────────────────

_CHILD_MAPPING = {
    "mappings": {
        "properties": {
            "parent_id":   {"type": "keyword"},
            "content":     {"type": "text", "analyzer": "standard"},
            "embedding":   {"type": "dense_vector", "dims": settings.EMBEDDING_DIMS,
                            "index": True, "similarity": "cosine"},
            "source_file": {"type": "keyword"},
            "chunk_index": {"type": "integer"},
        }
    }
}

_PARENT_MAPPING = {
    "mappings": {
        "properties": {
            "content":      {"type": "text"},
            "source_file":  {"type": "keyword"},
            "parent_index": {"type": "integer"},
        }
    }
}


async def ensure_indices() -> None:
    """确保 ES 索引存在，不存在则创建（幂等）。"""
    es = get_es_client()
    for idx, mapping in ((IDX_CHILD, _CHILD_MAPPING), (IDX_PARENT, _PARENT_MAPPING)):
        exists = await es.indices.exists(index=idx)
        if not exists:
            await es.indices.create(index=idx, body=mapping)
            logger.info("ES index created: %s", idx)


# ── Embedding ────────────────────────────────────────────────────────────────

async def embed_texts(texts: list[str]) -> list[list[float]]:
    """调用 DashScope Embedding API，返回每条文本的向量。"""
    if not texts:
        return []
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{settings.DASHSCOPE_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {settings.DASHSCOPE_API_KEY}"},
            json={"model": settings.EMBEDDING_MODEL, "input": texts},
        )
        r.raise_for_status()
    data = r.json()
    items = sorted(data["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in items]


# ── 检索 ─────────────────────────────────────────────────────────────────────

async def search_norms(query: str, top_k: int = 3) -> list[dict]:
    """混合检索（kNN 向量 + BM25 文本），命中子块后返回父块完整内容。

    返回列表，每项：
    {
      "parent_id": str,
      "content":   str,   # 父块 ~800 token 完整文本
      "source":    str,   # 来源文件名
      "score":     float,
    }
    """
    if not query.strip():
        return []

    es = get_es_client()

    # 获取 query embedding
    try:
        qvec = (await embed_texts([query]))[0]
    except Exception as e:
        logger.warning("embed query failed, fallback to BM25 only: %s", e)
        qvec = None

    # kNN + BM25 混合查询
    if qvec:
        body: dict[str, Any] = {
            "knn": {
                "field":        "embedding",
                "query_vector": qvec,
                "k":            top_k * 2,
                "num_candidates": top_k * 10,
            },
            "query": {
                "match": {"content": {"query": query, "boost": 0.3}}
            },
            "size": top_k * 2,
            "_source": ["parent_id", "source_file"],
        }
    else:
        body = {
            "query": {"match": {"content": query}},
            "size":  top_k * 2,
            "_source": ["parent_id", "source_file"],
        }

    try:
        resp = await es.search(index=IDX_CHILD, body=body)
    except Exception as e:
        logger.warning("ES search failed: %s", e)
        return []

    hits = resp["hits"]["hits"]
    if not hits:
        return []

    # 去重 parent_id，取评分最高的子块代表各父块
    seen_parents: dict[str, float] = {}
    for h in hits:
        pid   = h["_source"]["parent_id"]
        score = h["_score"] or 0.0
        if pid not in seen_parents or score > seen_parents[pid]:
            seen_parents[pid] = score

    # 按评分排序，只取 top_k 个父块
    top_parents = sorted(seen_parents.items(), key=lambda x: x[1], reverse=True)[:top_k]

    # 批量 mget 父块内容
    parent_ids = [pid for pid, _ in top_parents]
    try:
        mget = await es.mget(index=IDX_PARENT, body={"ids": parent_ids})
    except Exception as e:
        logger.warning("ES mget parent_doc failed: %s", e)
        return []

    pid_score = dict(top_parents)
    results = []
    for doc in mget["docs"]:
        if not doc.get("found"):
            continue
        src = doc["_source"]
        results.append({
            "parent_id": doc["_id"],
            "content":   src.get("content", ""),
            "source":    src.get("source_file", ""),
            "score":     pid_score.get(doc["_id"], 0.0),
        })

    return results
