from dataclasses import replace
import json
import os
from unittest.mock import patch

import httpx

from app.services import ai_client as ai
from app.services.cache import generate_versioned_cache_key
from app.services.document_provider import Chunk
from app.services.errors import BudgetExceeded, GenerationConfigChanged, InvalidCitation, ProviderUnavailable
from tests.support import SynthesisTestCase

resolve_model_revision = ai.resolve_model_revision


class ModelRevisionTests(SynthesisTestCase):
    def test_installed_tag_is_resolved_to_digest(self):
        response = httpx.Response(200, json={"models": [{"name": "tiny:latest", "digest": "actual-digest"}]}, request=httpx.Request("GET", "http://ollama.invalid/api/tags"))
        with patch.object(ai, "AI_MODEL", "tiny"), patch.object(ai.httpx, "get", return_value=response):
            self.assertEqual(resolve_model_revision(), "actual-digest")

    def test_missing_model_or_digest_is_unavailable(self):
        for models in ([], [{"name": ai.AI_MODEL}], [{"name": "other", "digest": "digest"}]):
            response = httpx.Response(200, json={"models": models}, request=httpx.Request("GET", "http://ollama.invalid/api/tags"))
            with self.subTest(models=models), patch.object(ai.httpx, "get", return_value=response), self.assertRaises(ProviderUnavailable):
                resolve_model_revision()

    def test_invalid_metadata_and_network_failure_are_unavailable(self):
        for response in (httpx.Response(503, request=httpx.Request("GET", "http://ollama.invalid")),
                         httpx.Response(200, json=[], request=httpx.Request("GET", "http://ollama.invalid"))):
            with patch.object(ai.httpx, "get", return_value=response), self.assertRaises(ProviderUnavailable):
                resolve_model_revision()
        with patch.object(ai.httpx, "get", side_effect=httpx.ConnectError("offline")), self.assertRaises(ProviderUnavailable):
            resolve_model_revision()

    def test_invalid_generation_options_are_rejected(self):
        for options in ({"AI_CONTEXT_SIZE": "100"}, {"AI_OUTPUT_TOKENS": "1"},
                        {"AI_TEMPERATURE": "nan"}, {"AI_OUTPUT_TOKENS": "2048"}):
            with self.subTest(options=options), patch.dict(os.environ, options), self.assertRaises(ValueError):
                ai.get_generation_config()


class ModelTests(SynthesisTestCase):
    def inference(self, **changes):
        body = {"model": self.config["model"], "done": True,
                "response": '{"selected_ids":{"D1":["E1"]}}',
                "eval_count": 8, "prompt_eval_count": 100, **changes}
        def respond(request):
            payload = json.loads(request.content)
            self.assertFalse(payload["stream"])
            choices = payload["format"]["properties"]["selected_ids"]
            self.assertEqual(choices["required"], ["D1"])
            self.assertEqual(choices["properties"]["D1"]["items"]["enum"], ["E1"])
            return httpx.Response(200, json=body)
        client = self.enterContext(httpx.Client(transport=httpx.MockTransport(respond)))
        return self.enterContext(patch.object(ai.httpx, "stream", side_effect=client.stream))

    def generate(self):
        return ai._generate("Small prompt", self.config, lambda: None, {"D1": ["E1"]}, 1)

    def test_structured_schema_restricts_ids_and_requires_document(self):
        self.inference()
        self.assertEqual(json.loads(self.generate()), {"selected_ids": {"D1": ["E1"]}})

    def test_model_failure_cannot_become_success_or_truncated_text(self):
        for changes, error in [({"done": False}, ProviderUnavailable),
                               ({"response": ""}, ProviderUnavailable),
                               ({"model": "wrong-model"}, GenerationConfigChanged),
                               ({"done_reason": "length"}, BudgetExceeded),
                               ({"eval_count": self.config["options"]["num_predict"]}, BudgetExceeded),
                               ({"prompt_eval_count": self.config["options"]["num_ctx"]}, BudgetExceeded)]:
            with self.subTest(changes=changes):
                self.inference(**changes)
                with self.assertRaises(error):
                    self.generate()

    def test_transport_error_is_retryable(self):
        with patch.object(ai.httpx, "stream", side_effect=httpx.ConnectError("offline")), self.assertRaises(ProviderUnavailable):
            self.generate()

    def test_over_context_prompt_fails_before_inference(self):
        with patch.object(ai.httpx, "stream") as stream, self.assertRaises(BudgetExceeded):
            ai._generate("X" * 5000, self.config, lambda: None, {"D1": ["E1"]}, 1)
        stream.assert_not_called()

    def test_oversized_response_is_bounded(self):
        with patch.object(ai.httpx, "stream") as stream:
            response = stream.return_value.__enter__.return_value
            response.iter_bytes.return_value = [b"X" * 100000]
            with self.assertRaises(BudgetExceeded):
                self.generate()

    def test_configuration_change_rejects_snapshot(self):
        with patch.dict(os.environ, {"AI_TEMPERATURE": "0.7"}), self.assertRaises(GenerationConfigChanged):
            ai.ensure_generation_config(self.config)


class SummaryTests(SynthesisTestCase):
    @staticmethod
    def select_one(prompt, config, checkpoint, choices, limit):
        checkpoint()
        return json.dumps({"selected_ids": {doc: [ids[0]] for doc, ids in choices.items()}})

    def test_output_is_original_text_with_server_owned_citations(self):
        chunks = [replace(self.chunks[0], text="Original [1]."), self.chunks[1]]
        with patch.object(ai, "_generate", side_effect=self.select_one):
            result = ai.generate_summary(chunks, "Summarise", self.config)
        self.assertEqual(result["summary_text"], "> Original [1]. [S1]\n\n> Consumers handle duplicates. [S2]")
        self.assertEqual([c["chunk_id"] for c in result["citations"]], ["c1", "c2"])
        self.assertEqual(result["model_version"], "digest-v1")

    def test_wrong_source_invented_duplicate_or_missing_selections_fail(self):
        records = [{"id": "E1", "document_id": "D1"}, {"id": "E2", "document_id": "D2"}]
        invalid = ["not JSON", "[]", '{"selected_ids":[]}',
                   '{"selected_ids":{"D1":["E1"]}}',
                   '{"selected_ids":{"D1":["E2"],"D2":["E1"]}}',
                   '{"selected_ids":{"D1":["fake"],"D2":["E2"]}}',
                   '{"selected_ids":{"D1":["E1","E1"],"D2":["E2"]}}']
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(InvalidCitation):
                ai.select_excerpts(selection, records)

    def test_reduction_requires_one_excerpt_per_document(self):
        records = [{"id": "E1", "document_id": "D1"}, {"id": "E2", "document_id": "D1"}]
        with self.assertRaises(InvalidCitation):
            ai.select_excerpts('{"selected_ids":{"D1":["E1","E2"]}}', records, "reduce")

    def test_long_documents_visit_all_original_passages(self):
        chunks = [Chunk(f"doc-{i}", 1, f"c{i}", " ".join(f"{'A' * 100}{n}." for n in range(45))) for i in range(2)]
        seen = {}
        def infer(prompt, config, checkpoint, choices, limit):
            self.assertTrue(ai.prompt_fits(prompt, config))
            records = json.loads(prompt.split("Source material (JSON):\n", 1)[1])
            seen.update({r["id"]: r for r in records})
            return self.select_one(prompt, config, checkpoint, choices, limit)
        with patch.object(ai, "_generate", side_effect=infer):
            result = ai.generate_summary(chunks, "Summarise", self.config)
        self.assertGreater(result["calls"], 1)
        self.assertEqual(len(result["citations"]), 2)
        for i, chunk in enumerate(chunks, 1):
            observed = "".join(r["text"] for r in seen.values() if r["document_id"] == f"D{i}")
            self.assertEqual(observed, chunk.text.replace(" ", ""))

    def test_long_unpunctuated_source_can_reduce(self):
        with patch.object(ai, "_generate", side_effect=self.select_one):
            result = ai.generate_summary([Chunk("a", 1, "c", "A" * 9000)], "Summarise", self.config)
        self.assertGreater(result["calls"], 1)
        self.assertEqual(len(result["citations"]), 1)

    def test_duplicate_passages_are_removed_without_losing_documents(self):
        chunks = [replace(c, text="Repeated fact. Repeated fact.") for c in self.chunks]
        with patch.object(ai, "_generate", side_effect=self.select_one):
            result = ai.generate_summary(chunks, "Summarise", self.config)
        self.assertEqual(result["summary_text"].count("Repeated fact."), 2)

    def test_source_and_call_budgets_fail_explicitly(self):
        with self.assertRaises(BudgetExceeded):
            ai.generate_summary(self.chunks, "Summarise", {**self.config, "max_source_bytes": 1})
        with patch.object(ai, "_generate", side_effect=self.select_one), self.assertRaises(BudgetExceeded):
            ai.generate_summary([Chunk("a", 1, "c", "A" * 9000)], "Summarise", {**self.config, "max_calls": 1})


class CacheTests(SynthesisTestCase):
    def key(self, user="alice", versions=None, instruction="Summarise", config=None):
        versions = self.versions if versions is None else versions
        return generate_versioned_cache_key(user, [{"documentId": d, "documentVersion": v} for d, v in versions.items()], instruction, config or self.config)

    def test_order_and_instruction_whitespace_do_not_change_identity(self):
        self.assertEqual(self.key(), self.key(versions=dict(reversed(list(self.versions.items()))), instruction=" Summarise "))

    def test_user_version_instruction_and_model_changes_separate_cache(self):
        for key in (self.key(user="bob"), self.key(versions={"doc-601": 2, "doc-602": 1}),
                    self.key(instruction="Other"), self.key(config={**self.config, "model_revision": "digest-v2"})):
            self.assertNotEqual(self.key(), key)

    def test_invalid_document_metadata_is_rejected(self):
        for documents in ([], [{"documentId": "a", "documentVersion": True}],
                          [{"documentId": "a", "documentVersion": 0}],
                          [{"documentId": "a", "documentVersion": 1}] * 2):
            with self.subTest(documents=documents), self.assertRaises(ValueError):
                generate_versioned_cache_key("alice", documents, "Summarise", self.config)
