"""Deadline-aware loopback HTTP, including slow status/header/body drips.

The socket's makefile uses SocketIO -> recv_into; updating that boundary also
covers http.client's buffered status/header parser and chunked body parser.
No request thread/process survives expiry, no DNS/proxy/redirect is allowed.
"""
import http.client
import socket
from urllib.request import HTTPHandler, HTTPRedirectHandler, ProxyHandler, build_opener

from src.runtime.io_budget import BudgetExpired, Deadline


class _BudgetSocket(socket.socket):
    def __init__(self, deadline: Deadline, socket_timeout: float):
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self._deadline = deadline
        self._socket_timeout = socket_timeout

    def _before_io(self):
        self.settimeout(min(self._socket_timeout, self._deadline.remaining()))

    def _perform(self, action, *args):
        self._before_io()
        try:
            result = action(*args)
        except OSError:
            # Distinguish a remaining idle timeout from exhaustion of the tick.
            self._deadline.check()
            raise
        self._deadline.check()
        return result

    def connect(self, address):
        if address[0] != '127.0.0.1':
            raise ValueError('NON_LOOPBACK_BUDGET_TRANSPORT')
        return self._perform(super().connect, address)

    def recv_into(self, buffer, nbytes=0, flags=0):
        return self._perform(super().recv_into, buffer, nbytes, flags)

    def recv(self, buffersize, flags=0):
        return self._perform(super().recv, buffersize, flags)

    def sendall(self, data, flags=0):
        view = memoryview(data)
        while view:
            sent = self._perform(super().send, view, flags)
            if not sent:
                raise ConnectionError('CONNECTION_CLOSED')
            view = view[sent:]


class _BudgetHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *, deadline, socket_timeout, **kwargs):
        super().__init__(host, **kwargs)
        self._deadline = deadline
        self._socket_timeout = socket_timeout

    def connect(self):
        if self.host != '127.0.0.1' or self._tunnel_host is not None:
            raise ValueError('NON_LOOPBACK_BUDGET_TRANSPORT')
        sock = _BudgetSocket(self._deadline, self._socket_timeout)
        try:
            sock.connect((self.host, self.port))
        except BaseException:
            sock.close()
            raise
        self.sock = sock


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class _BudgetHTTPHandler(HTTPHandler):
    def __init__(self, deadline, socket_timeout):
        super().__init__()
        self.deadline, self.socket_timeout = deadline, socket_timeout

    def http_open(self, request):
        def connection(host, **kwargs):
            return _BudgetHTTPConnection(host, deadline=self.deadline,
                                         socket_timeout=self.socket_timeout, **kwargs)
        return self.do_open(connection, request)


def build_deadline_opener(deadline: Deadline, socket_timeout: float):
    deadline.check()
    return build_opener(ProxyHandler({}), _NoRedirect(),
                        _BudgetHTTPHandler(deadline, socket_timeout))
