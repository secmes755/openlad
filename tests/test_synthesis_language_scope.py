"""Contracts for language-neutral synthesis and evidence-silence discipline."""
import re
from pathlib import Path
from typing import Any, cast

import pytest

from core.plugins import PluginRegistry
from core.retrieval.synthesizer import AnswerSynthesizer

LANGUAGE_RULE = ("Respond entirely in the same language as the user's question. "
                 "Never switch to another language, even when the retrieved content "
                 "or these instructions use a different language.")


def _synth(model=None):
    synth = object.__new__(AnswerSynthesizer)
    if model is not None:
        synth.model_client = model
    return synth


def test_generic_silence_rule_survives_overlay_and_prompt_composition():
    root = Path(__file__).resolve().parent.parent
    registry = PluginRegistry(scan_dirs=[str(root / 'industries')])
    for pack in (registry.get_generic(), registry.compose_with_base(
            registry.get_plugin('sample_semiconductor'))):
        pack = cast(Any, pack)
        rules = pack.retrieval.get_retrieval_prompts().get('answer_rules', [])
        assert any('Absence of a statement is not evidence of a negative fact' in rule
                   for rule in rules)
        addon = _synth()._build_industry_addon(pack)
        assert '文档未提及不等于不支持' in addon


@pytest.mark.parametrize('query', [
    '这是什么？', 'What is this?', '日本語の質問', '한국어 질문',
    'Question en français', '请用中文回答 this mixed question',
])
def test_language_instruction_is_not_a_cjk_latin_split(query):
    instruction = AnswerSynthesizer._get_language_instruction(query)
    assert instruction == LANGUAGE_RULE
    assert '中文助手' not in instruction
    assert 'English assistant' not in instruction


def test_legacy_prompt_separates_language_rule_and_uses_original_query():
    prompt = _synth()._build_legacy_prompt(
        query='English rewrite', context='ctx', catalog='catalog',
        history_section='', structured_context='', original_query='中文原问题')
    assert 'User question: English rewrite' in prompt
    assert f"Answer the user's question directly.\n\n{LANGUAGE_RULE}" in prompt
    assert 'directly.Use the language' not in prompt


def test_legacy_prompt_uses_domain_neutral_structured_data_label():
    prompt = _synth()._build_legacy_prompt(
        query='q', context='ctx', catalog='', history_section='',
        structured_context='A -> B', original_query='q')
    assert 'Structured document data' in prompt
    assert 'Schematic' not in prompt
    assert 'pin-to-net' not in prompt


class _RetryModel:
    def __init__(self, response='同比增长率 = (2 - 1) / 1 = 100%'):
        self.response = response
        self.prompts = []

    def generate(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _retry_synth(model):
    synth = _synth(model)
    synth._derived_rate_patterns = lambda industry_pack: re.compile(r'增长|growth|成長', re.I)
    synth._is_garbled = lambda text: False
    synth._post_process_answer = lambda text: text
    return synth


@pytest.mark.parametrize('query', ['同比增长率是多少？', 'What is the growth rate?', '成長率は？'])
def test_derived_retry_reuses_language_contract(query):
    model = _RetryModel()
    answer = _retry_synth(model)._enforce_derived_figure('2025: 2; 2024: 1', query)
    assert '100%' in answer
    assert len(model.prompts) == 1
    assert f'Question: {query}' in model.prompts[0]
    assert model.prompts[0].endswith(LANGUAGE_RULE)


@pytest.mark.parametrize('answer,query', [
    ('增长率已经给出：100%', '同比增长率是多少？'),
    ('没有数字', '同比增长率是多少？'),
    ('2025: 2; 2024: 1', '数值是多少？'),
])
def test_derived_retry_gates_are_unchanged(answer, query):
    model = _RetryModel()
    result = _retry_synth(model)._enforce_derived_figure(answer, query)
    assert result == answer
    assert model.prompts == []


@pytest.mark.parametrize('response', ['仍然没有百分号', RuntimeError('unavailable')])
def test_derived_retry_failure_keeps_original_answer(response):
    model = _RetryModel(response)
    original = '2025: 2; 2024: 1'
    result = _retry_synth(model)._enforce_derived_figure(original, '同比增长率是多少？')
    assert result == original
