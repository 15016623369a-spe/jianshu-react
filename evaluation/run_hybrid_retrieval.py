"""Compare BM25 and optional dense/hybrid retrieval on the manual holdout."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from app.rag.hybrid import HybridIndex
from app.rag.rerank import build_reranker
from app.rag.ingest import parse_markdown
from app.rag.models import DocumentMetadata
from app.rag.retrieval import BM25Index

ROOT = Path(__file__).resolve().parents[1]
MANUAL = ROOT / "app" / "manual_corpus"
CASES = Path(__file__).with_name("manual_retrieval_cases.json")
MODEL = Path("/Users/jace/RAG/data/bird/.retrieval/bge-m3")


def corpus() -> tuple:
    passages = []
    for version in (1, 2):
        raw = (MANUAL / f"guide-v{version}.md").read_text()
        metadata = DocumentMetadata(f"guide-v{version}", f"手册第 {version} 版", source="manual", version=version)
        passages.extend(parse_markdown(raw, metadata))
    return tuple(passages)


def recall(hits, gold: str, k: int) -> bool:
    return any(hit.passage.section == gold for hit in hits[:k])


def run(*, dense: bool, rerank: bool = False, repeats: int = 3) -> dict:
    passages = corpus()
    index = HybridIndex(passages)
    if dense:
        from app.rag.dense import SentenceTransformerDense
        dense_index = SentenceTransformerDense(passages, model_name=str(MODEL), local_files_only=True)
        index = HybridIndex(passages, dense=dense_index)
    if rerank:
        index = HybridIndex(passages, reranker=build_reranker(passages))
    cases = json.loads(CASES.read_text())["cases"]
    timings = []
    results = []
    for repeat in range(repeats):
        for case in cases:
            began = perf_counter()
            hits = index.search(case["query"], top_k=5, filters={"version": case["version"]})
            timings.append((perf_counter() - began) * 1000)
            results.append({
                "id": case["id"], "query": case["query"], "gold": case["gold"],
                "rank": next((rank for rank, hit in enumerate(hits, 1) if hit.passage.section == case["gold"]), None),
            })
    return {
        "suite": "manual-retrieval-holdout-v1", "dense": dense, "rerank": rerank, "repeats": repeats,
        "cases": len(cases) * repeats, "recall@1": sum(hit["rank"] == 1 for hit in results) / len(results),
        "recall@3": sum(hit["rank"] is not None and hit["rank"] <= 3 for hit in results) / len(results),
        "recall@5": sum(hit["rank"] is not None and hit["rank"] <= 5 for hit in results) / len(results),
        "median_ms": median(timings), "p95_ms": sorted(timings)[max(0, int(len(timings) * .95) - 1)],
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dense", action="store_true")
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(dense=args.dense, rerank=args.rerank, repeats=args.repeats)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("suite", "dense", "rerank", "cases", "recall@1", "recall@3", "recall@5", "median_ms", "p95_ms")}, ensure_ascii=False))
    failures = [item for item in report["results"] if item["rank"] is None]
    for item in failures:
        print("MISS", item["id"], item["query"], "gold=", item["gold"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
