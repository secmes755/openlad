"""#22: generate_with_image must not flatten failures into "".

The vision call wrapped everything — image loading, encoding, endpoint
routing — in one blanket except that logged "Image parsing failed" and
returned "". Callers could not distinguish a corrupt image from the model
returning no content, and the log message mislabeled endpoint-stage bugs
as image problems.

Contract after the fix:
- image load/encode failures raise RuntimeError with the real reason
- a missing PIL raises RuntimeError
- the endpoint layer keeps its existing contract: "" means the endpoint
  returned no content (permanent rejections are logged loudly there)
"""
import sys

import pytest

from core.models import client as client_mod


@pytest.fixture
def client():
    return client_mod.ModelClient()


def test_unreadable_image_raises(client, tmp_path):
    missing = tmp_path / "gone.png"
    with pytest.raises(RuntimeError, match="Image load/encode failed"):
        client.generate_with_image("describe", str(missing), endpoint="llm")


def test_missing_pil_raises(client, tmp_path, monkeypatch):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(RuntimeError, match="PIL"):
        client.generate_with_image("describe", str(img), endpoint="llm")


def test_valid_image_reaches_endpoint(client, tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    img = tmp_path / "ok.png"
    Image.new("RGB", (8, 8), "white").save(img)
    monkeypatch.setattr(client, "_chat_completion",
                        lambda *a, **k: "transcribed text")
    out = client.generate_with_image("describe", str(img), endpoint="llm")
    assert out == "transcribed text"


def test_endpoint_empty_string_passthrough(client, tmp_path, monkeypatch):
    """"" is a legitimate endpoint result (permanent rejection / no content);
    the endpoint layer logs those loudly — the vision wrapper must not
    convert them into exceptions."""
    pytest.importorskip("PIL")
    from PIL import Image
    img = tmp_path / "ok.png"
    Image.new("RGB", (8, 8), "white").save(img)
    monkeypatch.setattr(client, "_chat_completion", lambda *a, **k: "")
    assert client.generate_with_image("describe", str(img), endpoint="llm") == ""
