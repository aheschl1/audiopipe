"""The MCP server and its ASGI sub-app.

Rides the API process: tools read the repos directly and authenticate per
call with the same bearer tokens as /v1 (see :mod:`api.mcp.auth`). Stateless
streamable HTTP — every POST stands alone, so no session affinity and no
server-side session table.
"""

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

mcp = MCPServer(
    "audio-pipeline",
    instructions=(
        "Query tools over always-on audio recordings: sessions (one capture "
        "run each), their speaker-attributed transcripts, sound-event tags, "
        "and text-described audio search. Times in and out are ISO-8601; "
        "naive inputs are read as UTC. Session-relative positions are "
        "milliseconds from the session's started_at. Start broad with "
        "day_summary or list_sessions, then drill in with get_transcript, "
        "get_timeline and the search tools."
    ),
)


def streamable_app() -> Starlette:
    """The ASGI app serving ``/mcp``.

    ``api.main`` routes the exact path here (a Route, not a Mount: mounting
    would 307 bare ``/mcp`` to ``/mcp/``, an extra round trip per call in
    stateless mode and a break for clients that don't follow redirects), so
    the transport keeps its default ``/mcp`` path. DNS-rebinding protection
    is off for the same reason CORS is wide open: auth is a bearer header,
    never ambient, and the app serves non-localhost Hosts behind the
    reverse proxy.
    """
    return mcp.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )


from api.mcp.tools import search, sessions, speakers  # noqa: E402,F401  (registers the tools)
