"""OCR transcription continuation: when a page transcription hits the token
budget (finish_reason == "length"), the page is re-sent with a resume cue and
outputs are concatenated, up to the configured continuation cap."""
from unittest.mock import patch

from PIL import Image

from core.ingestion import parser as parser_module
from core.ingestion.parser import DocumentParser


class FakeOCRClient:
    """Scripted OCR endpoint double: returns texts in order with the
    matching finish_reason, exactly like ModelClient exposes it."""

    def __init__(self, script):
        # script: list of (text, finish_reason)
        self._script = list(script)
        self.calls = []

    def generate_with_image(self, prompt, image_path, max_tokens, temperature,
                            endpoint):
        self.calls.append(prompt)
        text, finish = self._script.pop(0)
        self.last_finish_reason = finish
        return text


def _tiny_image():
    return Image.new("RGB", (8, 8), "white")


def _transcribe(client, max_continuations=2):
    parser = DocumentParser()
    img_cfg = dict(parser_module.settings.CHART_CONFIG)
    img_cfg["ocr_transcription_max_continuations"] = max_continuations
    with patch.object(parser_module, "get_model_client", return_value=client), \
         patch.object(parser_module.settings, "CHART_CONFIG", img_cfg):
        return parser._transcribe_pdf_page_with_ocr(_tiny_image(), 7)


def test_no_truncation_single_call():
    client = FakeOCRClient([("整页内容", "stop")])
    assert _transcribe(client) == "整页内容"
    assert len(client.calls) == 1


def test_truncated_then_completed_concatenates():
    client = FakeOCRClient([("前半段", "length"), ("后半段", "stop")])
    assert _transcribe(client) == "前半段后半段"
    assert len(client.calls) == 2
    # The continuation call carries the resume cue with the previous tail.
    assert "前半段" in client.calls[1]


def test_still_truncated_after_cap_keeps_partial_and_stops():
    client = FakeOCRClient([("a", "length"), ("b", "length"), ("c", "length")])
    # cap=2 -> 1 initial + 2 continuations, never a 4th call
    assert _transcribe(client) == "abc"
    assert len(client.calls) == 3
    # Last call's finish_reason still signals truncation for the caller's warning.
    assert client.last_finish_reason == "length"


def test_empty_continuation_breaks():
    client = FakeOCRClient([("前半段", "length"), ("", "length")])
    assert _transcribe(client) == "前半段"
    assert len(client.calls) == 2


def test_repeated_continuation_breaks():
    client = FakeOCRClient([("前半段", "length"), ("前半段", "stop")])
    # Model echoed earlier content instead of continuing: stop, don't append.
    assert _transcribe(client) == "前半段"
    assert len(client.calls) == 2


def test_continuations_disabled_restores_old_behaviour():
    client = FakeOCRClient([("前半段", "length")])
    assert _transcribe(client, max_continuations=0) == "前半段"
    assert len(client.calls) == 1
    assert client.last_finish_reason == "length"


def test_first_call_empty_returns_empty():
    client = FakeOCRClient([("", "stop")])
    assert _transcribe(client) == ""
    assert len(client.calls) == 1
