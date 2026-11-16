"""Pytest uses the same pure replay evaluator as the offline CLI."""
from src.evaluation import evaluate_bundle, quality_gates
from scripts.run_evaluation import quality_gates as cli_quality_gates


def test_cli_and_pytest_share_quality_gates():
    assert cli_quality_gates is quality_gates


def test_unmeasured_evaluation_is_incomplete():
    result = evaluate_bundle({'schema_version': '1.0',
                              'dataset': {'id': 'empty-control', 'kind': 'synthetic_control'},
                              'cases': [], 'outcomes': []})
    assert result['verdict'] == 'INCOMPLETE'
    assert quality_gates(result['metrics']) == result['gates'][:11]
