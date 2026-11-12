"""The quickstart, runtime fallbacks and deployment docs must use one alias."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
QUERY_ALIAS = 'qwen3.6-35b-a3b'


def test_query_alias_is_consistent_across_entrypoints():
    files = [
        '.env.example', 'docker/.env.example', 'start.sh',
        'docker-compose.yml', 'scripts/docker_verify.sh',
        'README.md', 'README_zh-CN.md',
    ]
    for name in files:
        text = (ROOT / name).read_text()
        assert QUERY_ALIAS in text, name
        assert 'qwen3.5-9b' not in text, name


def test_deployment_guides_describe_both_staged_profiles():
    for name in ('docs/deployment.md', 'docs/zh-CN/deployment.md'):
        text = (ROOT / name).read_text()
        assert 'Qwen3.6-35B-A3B' in text
        assert 'Qwen3.5-9B' in text
        assert '--ctx-size' in text and '32768' in text
        assert '--parallel' in text and '`1`' in text
        assert 'OCR' in text


def test_compose_file_remains_valid_yaml():
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    environment = compose['services']['openlad']['environment']
    assert QUERY_ALIAS in environment['OPENLAD_LLM_MODEL']
    assert environment['OPENLAD_QUERY_CONCURRENCY_MODE'].endswith('serial}')
