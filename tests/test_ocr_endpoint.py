"""Dedicated OCR endpoint: configuration chain, vision-call routing in
ModelClient, parser OCR-mode wiring, and ingest-warning collection.

Contract under test:
- ocr_url empty (default) = OCR endpoint disabled; vision calls fall back
  to the main LLM endpoint (VLM path) — behavior unchanged for existing
  deployments
- resolution order: system DB > env > built-in default, same as llm/emb
- ModelClient.generate_with_image(endpoint="auto") routes to the OCR
  endpoint (url/model/key) when configured; "llm"/"ocr" force a backend
- parser OCR transcription passes endpoint="ocr" and returns "" on failure
  (caller turns that into an ingest warning, never a silent hollow page)
- builder merges page-level visual warnings into ingest_warnings
"""
import pytest

from core.db.system_db import get_system_db
from core.services import model_config


@pytest.fixture()
def clean_model_config(monkeypatch):
    """Fresh registry state: no DB overrides, no env interference."""
    db = get_system_db()
    for f in model_config._FIELDS:
        db.set_config(model_config._DB_PREFIX + f, "")
    for var in ("OPENLAD_LLM_URL", "OPENLAD_LLM_API_KEY", "OPENLAD_LLM_MODEL",
                "OPENLAD_EMB_URL", "OPENLAD_EMB_API_KEY", "OPENLAD_EMB_MODEL",
                "OPENLAD_OCR_URL", "OPENLAD_OCR_API_KEY", "OPENLAD_OCR_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield db
    for f in model_config._FIELDS:
        db.set_config(model_config._DB_PREFIX + f, "")


def _real_pil_image():
    """Return real PIL.Image or skip. In the CI minimal venv (no Pillow),
    test_embedding_failure_visibility injects a stub into sys.modules —
    importorskip would "succeed" on the stub, so check for a real API."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")
    if not hasattr(Image, "new"):
        pytest.skip("PIL.Image stubbed (Pillow absent in this env)")
    return Image


class TestOcrConfigChain:
    def test_ocr_disabled_by_default(self, clean_model_config):
        cfg = model_config.get_model_settings()
        assert cfg["ocr_url"] == ""
        assert cfg["ocr_model"] == ""

    def test_env_enables_ocr(self, clean_model_config, monkeypatch):
        monkeypatch.setenv("OPENLAD_OCR_URL", "http://127.0.0.1:8082/v1")
        monkeypatch.setenv("OPENLAD_OCR_MODEL", "ovisocr2")
        cfg = model_config.get_model_settings()
        assert cfg["ocr_url"] == "http://127.0.0.1:8082/v1"
        assert cfg["ocr_model"] == "ovisocr2"
        assert cfg["ocr_api_key"] == "123"  # local placeholder default

    def test_db_beats_env(self, clean_model_config, monkeypatch):
        monkeypatch.setenv("OPENLAD_OCR_URL", "http://env-ocr/v1")
        clean_model_config.set_config("model_ocr_url", "http://db-ocr/v1")
        assert model_config.get_model_settings()["ocr_url"] == "http://db-ocr/v1"

    def test_public_view_includes_ocr_fields(self, clean_model_config, monkeypatch):
        monkeypatch.setenv("OPENLAD_OCR_URL", "http://127.0.0.1:8082/v1")
        view = model_config.public_view(model_config.get_model_settings())
        assert view["ocr_url"] == "http://127.0.0.1:8082/v1"
        assert view["ocr_api_key"]["set"] is True
        assert view["ocr_api_key"]["hint"].startswith("...")


def _make_client(monkeypatch, ocr_url="", ocr_model="", ocr_key="123"):
    """Build a ModelClient with a stubbed settings registry."""
    from core.models import client as client_mod

    fake = {
        "llm_url": "http://llm/v1", "llm_api_key": "llm-key", "llm_model": "llm-m",
        "emb_url": "http://emb/v1", "emb_api_key": "123", "emb_model": "emb-m",
        "ocr_url": ocr_url, "ocr_api_key": ocr_key, "ocr_model": ocr_model,
    }
    monkeypatch.setattr(
        "core.services.model_config.get_model_settings", lambda: dict(fake))
    return client_mod.ModelClient()


class _Recorder:
    """Capture the routing args _chat_completion receives."""

    def __init__(self):
        self.calls = []

    def __call__(self, messages, max_tokens=2048, temperature=0.7,
                 json_mode=False, json_array_mode=False,
                 base_url=None, model=None, api_key=None):
        self.calls.append({"base_url": base_url, "model": model, "api_key": api_key})
        return "ok"


class TestVisionEndpointRouting:
    def test_auto_falls_back_to_llm_when_unconfigured(self, monkeypatch, tmp_path):
        client = _make_client(monkeypatch)  # no ocr_url
        assert client.ocr_endpoint_available is False
        rec = _Recorder()
        monkeypatch.setattr(client, "_chat_completion", rec)
        img = tmp_path / "p.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        # generate_with_image needs PIL to open the file; stub the image load
        # by letting it fail gracefully is not acceptable — provide real PNG.
        Image = _real_pil_image()
        Image.new("RGB", (8, 8), "white").save(img)
        assert client.generate_with_image("p", str(img)) == "ok"
        assert rec.calls == [{"base_url": None, "model": None, "api_key": None}]

    def test_auto_routes_to_ocr_when_configured(self, monkeypatch, tmp_path):
        client = _make_client(monkeypatch, ocr_url="http://ocr/v1",
                              ocr_model="ovisocr2", ocr_key="ocr-key")
        assert client.ocr_endpoint_available is True
        rec = _Recorder()
        monkeypatch.setattr(client, "_chat_completion", rec)
        Image = _real_pil_image()
        img = tmp_path / "p.png"
        Image.new("RGB", (8, 8), "white").save(img)
        assert client.generate_with_image("p", str(img)) == "ok"
        assert rec.calls == [{"base_url": "http://ocr/v1", "model": "ovisocr2",
                              "api_key": "ocr-key"}]

    def test_forced_llm_bypasses_ocr(self, monkeypatch, tmp_path):
        client = _make_client(monkeypatch, ocr_url="http://ocr/v1", ocr_model="ovisocr2")
        rec = _Recorder()
        monkeypatch.setattr(client, "_chat_completion", rec)
        Image = _real_pil_image()
        img = tmp_path / "p.png"
        Image.new("RGB", (8, 8), "white").save(img)
        client.generate_with_image("p", str(img), endpoint="llm")
        assert rec.calls[0]["base_url"] is None

    def test_forced_ocr_without_config_falls_back(self, monkeypatch, tmp_path):
        client = _make_client(monkeypatch)  # no ocr_url
        rec = _Recorder()
        monkeypatch.setattr(client, "_chat_completion", rec)
        Image = _real_pil_image()
        img = tmp_path / "p.png"
        Image.new("RGB", (8, 8), "white").save(img)
        client.generate_with_image("p", str(img), endpoint="ocr")
        assert rec.calls == [{"base_url": None, "model": None, "api_key": None}]


class TestParserOcrTranscription:
    def test_transcribe_uses_ocr_endpoint(self, monkeypatch):
        from core.ingestion.parser import DocumentParser

        calls = {}

        class FakeClient:
            ocr_endpoint_available = True

            def generate_with_image(self, prompt, image_path, system_prompt=None,
                                    max_tokens=2048, temperature=0.3,
                                    max_image_size=1024, endpoint="auto"):
                calls["endpoint"] = endpoint
                calls["max_tokens"] = max_tokens
                return "transcribed text"

        monkeypatch.setattr("core.ingestion.parser.get_model_client",
                            lambda: FakeClient())
        Image = _real_pil_image()
        img = Image.new("RGB", (8, 8), "white")
        parser = DocumentParser.__new__(DocumentParser)
        out = parser._transcribe_pdf_page_with_ocr(img, 3)
        assert out == "transcribed text"
        assert calls["endpoint"] == "ocr"
        assert calls["max_tokens"] > 0

    def test_transcribe_failure_returns_empty(self, monkeypatch):
        from core.ingestion.parser import DocumentParser

        class FailingClient:
            ocr_endpoint_available = True

            def generate_with_image(self, *a, **kw):
                raise RuntimeError("endpoint down")

        monkeypatch.setattr("core.ingestion.parser.get_model_client",
                            lambda: FailingClient())
        Image = _real_pil_image()
        img = Image.new("RGB", (8, 8), "white")
        parser = DocumentParser.__new__(DocumentParser)
        assert parser._transcribe_pdf_page_with_ocr(img, 3) == ""


class TestHotReload:
    def test_reload_model_client_syncs_ocr_attrs(self, monkeypatch):
        """Regression: hot-reload (admin UI save path) must propagate the OCR
        endpoint onto the live singleton — 2026-09-01 integration test caught
        reload only syncing llm/emb, leaving ocr_endpoint_available False."""
        from core.models import client as client_mod

        live_cfg = {
            "llm_url": "http://llm/v1", "llm_api_key": "k", "llm_model": "m",
            "emb_url": "http://emb/v1", "emb_api_key": "k", "emb_model": "m",
            "ocr_url": "", "ocr_api_key": "123", "ocr_model": "",
        }
        monkeypatch.setattr(
            "core.services.model_config.get_model_settings",
            lambda: dict(live_cfg))
        c = client_mod.ModelClient()
        assert c.ocr_endpoint_available is False  # ocr_url empty at construction
        live_cfg.update({"ocr_url": "http://ocr/v1", "ocr_model": "om", "ocr_api_key": "ok"})
        monkeypatch.setattr(client_mod, "_model_client", c)
        assert client_mod.reload_model_client() is True
        assert c.ocr_base_url == "http://ocr/v1"
        assert c.ocr_model == "om"
        assert c.ocr_endpoint_available is True


class TestIngestWarningCollection:
    def test_merge_embed_and_visual_warnings(self):
        from core.ingestion.builder import DocumentIndexBuilder

        merged = DocumentIndexBuilder._collect_ingest_warnings(
            ["chunk 3 embedding failed"],
            {"visual_transcription_warnings": ["page 5: OCR transcription empty or failed"]},
        )
        assert merged == ["chunk 3 embedding failed",
                          "page 5: OCR transcription empty or failed"]

    def test_empty_inputs_yield_empty(self):
        from core.ingestion.builder import DocumentIndexBuilder

        assert DocumentIndexBuilder._collect_ingest_warnings([], None) == []
        assert DocumentIndexBuilder._collect_ingest_warnings(None, {}) == []
