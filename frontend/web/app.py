"""Flask app: invite the music bot and show management status."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for

# frontend/web/app.py → project root
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from bot import db  # noqa: E402

ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

# Send Messages | Embed Links | Connect | Speak | Use Voice Activity
BOT_PERMISSIONS = 36_718_592
OAUTH_SCOPES = "bot applications.commands"

app = Flask(__name__)


def _reload_env() -> None:
    load_dotenv(ENV_PATH, override=True)


def _client_id() -> str:
    _reload_env()
    return os.getenv("DISCORD_CLIENT_ID", "").strip()


def _redirect_uri() -> str:
    _reload_env()
    return os.getenv(
        "DISCORD_REDIRECT_URI",
        "http://127.0.0.1:8080/callback",
    ).strip()


def build_invite_url() -> str | None:
    client_id = _client_id()
    if not client_id:
        return None
    query = urlencode(
        {
            "client_id": client_id,
            "permissions": BOT_PERMISSIONS,
            "scope": OAUTH_SCOPES,
            "response_type": "code",
            "redirect_uri": _redirect_uri(),
        }
    )
    return f"https://discord.com/api/oauth2/authorize?{query}"


def update_guild_id(guild_id: str) -> None:
    """Set or replace DISCORD_GUILD_ID in the project .env file."""
    key = "DISCORD_GUILD_ID"
    line = f"{key}={guild_id}\n"

    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8")
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}={guild_id}", text)
            if not text.endswith("\n"):
                text += "\n"
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += line
        ENV_PATH.write_text(text, encoding="utf-8")
    else:
        ENV_PATH.write_text(line, encoding="utf-8")

    os.environ[key] = guild_id


@app.get("/")
def index():
    invite_url = build_invite_url()
    status = _safe_status()
    return render_template(
        "index.html",
        invite_ready=invite_url is not None,
        redirect_uri=_redirect_uri(),
        status=status,
        uptime_label=db.format_uptime(status.get("uptime_seconds", 0)),
    )


@app.get("/dashboard")
def dashboard():
    status = _safe_status()
    return render_template(
        "dashboard.html",
        status=status,
        uptime_label=db.format_uptime(status.get("uptime_seconds", 0)),
        invite_ready=build_invite_url() is not None,
    )


@app.get("/api/status")
def api_status():
    return jsonify(_safe_status())


@app.get("/invite")
def invite():
    invite_url = build_invite_url()
    if invite_url is None:
        return render_template(
            "result.html",
            ok=False,
            title="Configuração incompleta",
            message=(
                "Defina DISCORD_CLIENT_ID no arquivo .env "
                "(ID do aplicativo no Developer Portal)."
            ),
        ), 400
    return redirect(invite_url)


@app.get("/callback")
def callback():
    error = request.args.get("error")
    if error:
        desc = request.args.get("error_description", error)
        return render_template(
            "result.html",
            ok=False,
            title="Autorização cancelada",
            message=desc,
        ), 400

    guild_id = (request.args.get("guild_id") or "").strip()
    if not guild_id.isdigit():
        return render_template(
            "result.html",
            ok=False,
            title="Servidor não identificado",
            message=(
                "O Discord não enviou um guild_id válido. "
                "Confirme o Redirect em OAuth2 e tente de novo."
            ),
        ), 400

    update_guild_id(guild_id)
    try:
        db.upsert_guild(
            guild_id=guild_id,
            name=f"Servidor {guild_id}",
            member_count=None,
            icon_url=None,
        )
    except Exception:
        pass

    return render_template(
        "result.html",
        ok=True,
        title="Bot adicionado",
        message=(
            f"Servidor conectado. DISCORD_GUILD_ID={guild_id} foi salvo no .env. "
            "Se o bot já estiver rodando, ele aparece no painel em instantes."
        ),
        guild_id=guild_id,
        home_url=url_for("dashboard"),
    )


def _safe_status() -> dict:
    try:
        return db.get_status()
    except Exception as exc:
        return {
            "online": False,
            "bot_username": None,
            "bot_user_id": None,
            "started_at": None,
            "last_heartbeat": None,
            "uptime_seconds": 0,
            "guild_count": 0,
            "database": db._safe_db_label(),
            "guilds": [],
            "error": str(exc),
        }


def create_app() -> Flask:
    return app


def main() -> None:
    host = os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("WEB_PORT", "8080") or "8080")
    try:
        db.ensure_schema()
    except Exception as exc:
        print(f"Aviso: PostgreSQL ainda nao esta pronto ({exc})")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
