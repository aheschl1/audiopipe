"""Per-call bearer resolution for MCP tools.

Streamable HTTP carries the same ``Authorization: Bearer`` header the /v1
routes use, so every tool is scoped to the token's owner exactly like an
HTTP route. Auth runs on its own short transaction (``authenticate_bearer``)
before the tool opens its working pipe — the same split ``api.deps`` uses.
"""

from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from api.deps import authenticate_bearer
from database.schema.users import User


async def require_user(ctx: Context) -> User:
    """The calling user, or a tool error the client can act on."""
    header = (ctx.headers or {}).get("authorization", "")
    token = header.removeprefix("Bearer ").strip() or None
    user = await authenticate_bearer(token)
    if user is None:
        raise ToolError(
            "a valid 'Authorization: Bearer <token>' header is required "
            "(issue one with the `tokens issue` CLI)"
        )
    return user
