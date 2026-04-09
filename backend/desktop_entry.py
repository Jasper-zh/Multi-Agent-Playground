from __future__ import annotations

import os

import uvicorn

from app.main import app


def main() -> None:
    host = str(os.getenv("AGENT_PLAYGROUND_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    raw_port = str(os.getenv("AGENT_PLAYGROUND_PORT", "8011")).strip() or "8011"
    try:
        port = int(raw_port)
    except ValueError:
        port = 8011

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
