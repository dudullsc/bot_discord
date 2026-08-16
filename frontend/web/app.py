"""Flask app: invite the music bot and show management status."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
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

# Pedidas na tela de autorização do Discord. O servidor precisa conceder.
VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
READ_MESSAGE_HISTORY = 1 << 16
CONNECT = 1 << 20
SPEAK = 1 << 21
USE_VAD = 1 << 25
ADD_REACTIONS = 1 << 6
USE_APP_COMMANDS = 1 << 31
BOT_PERMISSIONS = (
    VIEW_CHANNEL
    | SEND_MESSAGES
    | EMBED_LINKS
    | READ_MESSAGE_HISTORY
    | CONNECT
    | SPEAK
    | USE_VAD
    | ADD_REACTIONS
    | USE_APP_COMMANDS
)
OAUTH_SCOPES = "bot applications.commands"

app = Flask(__name__)


@app.context_processor
def inject_template_globals() -> dict:
    return {"current_year": datetime.now().year}


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


def _guild_profile(guild_id: str) -> dict[str, str | None]:
    """Resolve a friendly guild name/icon from Discord when the bot token is set."""
    name = "seu servidor"
    icon_url = None
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "seu_token_aqui":
        return {"name": name, "icon_url": icon_url}

    req = urllib.request.Request(
        f"https://discord.com/api/v10/guilds/{guild_id}",
        headers={"Authorization": f"Bot {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return {"name": name, "icon_url": icon_url}

    name = (data.get("name") or name).strip() or name
    icon = data.get("icon")
    if icon:
        icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon}.png"
    return {"name": name, "icon_url": icon_url}


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
            title="Convite indisponível",
            message="O convite está temporariamente indisponível. Tente novamente em instantes.",
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
                "Não foi possível identificar o servidor. "
                "Tente adicionar o bot novamente."
            ),
        ), 400

    profile = _guild_profile(guild_id)
    try:
        db.upsert_guild(
            guild_id=guild_id,
            name=profile["name"] or "seu servidor",
            member_count=None,
            icon_url=profile["icon_url"],
        )
    except Exception:
        pass

    guild_name = profile["name"]
    if guild_name == "seu servidor":
        connected = "O bot já está no seu servidor."
    else:
        connected = f"O bot já está no servidor {guild_name}."

    return render_template(
        "result.html",
        ok=True,
        title="Bot adicionado",
        message=connected,
        guild_name=guild_name,
        home_url=url_for("dashboard"),
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


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
    host = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = int(os.getenv("WEB_PORT", "8080") or "8080")
    try:
        db.ensure_schema()
    except Exception as exc:
        print(f"Aviso: PostgreSQL ainda nao esta pronto ({exc})")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
