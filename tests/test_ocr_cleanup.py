"""Unit tests for OCR transcription degeneration cleanup.

Small OCR models can fall into tail repetition loops once the real page
content is exhausted. The cleanup helpers in core.ingestion.parser trim
two degeneration shapes: exact periodic repeats (digit/word runs) and
numbered pseudo-repeats (the same sentence re-typed with an incrementing
index). Legitimate content must never be touched.
"""
from core.ingestion.parser import (
    _clean_ocr_transcription,
    _trim_exact_period_repeats,
    _trim_numbered_pseudo_repeats,
    clean_ocr_transcription_detailed,
)


class TestExactPeriodRepeats:
    def test_trims_long_digit_run_tail(self):
        text = "标题\n\n正文内容若干。\n\n" + "878" * 700  # > min_text_len
        out = _trim_exact_period_repeats(text)
        assert "878878" not in out
        assert "正文内容若干" in out

    def test_short_text_below_minimum_untouched(self):
        text = "短文本" + "ab" * 10  # below min_text_len
        assert _trim_exact_period_repeats(text) == text

    def test_no_repeat_untouched(self):
        text = "正常段落" * 200  # long enough, but no period tail
        assert _trim_exact_period_repeats(text) == text


class TestNumberedPseudoRepeats:
    # Observed degeneration shape: real blocks on their own lines, then a
    # fabricated lead-in line ending with a list colon, then the numbered
    # repeat loop.
    DEGEN = (
        "## RK1820实测\n\n正文内容。\n\n"
        "这段是编造的引入语。以下是一些常见的方法：\n\n"
        + "\n\n".join(f"{i}. 音频采样和采样，使用音频采样和采样"
                     f"（如 Audacity, Adobe Audition 等）进行音频采样处理，"
                     f"可以减少采样频率。" for i in range(1, 20))
        + "\n"
    )

    def test_trims_numbered_repeat_block(self):
        out = _trim_numbered_pseudo_repeats(self.DEGEN)
        assert len(out) < len(self.DEGEN)
        assert "正文内容" in out
        assert "20." not in out  # no surviving repeat lines
        # The fabricated introducer line is cut together with its list.
        assert "以下是一些常见的方法" not in out

    def test_same_line_introducer_fully_cut(self):
        # Trade-off: real content sharing a line with a fabricated colon
        # clause cannot be told apart from a fabricated lead-in — the whole
        # line is cut. OCR separates visual blocks with newlines, so this
        # shape is virtually always pure fabrication.
        text = ("## 标题\n\n混在同一行的内容。以下是一些方法：\n\n"
                + "\n\n".join(f"{i}. " + "重复内容句子" * 8 for i in range(1, 6)))
        out = _trim_numbered_pseudo_repeats(text)
        assert "以下是一些方法" not in out
        assert "混在同一行的内容" not in out

    def test_handles_truncated_final_line(self):
        text = self.DEGEN + "20. 音频采样和采样，使用音频采样和采样"  # cut mid-sentence
        out = _trim_numbered_pseudo_repeats(text)
        assert len(out) < len(text)

    def test_legitimate_list_untouched(self):
        legit = (
            "## 步骤\n\n"
            "1. 打开电源开关，确认指示灯亮起。\n\n"
            "2. 连接网线到路由器 LAN 口。\n\n"
            "3. 在浏览器输入管理地址进入配置页。\n\n"
            "4. 保存设置并重启设备。\n\n"
            "5. 完成安装。"
        )
        assert _trim_numbered_pseudo_repeats(legit) == legit


class TestCleanOCRTranscription:
    def test_empty_input(self):
        assert _clean_ocr_transcription("") == ""

    def test_plain_text_unchanged(self):
        text = "## 标题\n\n普通段落内容若干，不含重复退化。"
        assert _clean_ocr_transcription(text) == text

    def test_both_degeneration_shapes_removed(self):
        text = ("正文部分。\n\n"
                + "\n\n".join(f"{i}. 音频采样和采样，使用音频采样和采样进行"
                             f"音频采样处理，可以减少采样频率。"
                             for i in range(1, 12))
                + "\n\n" + "878" * 700)
        out = _clean_ocr_transcription(text)
        assert len(out) < 500
        assert "正文部分" in out

    def test_numbered_loop_followed_by_digit_tail(self):
        # Regression: degeneration switching shape mid-tail. The exact-period
        # pass leaves a one-unit stub below the numbered loop; that stub must
        # not shield the numbered lines above it from trimming.
        text = ("正文部分。\n\n"
                + "\n\n".join(f"{i}. 音频采样和采样，使用音频采样和采样"
                             f"（如 Audacity, Adobe Audition 等）进行音频采样处理，"
                             f"可以减少采样频率。"
                             for i in range(1, 20))
                + "\n\n" + "878" * 700)
        out = _clean_ocr_transcription(text)
        assert "正文部分" in out
        assert "878878" not in out
        assert "19. 音频采样" not in out
        assert len(out) < 300

    def test_legitimate_list_with_short_tail_stub_untouched(self):
        legit = (
            "## 步骤\n\n"
            "1. 打开电源开关，确认指示灯亮起。\n\n"
            "2. 连接网线到路由器 LAN 口。\n\n"
            "3. 在浏览器输入管理地址进入配置页。\n\n"
            "4. 保存设置并重启设备。\n\n"
            "5. 完成安装。\n\n"
            "878"  # stray short stub after a legitimate list
        )
        assert _clean_ocr_transcription(legit) == legit


class TestDetailedReport:
    """The cleanup runs unconditionally on every OCR page, so a page that
    merely *looks* degenerative can be shortened with no log and no degraded
    flag. ``clean_ocr_transcription_detailed`` reports what was removed —
    including the case where a legitimate repetitive page (dimension drawing,
    pin table) is mis-fired on — so the caller can surface it.
    """

    def test_no_trim_reports_zero(self):
        text = "## 标题\n\n普通段落内容若干，不含重复退化。"
        out, info = clean_ocr_transcription_detailed(text)
        assert out == text
        assert info == {"trimmed_chars": 0, "passes": []}

    def test_empty_input(self):
        out, info = clean_ocr_transcription_detailed("")
        assert out == ""
        assert info["trimmed_chars"] == 0

    def test_exact_period_pass_named(self):
        text = "标题\n\n正文内容若干。\n\n" + "878" * 700
        out, info = clean_ocr_transcription_detailed(text)
        assert info["trimmed_chars"] == len(text) - len(out) > 0
        assert info["passes"] == ["exact_period"]

    def test_numbered_pass_named(self):
        # Long, template-shared bodies — the shape observed on real dimension
        # pages (see test_misfire_on_legitimate_repetitive_content_is_reported).
        text = ("正文部分。\n\n"
                + "\n".join(f"{i}. DIMENSION_SYM_{i} NOMINAL 322.6 MAX 322.7"
                           for i in range(10, 120)))
        out, info = clean_ocr_transcription_detailed(text)
        assert info["trimmed_chars"] > 0
        assert info["passes"] == ["numbered_pseudo"]

    def test_misfire_on_legitimate_repetitive_content_is_reported(self):
        # The regression that motivated this report: a dimension-symbol table
        # whose bodies are all distinct but share a template is judged
        # pseudo-repetition and cut. The report is what makes that visible.
        head = ("# Mechanical Dimensions\n\n"
                "This section describes the package outline and reference "
                "dimensions. All dimensions are in millimetres. The following "
                "list gives each symbol with its nominal and maximum value.\n\n")
        table = "\n".join(f"{i}. DIMENSION_SYM_{i} NOMINAL 322.6 MAX 322.7"
                          for i in range(10, 120))
        out, info = clean_ocr_transcription_detailed(head + table)
        assert info["trimmed_chars"] > 0, "the mis-fire must be reported"
        assert "numbered_pseudo" in info["passes"]
        assert "DIMENSION_SYM_119" not in out, "content was in fact lost"
