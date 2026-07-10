"""Entry point: `python -m maps_mcp` runs the server over streamable HTTP,
ready to sit behind the Cloudflare Tunnel / Zero Trust / OAuth Worker stack.
"""
import os

from .server import mcp


def main() -> None:
    port = int(os.environ.get("PORT", "3000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
