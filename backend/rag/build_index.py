"""Build Qdrant collections from the legacy docstore.

    python -m backend.rag.build_index            # both v1 (baseline chunks) and v2 (cleaned + re-chunked)
    python -m backend.rag.build_index --only v2 --recreate
"""
import argparse
import json
import time

from backend.core import config
from backend.rag import documents as D
from backend.rag.store import ensure_collection, upsert_chunks, collection_stats

COLLECTIONS = {"v1": "sde_kb_v1", "v2": "sde_kb_v2"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(COLLECTIONS))
    ap.add_argument("--recreate", action="store_true")
    args = ap.parse_args()

    nodes = D.load_legacy_nodes()
    targets = [args.only] if args.only else list(COLLECTIONS)
    for version in targets:
        name = COLLECTIONS[version]
        t = time.time()
        if version == "v1":
            chunks, report = D.legacy_chunks(nodes), {"strategy": "legacy LlamaIndex chunks, unchanged"}
        else:
            docs, report = D.clean_documents(D.reconstruct_documents(nodes))
            chunks = D.chunk_documents(docs)
            report["strategy"] = "cleaned docs, markdown-section aware, 320-token chunks, contextual headers"
        ensure_collection(name, recreate=args.recreate)
        upsert_chunks(name, chunks)
        report.update(collection_stats(name), seconds=round(time.time() - t))
        print(json.dumps({version: report}, indent=2))
    print("Live collection used by the app:", config.KB_COLLECTION)


if __name__ == "__main__":
    main()
