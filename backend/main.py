"""Flask entrypoint — use create_app() for Phase 2 safety bootstrap."""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from backend.app_factory import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
