"""Upload synthetic documents and write matching mock text; no Search dependency."""
import argparse
import json
from pathlib import Path

import httpx

FIXTURE = Path(__file__).resolve().parents[1] / "dev-fixtures" / "documents.json"
DEMO_TEXTS = [
    "A transactional outbox stores an event in the same database transaction as the business update. "
    "A relay publishes committed events to RabbitMQ. An outbox alone does not guarantee message order.",
    "RabbitMQ can redeliver a message after a consumer crashes. "
    "An idempotent consumer handles repeated deliveries without repeating business effects. "
    "A consumer acknowledges a message after its database transaction commits.",
]


def prepare_documents(texts, *, content_url="http://127.0.0.1:8000", fixture=FIXTURE):
    documents = {}
    with httpx.Client(base_url=content_url, headers={"User_Name": "alice"}, timeout=15) as client:
        for index, text in enumerate(texts):
            response = client.post("/documents", files={
                "file": (f"synthesis-demo-{index + 1}.txt", text.encode(), "text/plain")})
            response.raise_for_status()
            doc_id = response.json()["documentId"]
            metadata = client.get(f"/documents/{doc_id}")
            metadata.raise_for_status()
            documents[doc_id] = {"version": metadata.json()["documentVersion"], "content": text}
    fixture.parent.mkdir(parents=True, exist_ok=True)
    temporary = fixture.with_suffix(".tmp")
    temporary.write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(fixture)
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if FIXTURE.exists():
        raise SystemExit("Fixture already exists. Use its document IDs, or remove it after stopping active jobs to prepare new demo documents.")
    documents = prepare_documents(DEMO_TEXTS, content_url=args.content_url)
    print(json.dumps({"document_ids": list(documents), "fixture": str(FIXTURE)}, indent=2))


if __name__ == "__main__":
    main()
