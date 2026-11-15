"""Запуск: python -m backend"""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.app:app",
        host="127.0.0.1",
        port=int(os.environ.get("PARSER_PORT", "8777")),
        log_level="info",
    )
