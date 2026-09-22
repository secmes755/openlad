"""OCR degeneration trimming: when the OCR model exhausts the real page
content it can drift into fabrication (fluent off-topic prose, or a numbered
list of near-identical items) instead of stopping. Cleanup must cut the
fabricated tail — including the hallucinated lead-in paragraph that announces
a degenerate list — while never touching legitimate lists, and the
continuation loop must stop instead of appending more fabrication."""
from pathlib import Path
from unittest.mock import patch

from core.ingestion import parser as parser_module
from core.ingestion.parser import DocumentParser, _clean_ocr_transcription
from tests.test_ocr_continuation import FakeOCRClient, _tiny_image

FIXTURE = (Path(__file__).parent / "fixtures"
           / "ocr_p7_degeneration.txt")


def _degenerate_body(i: int) -> str:
    # p7-style drift: same sentence re-typed with an incrementing index.
    return (f"使用音频和音频，使用音频的采样率（如 Audacity, Adobe Audition）"
            f"进行音频和音频，如音频、压、音频等，可以调整音频的采样率，"
            f"以减小误差{i % 3}。")


def _degenerate_list(n: int = 8) -> str:
    return "\n\n".join(f"{i}. {_degenerate_body(i)}" for i in range(1, n + 1))


def test_real_fixture_trims_to_visible_content():
    raw = FIXTURE.read_text()
    cleaned = _clean_ocr_transcription(raw)
    # Real slide content survives.
    assert "RK1820实测表现" in cleaned
    assert "75.30tokens/s" in cleaned
    assert "28.87tokens/s" in cleaned
    # Fabrication is gone: no audio ramble, no numbered loop, no lead-in.
    assert "音频" not in cleaned
    assert "以下是一些常见的" not in cleaned
    assert "\n1. " not in cleaned
    assert len(cleaned) < 300


def test_introducer_colon_line_cut_with_degenerate_list():
    text = ("## 实测数据\n\n指标A 100\n\n指标B 200\n\n"
            "音频的时序关系通常由音频信号决定，这可能会影响最终输出的质量。"
            "以下是一些常见的音频处理方法：\n\n" + _degenerate_list())
    cleaned = _clean_ocr_transcription(text)
    assert "指标A 100" in cleaned
    assert "指标B 200" in cleaned
    assert "音频" not in cleaned
    assert "以下是一些常见的" not in cleaned


def test_legit_numbered_list_survives():
    text = ("支持以下操作系统：\n\n"
            "1. Android 13 with long-term support for OS upgrades\n\n"
            "2. Linux kernel 5.15 LTS with Qualcomm optimizations\n\n"
            "3. Ubuntu 22.04 for development and deployment\n\n"
            "4. Windows 11 IoT Enterprise for industrial PCs")
    assert _clean_ocr_transcription(text) == text


def test_legit_list_with_long_bodies_survives():
    # Distinct long bodies — similarity gate must not fire.
    items = [
        "采用6nm制程工艺，八核Kryo 670 CPU主频最高2.7GHz，面向工业物联网场景",
        "集成Adreno 643 GPU，支持OpenGL ES 3.2、Vulkan 1.1和OpenCL 2.0",
        "搭载第六代AI引擎，总算力可达12 TOPS，支持INT8/INT16混合精度",
        "支持5G毫米波与Sub-6GHz频段，下行3.7Gbps，上行2.5Gbps",
        "Spectra 570L ISP支持最高6400万像素单摄或3600万+2200万双摄",
        "支持Wi-Fi 6E与蓝牙5.2，面向企业级物联网应用优化",
    ]
    text = "主要特性如下：\n\n" + "\n\n".join(
        f"{i}. {b}" for i, b in enumerate(items, 1))
    assert _clean_ocr_transcription(text) == text


def test_continuation_gate_stops_on_degeneration():
    # First call truncates (finish=length) but the content is already
    # degenerate — the gate must skip the continuation entirely.
    degenerate = ("## 真实标题\n\n真实数据 123\n\n"
                  "这段是编造的引入语。以下是一些常见的编造方法：\n\n"
                  + _degenerate_list())
    client = FakeOCRClient([(degenerate, "length"), ("更多编造内容", "length")])
    parser = DocumentParser()
    img_cfg = dict(parser_module.settings.CHART_CONFIG)
    img_cfg["ocr_transcription_max_continuations"] = 2
    with patch.object(parser_module, "get_model_client", return_value=client), \
         patch.object(parser_module.settings, "CHART_CONFIG", img_cfg):
        result = parser._transcribe_pdf_page_with_ocr(_tiny_image(), 7)
    assert len(client.calls) == 1  # continuation never fired
    assert "真实数据 123" in result
    assert "编造" not in result


def test_continuation_proceeds_when_content_clean():
    # Truncated mid-table (no degeneration): continuation still fires.
    client = FakeOCRClient([("| col1 | col2 |\n| a | b |\n| c", "length"),
                            (" | d |\n| e | f |", "stop")])
    parser = DocumentParser()
    img_cfg = dict(parser_module.settings.CHART_CONFIG)
    img_cfg["ocr_transcription_max_continuations"] = 2
    with patch.object(parser_module, "get_model_client", return_value=client), \
         patch.object(parser_module.settings, "CHART_CONFIG", img_cfg):
        result = parser._transcribe_pdf_page_with_ocr(_tiny_image(), 7)
    assert len(client.calls) == 2
    assert result.endswith("| e | f |")
