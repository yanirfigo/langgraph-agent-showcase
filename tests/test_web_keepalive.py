"""The fetch client never reuses a connection: pinned requests share a pool key per IP, not per name."""

from job_copilot.tools.web import make_client


def test_fetch_client_keeps_no_connections_alive():
    with make_client(5.0) as client:
        pool = client._transport._pool  # httpcore.ConnectionPool behind httpx.HTTPTransport
        assert pool._max_keepalive_connections == 0
        assert pool._connections == []
