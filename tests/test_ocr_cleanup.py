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
    DEGEN = (
        "## RK1820实测\n\n正文内容。以下是一些常见的方法：\n\n"
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
