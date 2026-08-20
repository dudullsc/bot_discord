"""Discord music bot entrypoint."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import discord
import wavelink
from discord.ext import commands, tasks
from dotenv import load_dotenv

from bot import db

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

logger = logging.getLogger("bot")


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self._lavalink_ready = False
        self._guild_commands_synced = False

    async def setup_hook(self) -> None:
        await asyncio.to_thread(db.ensure_schema)

        host = os.getenv("LAVALINK_HOST", "127.0.0.1")
        port = os.getenv("LAVALINK_PORT", "2333")
        password = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
        uri = f"http://{host}:{port}"

        nodes = [
            wavelink.Node(
                uri=uri,
                password=password,
                inactive_player_timeout=180,
            )
        ]
        await wavelink.Pool.connect(nodes=nodes, client=self, cache_capacity=100)

        await self.load_extension("bot.cogs.music")
        await self._sync_commands()

    async def _sync_commands(self, guild: discord.abc.Snowflake | None = None) -> None:
        try:
            if guild is None:
                synced = await self.tree.sync()
                logger.info("Synced %s global slash command(s)", len(synced))
                return
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %s slash command(s) to guild %s",
                len(synced),
                getattr(guild, "id", guild),
            )
        except discord.Forbidden:
            logger.warning(
                "Sem permissão para sincronizar comandos em %s",
                getattr(guild, "id", guild),
            )
        except Exception:
            logger.exception("Falha ao sincronizar slash commands")

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info("Logged in as %s (%s)", self.user, self.user.id)

        if not self._guild_commands_synced:
            self._guild_commands_synced = True
            for guild in self.guilds:
                await self._sync_commands(guild)

        await self._persist_presence(initial=True)
        if not self.heartbeat_loop.is_running():
            self.heartbeat_loop.start()

    async def close(self) -> None:
        if self.heartbeat_loop.is_running():
            self.heartbeat_loop.cancel()
        try:
            await asyncio.to_thread(db.mark_offline)
        except Exception:
            logger.exception("Failed to mark bot offline in database")
        await super().close()
        await asyncio.to_thread(db.close_pool)

    @tasks.loop(seconds=30)
    async def heartbeat_loop(self) -> None:
        await self._persist_presence(initial=False)

    @heartbeat_loop.before_loop
    async def before_heartbeat(self) -> None:
        await self.wait_until_ready()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await asyncio.to_thread(
            db.upsert_guild,
            guild_id=str(guild.id),
            name=guild.name,
            member_count=guild.member_count,
            icon_url=str(guild.icon.url) if guild.icon else None,
        )
        await self._sync_commands(guild)
        await self._persist_presence(initial=False)
        await self._ask_voice_permissions(guild)

    async def _ask_voice_permissions(self, guild: discord.Guild) -> None:
        me = guild.me
        if me is None:
            return
        text = (
            "Para eu tocar música, este servidor precisa me dar permissão de "
            "**Ver canal**, **Conectar** e **Falar** nos canais de voz "
            "(também nos privados). Quem adicionou o bot pode conferir em "
            "Configurações do servidor → Cargos → music."
        )
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(me).send_messages:
            channel = next(
                (
                    ch
                    for ch in guild.text_channels
                    if ch.permissions_for(me).send_messages
                ),
                None,
            )
        if channel is None:
            return
        try:
            await channel.send(text)
        except discord.Forbidden:
            logger.warning("Sem permissão para pedir cargos em %s", guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await asyncio.to_thread(db.deactivate_guild, str(guild.id))
        await self._persist_presence(initial=False)

    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        logger.info("Lavalink node ready: %r | resumed=%s", payload.node, payload.resumed)
        if self._lavalink_ready and not payload.resumed:
            logger.warning("Lavalink reiniciou — limpando conexões de voz antigas")
            for guild in self.guilds:
                player = guild.voice_client
                if isinstance(player, wavelink.Player):
                    try:
                        await player.disconnect(force=True)
                    except Exception:
                        logger.exception(
                            "Failed to disconnect stale player in guild %s",
                            guild.id,
                        )
        self._lavalink_ready = True

    async def _persist_presence(self, *, initial: bool) -> None:
        if self.user is None:
            return

        guild_payload = [
            {
                "guild_id": str(guild.id),
                "name": guild.name,
                "member_count": guild.member_count,
                "icon_url": str(guild.icon.url) if guild.icon else None,
            }
            for guild in self.guilds
        ]

        def _write() -> None:
            if initial:
                db.mark_online(
                    bot_user_id=str(self.user.id),
                    bot_username=str(self.user),
                    guild_count=len(guild_payload),
                )
            else:
                db.heartbeat(guild_count=len(guild_payload))
            db.sync_guilds(guild_payload)

        try:
            await asyncio.to_thread(_write)
        except Exception:
            logger.exception("Failed to persist bot status to PostgreSQL")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "seu_token_aqui":
        raise SystemExit(
            "Defina DISCORD_TOKEN no arquivo .env (copie de .env.example)."
        )

    bot = MusicBot()
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
