"""Core owns pack composition mechanisms, not language/domain vocabulary."""
from pathlib import Path

from core.config import settings
from core.plugins import PluginRegistry, YAMLIndustryPlugin
from core.retrieval.spec_facts import _merged_spec_terms


def _registry():
    root = Path(__file__).resolve().parent.parent
    return PluginRegistry(scan_dirs=[str(root / 'industries')])


def test_core_has_no_spec_query_vocabulary_and_generic_is_composed():
    assert settings.CONTEXT_CONFIG.get('spec_query_terms') == {}
    terms = _merged_spec_terms(plan={}, industry_hint=None, query_text='')
    assert terms['数量'] == ['count', 'number']
    assert terms['版本'] == ['version', 'protocol', 'support']


def test_report_epoch_pattern_survives_base_overlay_composition():
    registry = _registry()
    composed = registry.compose_with_base(
        registry.get_plugin('sample_semiconductor'))
    # The sample technical pack declares no report epoch; an explicit base-only
    # contract is covered by the planner's all-pack aggregation tests.
    assert composed.retrieval.get_report_epoch_pattern() == ''
    assert registry.get_plugin(
        'sample_enterprise_report').retrieval.get_report_epoch_pattern()


def test_generic_pack_has_no_enterprise_or_schematic_vocabulary():
    registry = _registry()
    generic_rules = registry.get_generic().retrieval.get_entity_stopwords()
    assert '公司' not in generic_rules
    assert '净利润' not in generic_rules
    generic_prompts = registry.get_generic().retrieval.get_retrieval_prompts()
    assert '毛利率' not in generic_prompts.get('derived_rate_patterns', [])
    assert not any('wiring' in rule.lower()
                   for rule in generic_prompts.get('answer_rules', []))


def test_domain_increments_live_in_their_own_packs():
    registry = _registry()
    enterprise = registry.get_plugin('sample_enterprise_report')
    assert '毛利率' in enterprise.retrieval.get_retrieval_prompts()[
        'derived_rate_patterns']
    semiconductor = registry.get_plugin('sample_semiconductor')
    assert any('引脚关系' in rule for rule in
               semiconductor.retrieval.get_retrieval_prompts()['answer_rules'])


def test_unused_yaml_prompt_builder_with_hardcoded_policy_is_removed():
    assert not hasattr(YAMLIndustryPlugin, 'build_prompt')


def test_pack_messages_own_language_selection_and_fallback():
    from core.plugins import render_pack_message
    assert render_pack_message('no_result', '问题') == '根据当前知识库中的文档，未找到相关信息。'
    assert render_pack_message('no_result', 'question') == (
        'No relevant information was found in the available documents.')
    assert render_pack_message('source_excerpts_header', '问题') == '依据原文：'


def test_message_renderer_skips_bad_patterns_and_substitutes_values():
    from core.plugins import render_pack_message

    class Retrieval:
        def get_message_rules(self):
            return {'notice': [
                {'pattern': '[', 'text': 'broken'},
                {'pattern': '', 'text': 'Value: $value'},
            ]}

    assert render_pack_message(
        'notice', 'anything', retrieval=Retrieval(), value=42) == 'Value: 42'


def test_table_and_planner_intent_vocabulary_come_from_packs():
    from core.retrieval.planner import QueryPlanner
    from core.retrieval.synthesizer import AnswerSynthesizer
    synth = object.__new__(AnswerSynthesizer)
    assert synth._requires_table('请用表格回答')
    assert synth._requires_table('use a table')
    assert not synth._requires_table('plain question')
    registry = _registry()
    semiconductor = registry.compose_with_base(
        registry.get_plugin('sample_semiconductor'))
    assert not synth._requires_table('规格表写了什么', semiconductor)
    assert QueryPlanner._is_no_result_reason('知识库中没有相关文档')
    assert not QueryPlanner._is_no_result_reason('ordinary reasoning')
