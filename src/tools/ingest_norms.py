"""团队规范知识库初始化工具（父子分块 → ES 向量入库）。

用法：
    python -m src.tools.ingest_norms --path ./docs/coding_standards.md
    python -m src.tools.ingest_norms --path ./docs/ --clear

父子分块策略：
  父块（~800 token ≈ 2400 char）：按 Markdown 章节 / 段落边界拆分，保留完整规范语境
  子块（~150 token ≈ 450 char） ：父块内再次拆分，用于向量检索
"""

import argparse
import asyncio
import logging
import re
import sys
import uuid
from pathlib import Path

from src.tools.norm_retriever import embed_texts, ensure_indices, get_es_client, IDX_CHILD, IDX_PARENT

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 父块 / 子块目标字符数（中英混合按 3 char/token 估算）
_PARENT_CHARS = 2400   # ≈ 800 tokens
_CHILD_CHARS  = 450    # ≈ 150 tokens
_CHILD_OVERLAP = 80    # 子块间的重叠字符数（保留上下文连贯性）

# 批量 embed 每批大小（DashScope 单次最多 25 条）
_EMBED_BATCH = 25
# 批量索引每批大小
_INDEX_BATCH = 50


# ── 文本分块 ─────────────────────────────────────────────────────────────────

def _split_parent_chunks(text: str) -> list[str]:
    """按 Markdown 标题 / 空行边界拆分父块（≈800 token）。"""
    # 先按 ## 级标题切割（保留标题本身）
    sections = re.split(r"(?=^#{1,3} )", text, flags=re.MULTILINE)
    chunks: list[str] = []
    buf = ""
    for sec in sections:
        if len(buf) + len(sec) <= _PARENT_CHARS:
            buf += sec
        else:
            if buf.strip():
                chunks.append(buf.strip())
            # 如果单个 section 超长，按段落继续拆
            if len(sec) > _PARENT_CHARS:
                paragraphs = re.split(r"\n{2,}", sec)
                pb = ""
                for para in paragraphs:
                    if len(pb) + len(para) <= _PARENT_CHARS:
                        pb += para + "\n\n"
                    else:
                        if pb.strip():
                            chunks.append(pb.strip())
                        pb = para + "\n\n"
                if pb.strip():
                    chunks.append(pb.strip())
            else:
                buf = sec
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) >= 50]  # 过滤过短碎片


def _split_child_chunks(parent: str) -> list[str]:
    """将父块切分为子块（≈150 token），相邻子块有 _CHILD_OVERLAP 字符重叠。"""
    chunks: list[str] = []
    start = 0
    while start < len(parent):
        end = start + _CHILD_CHARS
        chunk = parent[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start += _CHILD_CHARS - _CHILD_OVERLAP
    return chunks


# ── ES 索引操作 ───────────────────────────────────────────────────────────────

async def _index_document(file_path: str, text: str) -> int:
    """将一个文档（text）切分并入库，返回写入的子块数量。"""
    es = get_es_client()
    parent_chunks = _split_parent_chunks(text)
    logger.info("  %s → %d parent chunks", file_path, len(parent_chunks))

    total_children = 0
    for p_idx, parent_text in enumerate(parent_chunks):
        parent_id = uuid.uuid4().hex

        # 1. 写入父块
        await es.index(
            index=IDX_PARENT,
            id=parent_id,
            document={
                "content":      parent_text,
                "source_file":  file_path,
                "parent_index": p_idx,
            },
        )

        # 2. 拆分子块
        children = _split_child_chunks(parent_text)
        if not children:
            continue

        # 3. 批量获取 embedding
        for batch_start in range(0, len(children), _EMBED_BATCH):
            batch = children[batch_start: batch_start + _EMBED_BATCH]
            try:
                embeddings = await embed_texts(batch)
            except Exception as e:
                logger.error("embed failed for batch, skipping: %s", e)
                continue

            # 4. 批量写子块
            actions = []
            for c_idx, (child_text, emb) in enumerate(zip(batch, embeddings)):
                doc = {
                    "parent_id":   parent_id,
                    "content":     child_text,
                    "embedding":   emb,
                    "source_file": file_path,
                    "chunk_index": batch_start + c_idx,
                }
                actions.append({"index": {"_index": IDX_CHILD}})
                actions.append(doc)

            if actions:
                await es.bulk(operations=actions)
                total_children += len(batch)

    return total_children


async def _clear_indices() -> None:
    es = get_es_client()
    for idx in (IDX_CHILD, IDX_PARENT):
        exists = await es.indices.exists(index=idx)
        if exists:
            await es.indices.delete(index=idx)
            logger.info("Deleted index: %s", idx)


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def ingest(paths: list[Path], clear: bool = False) -> None:
    if clear:
        await _clear_indices()

    await ensure_indices()

    total_docs = 0
    total_children = 0
    for p in paths:
        files = list(p.rglob("*.md")) + list(p.rglob("*.txt")) if p.is_dir() else [p]
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                logger.info("Ingesting %s (%d chars)", f, len(text))
                n = await _index_document(str(f), text)
                total_children += n
                total_docs += 1
            except Exception as e:
                logger.error("Failed to ingest %s: %s", f, e)

    logger.info("Done: %d document(s), %d child chunks indexed", total_docs, total_children)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest team norms into ES knowledge base")
    parser.add_argument("--path", required=True, nargs="+",
                        help="File or directory to ingest (.md/.txt)")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing indices before ingesting")
    args = parser.parse_args()

    paths = [Path(p) for p in args.path]
    for p in paths:
        if not p.exists():
            print(f"ERROR: path does not exist: {p}", file=sys.stderr)
            sys.exit(1)

    asyncio.run(ingest(paths, clear=args.clear))


if __name__ == "__main__":
    main()
