"""BUG-5: chart crops must land in the calling ingest's tenant directory.

The builder holds ONE shared ChartAnalyzer (app.state.builder singleton)
and mutated ``self.chart_analyzer.images_dir`` at the start of every
ingest. Two concurrent ingests for different tenants race: tenant A's
charts can be written into tenant B's directory — a cross-tenant data
leak, not just a naming conflict.

Fix: analyze_page/_analyze_region take images_dir as a per-call
parameter; the builder passes the current ingest's tenant directory and
never mutates the shared instance.
"""
import pytest
from PIL import Image

from core.ingestion.layout.chart_analyzer import ChartAnalyzer


class _StubClient:
    """No quick_chart_detect; generate_with_image returns empty so
    _analyze_region bails AFTER saving the crop (save happens first)."""
    def generate_with_image(self, *a, **k):
        return ""


@pytest.fixture
def analyzer():
    a = ChartAnalyzer(config={})
    a.model_client = _StubClient()
    return a


def _page_and_region():
    img = Image.new("RGB", (100, 100), "white")
    region = {"bbox": (10, 10, 90, 90), "type": "chart", "caption": ""}
    return img, region


def test_images_dir_param_overrides_instance_dir(analyzer, tmp_path):
    instance_dir = tmp_path / "tenant_stale"
    param_dir = tmp_path / "tenant_current"
    instance_dir.mkdir()
    param_dir.mkdir()
    analyzer.images_dir = instance_dir  # what a RACING ingest would have set

    img, region = _page_and_region()
    analyzer._analyze_region(img, region, "", "doc1", 1, 0,
                             images_dir=param_dir)

    assert (param_dir / "doc1_p1_chart0.png").exists()
    assert not (instance_dir / "doc1_p1_chart0.png").exists()


def test_images_dir_param_none_falls_back_to_instance(analyzer, tmp_path):
    analyzer.images_dir = tmp_path
    img, region = _page_and_region()
    analyzer._analyze_region(img, region, "", "doc2", 3, 1)
    assert (tmp_path / "doc2_p3_chart1.png").exists()


def test_analyze_page_threads_param_through(analyzer, tmp_path, monkeypatch):
    """analyze_page must forward images_dir to _analyze_region."""
    captured = {}
    real = analyzer._analyze_region
    def spy(page_image, region, page_text, doc_id, page_num, region_idx,
            **kw):
        captured.update(kw)
        return real(page_image, region, page_text, doc_id, page_num,
                    region_idx, **kw)
    monkeypatch.setattr(analyzer, "_analyze_region", spy)
    monkeypatch.setattr(analyzer, "_detect_regions",
                        lambda img, layout: [{"bbox": (10, 10, 90, 90),
                                              "type": "chart"}])

    class _Layout:
        page_type = "image_page"

    analyzer.analyze_page(Image.new("RGB", (100, 100)), _Layout(), "",
                          "doc3", 1, images_dir=tmp_path)
    assert captured.get("images_dir") == tmp_path
    assert (tmp_path / "doc3_p1_chart0.png").exists()
