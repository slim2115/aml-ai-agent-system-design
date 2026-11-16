"""Run with: python -B -m unittest discover -s tests/configuration -v."""
from __future__ import annotations

import builtins
import hashlib
import json
import os
import socket
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from src import config
from src.retrieval.embedding import (
    EmbeddingNotReadyError,
    build_embedding_function,
    validate_embedding_artifact,
)


class ConfigurationTests(unittest.TestCase):
    def test_knowledge_base_override_is_project_anchored_and_cannot_escape(self):
        from src.retrieval.knowledge_base import KnowledgeBase
        settings = config.get_settings()
        kb = KnowledgeBase([], persist_dir='var/tmp/override-check')
        self.assertEqual(Path(kb._persist_dir), settings.project_root / 'var/tmp/override-check')
        for path in ('../outside', str(settings.project_root.parent / 'outside'), ''):
            with self.subTest(path=path), self.assertRaises(ValueError):
                KnowledgeBase([], persist_dir=path)

    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        config.get_settings.cache_clear()

    def tearDown(self) -> None:
        config.get_settings.cache_clear()
        self.env.stop()

    def test_defaults_are_absolute_and_have_required_quality_floors(self) -> None:
        settings = config.get_settings()
        self.assertEqual(settings.llm.model_name, "qwen2.5:14b")
        self.assertEqual(settings.llm.embedding_model_name, config.DEFAULT_EMBEDDING_MODEL)
        self.assertEqual(settings.thresholds.rag_recall, 1.0)
        self.assertEqual(settings.thresholds.rag_precision, 1.0)
        self.assertEqual(settings.thresholds.faithfulness, 0.90)
        self.assertEqual(settings.thresholds.schema_match, 1.0)
        self.assertEqual(settings.thresholds.traceability, 1.0)
        self.assertEqual(settings.thresholds.refusal_correctness, 0.95)
        self.assertEqual(settings.performance.generation_timeout_sec, 180)
        for value in (
            settings.vector_store.persist_dir,
            settings.data.synthetic_data_dir,
            settings.llm.embedding_model_path,
            settings.llm.embedding_manifest_path,
        ):
            self.assertTrue(Path(value).is_absolute())
            self.assertTrue(Path(value).is_relative_to(settings.project_root))

    def test_import_has_no_settings_io_dotenv_loading_or_environment_mutation(self) -> None:
        source = compile(Path(config.__file__).read_text(encoding="utf-8-sig"), config.__file__, "exec")
        module = types.ModuleType("_configuration_import_contract")
        module.__file__ = config.__file__
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = Mock(side_effect=AssertionError("dotenv must not load"))
        before = dict(os.environ)
        with (
            patch.dict(sys.modules, {module.__name__: module, "dotenv": fake_dotenv}),
            patch.object(Path, "resolve", side_effect=AssertionError("import-time resolution")),
            patch.object(Path, "open", side_effect=AssertionError("import-time file access")),
            patch.object(builtins, "open", side_effect=AssertionError("import-time file access")),
        ):
            exec(source, module.__dict__)
        fake_dotenv.load_dotenv.assert_not_called()
        self.assertEqual(dict(os.environ), before)

    def test_relative_paths_use_project_root_not_current_directory(self) -> None:
        os.environ["CHROMA_PERSIST_DIR"] = "var/isolated-chroma"
        old_cwd = Path.cwd()
        try:
            os.chdir(config.PROJECT_ROOT.parent)
            settings = config.get_settings()
        finally:
            os.chdir(old_cwd)
        self.assertEqual(Path(settings.vector_store.persist_dir), settings.project_root / "var" / "isolated-chroma")

    def test_all_configured_paths_reject_outside_and_traversal_values(self) -> None:
        for key in ("CHROMA_PERSIST_DIR", "SYNTHETIC_DATA_DIR", "EMBEDDING_MODEL_PATH", "EMBEDDING_MANIFEST_PATH"):
            for value in ("../outside", "var/../inside", "var\\..\\inside", str(config.PROJECT_ROOT.parent / "outside")):
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                    config.get_settings.cache_clear()
                    with self.assertRaises(ValueError):
                        config.get_settings()

    def test_resolved_escape_is_rejected_without_needing_symlink_privileges(self) -> None:
        root = config.PROJECT_ROOT.resolve()
        original_resolve = Path.resolve

        def resolve(path: Path, *args, **kwargs):
            if path == root / "var" / "escaping-link":
                return root.parent / "outside"
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            with self.assertRaises(ValueError):
                config.resolve_project_path("var/escaping-link", root=root, key="CHROMA_PERSIST_DIR")

    def test_loopback_urls_are_validated_without_dns(self) -> None:
        accepted = ("http://127.0.0.1:11434", "http://127.1.2.3:11434", "http://localhost:11434", "https://[::1]:11434/")
        refused = (
            "http://example.org:11434", "http://192.168.1.20:11434", "http://0.0.0.0:11434",
            "http://[::]:11434", "http://localhost.example:11434", "http://localhost.:11434",
            "http://127.0.0.1.example:11434", "http://localhost@remote.test:11434",
            "http://user:secret@127.0.0.1:11434", "http://127.0.0.1:0", "http://127.0.0.1:65536",
            "http://127.0.0.1:wrong", "http://localhost:11434/api", "http://localhost:11434/?x=1",
            "http://localhost:11434/#fragment", "ftp://127.0.0.1:11434", "http://[::1%lo0]:11434",
            "http://local\nhost:11434", "", "127.0.0.1:11434",
        )
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS is forbidden")):
            for value in accepted:
                with self.subTest(accepted=value):
                    self.assertEqual(config.validate_loopback_url(value), value.rstrip("/"))
            for value in refused:
                with self.subTest(refused=value), self.assertRaises(ValueError):
                    config.validate_loopback_url(value)

    def test_thresholds_cannot_weaken_acceptance(self) -> None:
        keys = {
            "FAITHFULNESS_THRESHOLD": 0.90,
            "SCHEMA_MATCH_THRESHOLD": 1.0,
            "TRACEABILITY_THRESHOLD": 1.0,
            "REFUSAL_CORRECTNESS_THRESHOLD": 0.95,
            "RAG_RECALL_THRESHOLD": 1.0,
            "RAG_PRECISION_THRESHOLD": 1.0,
        }
        for key, floor in keys.items():
            for value in (str(floor - 0.01), "-1", "1.01", "nan", "inf", "-inf", "", "invalid"):
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                    config.get_settings.cache_clear()
                    with self.assertRaises(ValueError):
                        config.get_settings()
        with patch.dict(os.environ, {"FAITHFULNESS_THRESHOLD": "0.97", "REFUSAL_CORRECTNESS_THRESHOLD": "1.0"}):
            config.get_settings.cache_clear()
            self.assertEqual(config.get_settings().thresholds.faithfulness, 0.97)

    def test_timeout_must_be_an_integer_between_one_and_180(self) -> None:
        for value in ("0", "-1", "181", "180.0", "nan", "inf", ""):
            with self.subTest(value=value), patch.dict(os.environ, {"GENERATION_TIMEOUT_SEC": value}):
                config.get_settings.cache_clear()
                with self.assertRaises(ValueError):
                    config.get_settings()
        for value in (1, 180):
            self.assertEqual(config.PerformanceConfig(value).generation_timeout_sec, value)
        for value in (True, 1.5, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.PerformanceConfig(value)


class OfflineEmbeddingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        config.get_settings.cache_clear()
        base = config.PROJECT_ROOT / "var" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="configuration-test-", dir=base)
        self.root = Path(self.temporary.name)
        self.model_dir = self.root / "model"
        self.model_dir.mkdir()
        self.manifest_path = self.root / "model.manifest.json"
        (self.model_dir / "config.json").write_text("{}", encoding="utf-8")
        (self.model_dir / "weights.bin").write_bytes(b"synthetic-test-artifact-not-a-model")
        (self.model_dir / "1_Pooling").mkdir()
        (self.model_dir / "1_Pooling" / "config.json").write_text("{}", encoding="utf-8")
        self.manifest = {
            "schema_version": 1,
            "model_id": config.DEFAULT_EMBEDDING_MODEL,
            "files": [
                {"path": p.relative_to(self.model_dir).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(self.model_dir.rglob("*")) if p.is_file()
            ],
        }
        self.write_manifest()
        settings = config.get_settings()
        self.settings = replace(settings, llm=replace(settings.llm, embedding_model_path=str(self.model_dir), embedding_manifest_path=str(self.manifest_path)))

    def tearDown(self) -> None:
        self.temporary.cleanup()
        config.get_settings.cache_clear()
        self.env.stop()

    def write_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def assert_not_ready(self) -> None:
        factory = Mock()
        with self.assertRaises(EmbeddingNotReadyError) as raised:
            build_embedding_function(self.settings, model_factory=factory)
        self.assertEqual(raised.exception.code, "EMBEDDING_NOT_READY")
        self.assertNotIn(str(self.model_dir), str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        factory.assert_not_called()

    def test_valid_artifact_uses_configured_path_cpu_and_offline_only(self) -> None:
        model = Mock()
        model.encode.return_value.tolist.return_value = [[0.1, 0.2]]
        factory = Mock(return_value=model)
        embedding = build_embedding_function(self.settings, model_factory=factory)
        factory.assert_called_once_with(str(self.model_dir.resolve()), device="cpu", local_files_only=True, trust_remote_code=False)
        self.assertEqual(embedding(["synthetic test"]), [[0.1, 0.2]])
        model.encode.assert_called_once_with(["synthetic test"], convert_to_numpy=True, normalize_embeddings=False, show_progress_bar=False)
        self.assertEqual(embedding.get_config()["model_name"], config.DEFAULT_EMBEDDING_MODEL)

    def test_configured_model_identity_is_applied(self) -> None:
        changed = replace(self.settings, llm=replace(self.settings.llm, embedding_model_name="local/other-approved-model"))
        with self.assertRaises(EmbeddingNotReadyError):
            validate_embedding_artifact(changed)
        self.manifest["model_id"] = "local/other-approved-model"
        self.write_manifest()
        embedding = build_embedding_function(changed, model_factory=Mock())
        self.assertEqual(embedding.get_config()["model_name"], "local/other-approved-model")

    def test_missing_manifest(self) -> None:
        self.manifest_path.unlink()
        self.assert_not_ready()

    def test_missing_artifact(self) -> None:
        self.settings = replace(self.settings, llm=replace(self.settings.llm, embedding_model_path=str(self.root / "missing")))
        self.assert_not_ready()

    def test_wrong_identity(self) -> None:
        self.manifest["model_id"] = "unexpected/model"
        self.write_manifest()
        self.assert_not_ready()

    def test_corrupt_file(self) -> None:
        (self.model_dir / "weights.bin").write_bytes(b"altered")
        self.assert_not_ready()

    def test_missing_inventory_file(self) -> None:
        (self.model_dir / "weights.bin").unlink()
        self.assert_not_ready()

    def test_extra_file(self) -> None:
        (self.model_dir / "unlisted.bin").write_bytes(b"extra")
        self.assert_not_ready()

    def test_duplicate_manifest_path(self) -> None:
        self.manifest["files"].append(dict(self.manifest["files"][0]))
        self.write_manifest()
        self.assert_not_ready()

    def test_manifest_path_spellings_cannot_escape_or_alias(self) -> None:
        original = self.manifest["files"][0]["path"]
        for value in ("../outside", "/outside", "C:/outside", "sub/../config.json", "sub\\config.json", "./config.json", "sub//config.json", "config.json\x00"):
            with self.subTest(value=value):
                self.manifest["files"][0]["path"] = value
                self.write_manifest()
                self.assert_not_ready()
        self.manifest["files"][0]["path"] = original

    def test_malformed_manifest(self) -> None:
        for invalid in ({}, [], {"schema_version": True, "model_id": config.DEFAULT_EMBEDDING_MODEL, "files": []}):
            with self.subTest(invalid=invalid):
                self.manifest_path.write_text(json.dumps(invalid), encoding="utf-8")
                self.assert_not_ready()
        self.manifest_path.write_text("not JSON", encoding="utf-8")
        self.assert_not_ready()

    def test_loader_exception_is_sanitized_without_fallback(self) -> None:
        factory = Mock(side_effect=RuntimeError("private path and token must stay hidden"))
        with self.assertRaises(EmbeddingNotReadyError) as raised:
            build_embedding_function(self.settings, model_factory=factory)
        factory.assert_called_once()
        self.assertNotIn("private", str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)

    def test_knowledge_base_import_does_not_import_heavy_runtime(self) -> None:
        source_path = config.PROJECT_ROOT / "src" / "retrieval" / "knowledge_base.py"
        source = compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"chromadb", "sentence_transformers", "pydantic"}:
                raise AssertionError(f"Unexpected heavy import: {name}")
            return original_import(name, *args, **kwargs)

        module = types.ModuleType("_knowledge_base_import_contract")
        module.__file__ = str(source_path)
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(builtins, "__import__", guarded_import):
            exec(source, module.__dict__)


if __name__ == "__main__":
    unittest.main()
