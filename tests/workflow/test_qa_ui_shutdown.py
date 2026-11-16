"""Bounded cooperative UI shutdown; no browser, network or process launch."""
import asyncio
import socket
from types import SimpleNamespace

import pytest

from qa.tools import managed_service


@pytest.fixture(scope='module')
def run_async():
    # Windows creates a loopback socketpair for the event loop's own wakeups.
    # Build that plumbing before function-scoped network denial; the denial
    # remains active throughout every test and every coroutine under test.
    with asyncio.Runner() as runner:
        runner.get_loop()
        yield runner.run


def embedded_server(configuration):
    return SimpleNamespace(_starlette_server=SimpleNamespace(
        _server=SimpleNamespace(config=configuration)))


def test_pending_connection_drain_receives_a_finite_timeout():
    configuration = SimpleNamespace(timeout_graceful_shutdown=None)
    server = embedded_server(configuration)
    managed_service.configure_ui_shutdown(server)
    assert configuration.timeout_graceful_shutdown == 5
    assert configuration.timeout_graceful_shutdown < managed_service.UI_STOP_TIMEOUT_SECONDS < 45


@pytest.mark.parametrize('server', [
    SimpleNamespace(),
    SimpleNamespace(_starlette_server=None),
    SimpleNamespace(_starlette_server=SimpleNamespace(_server=None)),
    embedded_server(None),
    embedded_server(SimpleNamespace()),
])
def test_changed_streamlit_shape_fails_explicitly(server):
    with pytest.raises(RuntimeError, match='QA_UI_SHUTDOWN_CONFIG_UNSUPPORTED'):
        managed_service.configure_ui_shutdown(server)


def test_completed_shutdown_waits_for_owned_server_after_stop_request(run_async):
    with pytest.raises(RuntimeError, match='NETWORK_DISABLED_IN_HERMETIC_TEST'):
        socket.create_connection(('127.0.0.1', 9))
    async def scenario():
        done = asyncio.Event()
        calls = []

        def stop():
            calls.append('stop')
            done.set()

        await managed_service.stop_ui(SimpleNamespace(stop=stop, stopped=done.wait()))
        assert calls == ['stop']

    run_async(scenario())


def test_unfinished_shutdown_is_bounded_and_not_marked_success(monkeypatch, run_async):
    monkeypatch.setattr(managed_service, 'UI_STOP_TIMEOUT_SECONDS', .01)

    async def scenario():
        calls = []
        waiter = asyncio.create_task(asyncio.Event().wait())
        with pytest.raises(RuntimeError, match='QA_UI_SHUTDOWN_TIMEOUT'):
            await managed_service.stop_ui(SimpleNamespace(
                stop=lambda: calls.append('stop'), stopped=waiter))
        assert calls == ['stop']
        assert waiter.cancelled()

    run_async(scenario())


@pytest.mark.parametrize('failure', ['configuration', 'state_record'])
def test_setup_error_after_start_still_requests_cooperative_shutdown(failure, run_async):
    async def scenario():
        calls = []
        stopped = asyncio.Event()

        class Server:
            def __init__(self):
                if failure != 'configuration':
                    self._starlette_server = SimpleNamespace(_server=SimpleNamespace(
                        config=SimpleNamespace(timeout_graceful_shutdown=None)))

            async def start(self):
                calls.append('start')

            def stop(self):
                calls.append('stop')
                stopped.set()

            @property
            def stopped(self):
                return stopped.wait()

        def save(status):
            calls.append(status)
            raise RuntimeError('STATE_RECORD_FAILED')

        expected = ('QA_UI_SHUTDOWN_CONFIG_UNSUPPORTED' if failure == 'configuration'
                    else 'STATE_RECORD_FAILED')
        with pytest.raises(RuntimeError, match=expected):
            await managed_service.serve_ui(Server(), save, lambda: False)
        assert calls[0] == 'start'
        assert calls[-1] == 'stop'
        assert calls.count('stop') == 1

    run_async(scenario())
