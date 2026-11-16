"""Small offline self-check: python synthesis-service/scripts/smoke.py (no services required)."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError

from app.main import app
from app.schemas import SummaryRequest
from app.services import ai_client as ai, job_execution as jobs
from app.services.cache import generate_versioned_cache_key
from app.services.citations import source_catalog, validate_citations
from app.services.document_provider import Chunk, DocumentBundle, get_document_chunks, validate_bundle
from app.services.errors import (
    BudgetExceeded, GenerationConfigChanged, InvalidCitation, InvalidSourceData,
    ProviderNotConfigured, SourceVersionExpired,
)


def rejects(error_type, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except error_type:
        return
    raise AssertionError(f"Expected {error_type.__name__}")


def main():
    environment = {"STUDYHUB_DEV_IDENTITY": "1", "DOCUMENT_PROVIDER": "mock",
                   "AI_CONTEXT_SIZE": "2048", "AI_OUTPUT_TOKENS": "96", "AI_MAX_CALLS": "64"}
    with patch.dict(os.environ, environment), patch.object(ai, "resolve_model_revision", return_value="digest-v1"):
        schema = app.openapi()
        assert "citations" in schema["components"]["schemas"]["JobStatusResponse"]["properties"]
        request = SummaryRequest(document_ids=[" doc-601 "], instruction=" Summarise ")
        assert request.document_ids == ["doc-601"] and request.instruction == "Summarise"
        assert SummaryRequest(document_ids=["{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"]).document_ids == [
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
        for values in ({"document_ids": ["a"], "instruction": "   "},
                       {"document_ids": ["a", " a "]}, {"document_ids": [" "]}):
            rejects(ValidationError, SummaryRequest, **values)
            assert TestClient(app).post("/api/v1/summaries", json=values).status_code == 422

        versions = {"doc-601": 1, "doc-602": 1}
        chunks = get_document_chunks(list(reversed(versions)), versions, user_id="alice")
        assert [c.document_id for c in chunks] == list(versions)
        rejects(InvalidSourceData, validate_bundle, DocumentBundle(chunks[:1], versions), versions)
        rejects(InvalidSourceData, validate_bundle, DocumentBundle(chunks + chunks[:1], versions), versions)
        wrong = [Chunk("doc-601", 2, "c", "text"), chunks[1]]
        rejects(InvalidSourceData, validate_bundle, DocumentBundle(wrong, versions), versions)
        with patch.dict(os.environ, {"STUDYHUB_DEV_IDENTITY": "0"}):
            rejects(ProviderNotConfigured, get_document_chunks, list(versions), versions, user_id="alice")
        with patch.dict(os.environ, {"DOCUMENT_PROVIDER": "search", "SEARCH_SERVICE_URL": ""}):
            rejects(ProviderNotConfigured, get_document_chunks, list(versions), versions, user_id="alice")

        with tempfile.TemporaryDirectory(prefix="synthesis-fixture-smoke-") as scratch:
            fixture = Path(scratch) / "documents.json"
            fixture.write_text(json.dumps({"doc-601": {"version": 1, "content": "Synthetic content"}}), encoding="utf-8")
            with patch.dict(os.environ, {"MOCK_DOCUMENTS_FILE": str(fixture)}):
                assert get_document_chunks(["doc-601"], {"doc-601": 1}, user_id="alice")[0].text == "Synthetic content"
                old_config = ai.get_generation_config()
                fixture.write_text(json.dumps({"doc-601": {"version": 1, "content": "Changed fixture"}}), encoding="utf-8")
                rejects(GenerationConfigChanged, ai.ensure_generation_config, old_config)
                fixture.write_text("[]", encoding="utf-8")
                rejects(ProviderNotConfigured, get_document_chunks, ["doc-601"], {"doc-601": 1}, user_id="alice")

        catalog = source_catalog(chunks)
        citations = validate_citations("Overview [S1] and [S2].", catalog)
        assert citations[0]["document_version"] == 1 and citations[0]["chunk_id"] == "doc-601:1"
        for invalid in ("Unknown [S99]", "Unknown [doc-fake]", "No references", "Broken [S1"):
            rejects(InvalidCitation, validate_citations, invalid, catalog)

        config = ai.get_generation_config()
        documents = [{"documentId": d, "documentVersion": v} for d, v in versions.items()]
        key = generate_versioned_cache_key("alice", documents, "Summarise", config)
        assert key == generate_versioned_cache_key("alice", list(reversed(documents)), " Summarise ", config)
        changed = {**config, "options": {**config["options"], "temperature": 0.7}}
        assert key != generate_versioned_cache_key("alice", documents, "Summarise", changed)
        rejects(GenerationConfigChanged, ai.ensure_generation_config, changed)
        job = {"job_id": "offline", "document_ids": list(versions), "document_versions": versions,
               "generation_config": config, "user_id": "alice", "instruction": "Summarise", "cache_key": key}
        jobs.validate_job_snapshot(job)
        rejects(InvalidSourceData, jobs.validate_job_snapshot, {**job, "document_versions": None})
        rejects(InvalidSourceData, jobs.validate_job_snapshot, {**job, "cache_key": "stale"})

        def ollama_response(request):
            payload = json.loads(request.content)
            assert payload["options"] == config["options"] and payload["stream"] is False
            return httpx.Response(200, json={"model": config["model"], "done": True,
                                           "response": "Overview [S1]", "eval_count": 8,
                                           "prompt_eval_count": 100})

        with httpx.Client(transport=httpx.MockTransport(ollama_response)) as client, \
                patch.object(ai.httpx, "stream", side_effect=client.stream):
            assert ai._generate("Small prompt", config, lambda: None) == "Overview [S1]"

        # Simulated inference checks batching/coverage without calling Ollama.
        seen = {"S1": "", "S2": ""}
        calls = []

        def inference(prompt, config, checkpoint):
            checkpoint()
            assert ai.prompt_fits(prompt, config)
            records, _ = json.JSONDecoder().raw_decode(prompt.split("Source material (JSON):\n", 1)[1])
            calls.append(records)
            labels = list(dict.fromkeys(label for r in records for label in r["source_ids"]))
            for record in records:
                if set(record["text"]) in ({"A"}, {"B"}):
                    seen[record["source_ids"][0]] += record["text"]
            return "Overview " + " ".join(f"[{label}]" for label in labels)

        long_chunks = [Chunk("doc-601", 1, "a", "A" * 4500), Chunk("doc-602", 1, "b", "B" * 4500)]
        with patch.object(ai, "_generate", side_effect=inference):
            result = ai.generate_summary(long_chunks, "Summarise", config)
            assert len(calls) > 1 and len(result["citations"]) == 2
            assert seen == {"S1": "A" * 4500, "S2": "B" * 4500}
            rejects(BudgetExceeded, ai.generate_summary, long_chunks, "Summarise", {**config, "max_calls": 1})
        with patch.object(ai, "_generate", return_value="Fake source [S999]"):
            rejects(InvalidCitation, ai.generate_summary, chunks, "Summarise", config)

        with patch.object(jobs, "claim_job", return_value=job), \
                patch.object(jobs, "verify_source_snapshot", return_value={"doc-601": 2, "doc-602": 1}), \
                patch.object(jobs, "get_document_chunks") as retrieve, \
                patch.object(jobs, "complete_job") as save, patch.object(jobs, "fail_job") as fail:
            jobs.process_job("offline")
            assert isinstance(fail.call_args.args[1], SourceVersionExpired)
            retrieve.assert_not_called()
            save.assert_not_called()
        with patch.object(jobs, "claim_job", return_value=job), \
                patch.object(jobs, "verify_source_snapshot", side_effect=[versions, {"doc-601": 2, "doc-602": 1}]), \
                patch.object(jobs, "get_document_chunks", return_value=chunks), \
                patch.object(jobs, "generate_summary", return_value=result), \
                patch.object(jobs, "complete_job") as save, patch.object(jobs, "fail_job") as fail:
            jobs.process_job("offline")
            assert isinstance(fail.call_args.args[1], SourceVersionExpired)
            save.assert_not_called()
    print("PASS: schemas/HTTP 422, OpenAPI, provider coverage/fail-closed, citations, cache/config snapshots,")
    print("      bounded multi-document map/reduce, queued and post-generation version changes")


if __name__ == "__main__":
    main()
