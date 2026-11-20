"""Small offline self-check: python synthesis-service/scripts/smoke.py (no services required)."""
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import shutil
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
    ProviderNotConfigured, ProviderUnavailable, SourceVersionExpired,
)


def rejects(error_type, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except error_type:
        return
    raise AssertionError(f"Expected {error_type.__name__}")


def main():
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="synthesis-setup-check-") as scratch:
        folder = Path(scratch)
        (folder / "setup").mkdir()
        shutil.copy(root / ".env.example", folder / ".env.example")
        setup = root / "setup" / "setup-content-service.ps1"
        shutil.copy(setup, folder / "setup" / setup.name)
        shell_python = (root / "setup" / "setup-content-service.sh").read_text(encoding="utf-8").split("<<'PYTHON'\n", 1)[1].split("\nPYTHON", 1)[0]
        commands = [[sys.executable, "-c", shell_python]]
        if os.name == "nt":
            commands.append(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(folder / "setup" / setup.name)])
        for command in commands:
            local = folder / ".env"
            local.write_text("POSTGRES_PASSWORD=keep_existing\nSTUDYHUB_PROCESSING_TOKEN=keep_token\nSTUDYHUB_SEARCH_TOKEN=\n", encoding="utf-8")
            subprocess.run(command, cwd=folder, check=True, capture_output=True)
            first = local.read_text(encoding="utf-8")
            assert "POSTGRES_PASSWORD=keep_existing" in first and "STUDYHUB_PROCESSING_TOKEN=keep_token" in first
            assert "DOCUMENT_PROVIDER=mock" in first and "AI_SUMMARY_MODEL=qwen3:4b" in first
            assert "STUDYHUB_SEARCH_TOKEN=\n" not in first
            subprocess.run(command, cwd=folder, check=True, capture_output=True)
            assert local.read_text(encoding="utf-8") == first
    environment = {"STUDYHUB_DEV_IDENTITY": "1", "DOCUMENT_PROVIDER": "mock",
                   "AI_CONTEXT_SIZE": "2048", "AI_OUTPUT_TOKENS": "96", "AI_MAX_CALLS": "64"}
    with patch.dict(os.environ, environment), patch.object(ai, "resolve_model_revision", return_value="digest-v1"):
        from app.services import readiness
        from unittest.mock import MagicMock
        with patch("app.main.readiness_checks", return_value={"worker": False}):
            assert TestClient(app).get("/ready").status_code == 503
        with patch("app.main.readiness_checks", return_value={"worker": True}):
            assert TestClient(app).get("/ready").status_code == 200
        connection = MagicMock()
        connection.__enter__.return_value.execute.return_value.fetchall.return_value = [(s,) for s in readiness.SERVICES]
        broker = MagicMock()
        broker.__enter__.return_value.channel.return_value.queue_declare.return_value.method.consumer_count = 1
        with patch.object(readiness, "resolve_model_revision", return_value="digest-v1"), \
                patch.object(readiness, "get_connection", return_value=connection), \
                patch.object(readiness.pika, "BlockingConnection", return_value=broker), \
                patch.object(readiness.httpx, "post", return_value=httpx.Response(200, json={"documents": []}, request=httpx.Request("POST", "http://content/internal/documents/access-check"))), \
                patch.dict(os.environ, {"RABBITMQ_USER": "test", "RABBITMQ_PASSWORD": "test", "STUDYHUB_SYNTHESIS_TOKEN": "test"}):
            assert all(readiness.readiness_checks().values())
            broker.__enter__.return_value.channel.return_value.queue_declare.return_value.method.consumer_count = 0
            assert readiness.readiness_checks()["worker"] is False
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
            choice = payload["format"]["properties"]["selected_ids"]
            assert choice["required"] == ["D1"]
            assert choice["properties"]["D1"]["items"]["enum"] == ["E1"]
            return httpx.Response(200, json={"model": config["model"], "done": True,
                                           "response": json.dumps({"selected_ids": {"D1": ["E1"]}}), "eval_count": 8,
                                           "prompt_eval_count": 100})

        with httpx.Client(transport=httpx.MockTransport(ollama_response)) as client, \
                patch.object(ai.httpx, "stream", side_effect=client.stream):
            assert ai._generate("Small prompt", config, lambda: None, {"D1": ["E1"]}, 1) == json.dumps({"selected_ids": {"D1": ["E1"]}})

        # Remote inference failures must not become successful or truncated summaries.
        base = {"model": config["model"], "done": True, "response": "{}", "eval_count": 8, "prompt_eval_count": 100}
        for changes, expected in [({"done": False}, ProviderUnavailable),
                                  ({"response": ""}, ProviderUnavailable),
                                  ({"done_reason": "length"}, BudgetExceeded),
                                  ({"eval_count": config["options"]["num_predict"]}, BudgetExceeded),
                                  ({"model": "unexpected"}, GenerationConfigChanged),
                                  ({"prompt_eval_count": config["options"]["num_ctx"]}, BudgetExceeded)]:
            with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={**base, **changes}))) as client, \
                    patch.object(ai.httpx, "stream", side_effect=client.stream):
                rejects(expected, ai._generate, "Small prompt", config, lambda: None, {"D1": ["E1"]}, 1)
        from app.services import content_client
        with patch.object(content_client, "SYNTHESIS_TOKEN", "test"):
            for payload in ({"documents": [None]}, {"documents": [{"documentId": "a", "documentVersion": True}]},
                            {"documents": [{"documentId": "other", "documentVersion": 1}]},
                            {"documents": [{"documentId": "a", "documentVersion": 1}] * 2}):
                client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
                with patch.object(content_client.httpx, "Client", return_value=client):
                    rejects(content_client.ContentServiceUnavailable, content_client.verify_document_access, "alice", ["a"])

        # Simulated inference checks batching/coverage without calling Ollama.
        seen, calls = {}, []

        def inference(prompt, config, checkpoint, selection_ids, selection_limit):
            checkpoint()
            assert ai.prompt_fits(prompt, config)
            records, _ = json.JSONDecoder().raw_decode(prompt.split("Source material (JSON):\n", 1)[1])
            calls.append(records)
            selected = {}
            for record in records:
                seen[record["id"]] = record
                selected.setdefault(record["document_id"], [record["id"]])
            return json.dumps({"selected_ids": selected})

        long_chunks = [Chunk("doc-601", 1, "a", " ".join("A" * 100 + str(i) + "." for i in range(45))),
                       Chunk("doc-602", 1, "b", " ".join("B" * 100 + str(i) + "." for i in range(45)))]
        with patch.object(ai, "_generate", side_effect=inference):
            result = ai.generate_summary(long_chunks, "Summarise", config)
            assert len(calls) > 1 and len(result["citations"]) == 2
            for index, chunk in enumerate(long_chunks, 1):
                observed = "".join(r["text"] for r in seen.values() if r["document_id"] == f"D{index}")
                assert observed == chunk.text.replace(" ", "")
            rejects(BudgetExceeded, ai.generate_summary, long_chunks, "Summarise", {**config, "max_calls": 1})
        with patch.object(ai, "_generate", side_effect=inference):
            unpunctuated = ai.generate_summary([Chunk("one", 1, "c", "A" * 9000)], "Summarise", config)
            assert unpunctuated["calls"] > 1 and len(unpunctuated["citations"]) == 1
        with patch.object(ai, "_generate", return_value='{"selected_ids": ["E99999"]}'):
            rejects(InvalidCitation, ai.generate_summary, chunks, "Summarise", config)
        records = [{"id": "E1", "document_id": "a", "source_id": "S1", "text": "Original [1]."},
                   {"id": "E2", "document_id": "b", "source_id": "S2", "text": "Second fact."}]
        for invalid in ({"selected_ids": {"a": ["E1"]}},
                        {"selected_ids": {"a": ["E1", "E1"], "b": ["E2"]}},
                        {"selected_ids": {"a": ["E2"], "b": ["E1"]}},
                        {"selected_ids": {"a": ["fake"], "b": ["E2"]}},
                        {"selected_ids": [None]}, []):
            rejects(InvalidCitation, ai.select_excerpts, json.dumps(invalid), records)
        selected = ai.select_excerpts('{"selected_ids":{"b":["E2"],"a":["E1"]}}', records)
        assert selected == [records[1], records[0]]
        quote_chunks = [Chunk("a", 1, "a1", "Original [1]."), Chunk("b", 1, "b1", "Second fact.")]
        with patch.object(ai, "_generate", return_value='{"selected_ids":{"D2":["E2"],"D1":["E1"]}}'):
            quoted = ai.generate_summary(quote_chunks, "Summarise", config)
            assert quoted["summary_text"] == "> Second fact. [S2]\n\n> Original [1]. [S1]"
            assert [c["document_id"] for c in quoted["citations"]] == ["b", "a"]

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
    print("PASS: schemas/HTTP 422, OpenAPI, provider coverage/fail-closed, grounded excerpts, readiness, cache/config snapshots,")
    print("      bounded multi-document map/reduce, queued and post-generation version changes")


if __name__ == "__main__":
    main()
