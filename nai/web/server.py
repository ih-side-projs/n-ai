"""Server entry point for the web dashboard."""

from __future__ import annotations

import argparse
import os
import uvicorn

from .app import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="nai-web", description="Serve the nai dashboard locally")
    parser.add_argument("--host", default=None, help="Bind address (default env NAI_WEB_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port (default env NAI_WEB_PORT or 8080)")
    parser.add_argument("--db", default=None, help="Store path (default env NAI_DB_PATH or .nai/nai.sqlite3)")
    args = parser.parse_args(argv)

    db_path = args.db or os.environ.get("NAI_DB_PATH", ".nai/nai.sqlite3")
    host = args.host or os.environ.get("NAI_WEB_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("NAI_WEB_PORT", "8080"))

    app = create_app(db_path)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()