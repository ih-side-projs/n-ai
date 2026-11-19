"""Server entry point for WP8."""

from __future__ import annotations

import os
import uvicorn

from .app import create_app


def main():
    db_path = os.environ.get("NAI_DB_PATH", ".nai/nai.sqlite3")
    host = os.environ.get("NAI_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("NAI_WEB_PORT", "8080"))

    app = create_app(db_path)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()