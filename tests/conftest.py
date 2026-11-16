"""Hermetic default suite. Legacy resources require explicit integration mode."""
import json
from pathlib import Path
import socket
import pytest


def pytest_addoption(parser):
    parser.addoption('--run-integration', action='store_true', default=False,
                     help='Allow explicitly selected local runtime/legacy tests')


def pytest_collection_modifyitems(config, items):
    legacy = [item for item in items if '/tests/unit/' in item.path.as_posix()]
    if legacy and not config.getoption('--run-integration'):
        raise pytest.UsageError('Legacy tests require explicit --run-integration; default suite tests the new contracts/config/evaluator only.')
    for item in legacy:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def deny_network_by_default(request, monkeypatch):
    if request.config.getoption('--run-integration'):
        return
    def deny(*args, **kwargs):
        raise RuntimeError('NETWORK_DISABLED_IN_HERMETIC_TEST')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)


def require_integration(request):
    if not request.config.getoption('--run-integration'):
        raise pytest.UsageError('Runtime fixture requires --run-integration')


@pytest.fixture(scope='session')
def golden_dataset():
    path = Path(__file__).parent / 'golden_dataset.json'
    cases = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(cases, list) or not cases:
        raise ValueError('Golden dataset is missing or empty; cannot skip acceptance')
    return cases


@pytest.fixture(scope='session')
def knowledge_base(request):
    require_integration(request)
    from src.data_access.rule_repository import RuleRepository
    from src.retrieval import KnowledgeBase
    kb = KnowledgeBase(RuleRepository().find_all())
    kb.index()
    return kb


@pytest.fixture(scope='session')
def transaction_repository(request):
    require_integration(request)
    from src.data_access import TransactionRepository
    return TransactionRepository()


@pytest.fixture(scope='session')
def agent(request):
    require_integration(request)
    from src.agent import AMLAgent
    return AMLAgent()
