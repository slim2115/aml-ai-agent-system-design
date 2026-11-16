"""Separate live QA profile; the ordinary hermetic suite remains unchanged.

Python sockets can reach only fixed QA loopback ports. Native driver clients
also enforce their destinations through the validated profile; this is not an
OS firewall, and is not a sandbox for arbitrary untrusted tests or browser code.
"""
import socket
import pytest

from src.qa_profile import get_qa_profile


@pytest.fixture(scope='session')
def qa_profile():
    profile = get_qa_profile()
    if profile is None:
        raise pytest.UsageError('Use qa/tools/run_checks.py with an owned --run-id')
    return profile


@pytest.fixture(autouse=True)
def qa_network_boundary(qa_profile, monkeypatch):
    allowed = {qa_profile.api_port, qa_profile.ui_port, qa_profile.pg_port, qa_profile.kafka_port}
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_create = socket.create_connection

    def check(address):
        if not (isinstance(address, tuple) and len(address) == 2
                and address[0] == '127.0.0.1' and address[1] in allowed):
            raise RuntimeError('QA_NETWORK_TARGET_FORBIDDEN')

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def create(address, *args, **kwargs):
        check(address)
        return original_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
    monkeypatch.setattr(socket, 'create_connection', create)
