"""Slash commands for music playback via Wavelink/Lavalink."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import cast

import discord
import wavelink
from discord import app_commands
from discord.ext import commands
from wavelink.exceptions import ChannelTimeoutException

logger = logging.getLogger("bot")

URL_RE = re.compile(r"https?://", re.IGNORECASE)
YT_LIST_RE = re.compile(r"[?&]list=([^&]+)", re.IGNORECASE)
MIX_QUERY_RE = re.compile(r"\bmix\b", re.IGNORECASE)
EMBED_COLOR = discord.Color.blurple()
MIX_TRACK_LIMIT = 20

SOURCE_DOMAINS: dict[str, str] = {
    "deezer.com": "Deezer",
    "soundcloud.com": "SoundCloud",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
}


def track_source_domain(track: wavelink.Playable | None) -> str:
    uri = ((track.uri if track else "") or "").lower()
    for domain in SOURCE_DOMAINS:
        if domain in uri:
            return domain
    return ""


SEARCH_SOURCES: dict[str, str] = {
    "dzsearch": "deezer.com",
    "scsearch": "soundcloud.com",
}


async def search_source_tracks(prefix: str, query: str) -> list[wavelink.Playable]:
    source = prefix if prefix.endswith(":") else f"{prefix}:"
    try:
        results = await wavelink.Playable.search(query, source=source)
    except Exception:
        logger.exception("%s search failed for %s", prefix, query)
        return []
    if not results or isinstance(results, wavelink.Playlist):
        return []

    expected_domain = SEARCH_SOURCES.get(prefix.rstrip(":"), "")
    tracks = list(results)
    if expected_domain:
        tracks = [track for track in tracks if expected_domain in (track.uri or "").lower()]
    return tracks


def normalize_play_query(query: str) -> str:
    stripped = query.strip()
    if not URL_RE.match(stripped):
        return f"ytsearch:{stripped}"

    stripped = re.sub(
        r"https?://music\.youtube\.com/",
        "https://www.youtube.com/",
        stripped,
        flags=re.IGNORECASE,
    )

    list_match = YT_LIST_RE.search(stripped)
    if list_match:
        playlist_id = list_match.group(1)
        return f"https://www.youtube.com/playlist?list={playlist_id}"

    return stripped


def is_mix_query(query: str) -> bool:
    stripped = query.strip()
    if URL_RE.match(stripped):
        return False
    return bool(MIX_QUERY_RE.search(stripped))


def source_label(track: wavelink.Playable | list[wavelink.Playable] | wavelink.Playlist) -> str:
    sample: wavelink.Playable | None
    if isinstance(track, wavelink.Playlist):
        try:
            sample = next(iter(track), None)
        except Exception:
            sample = None
    elif isinstance(track, list):
        sample = track[0] if track else None
    else:
        sample = track
    domain = track_source_domain(sample)
    if domain:
        return SOURCE_DOMAINS[domain]
    return "busca"


async def search_play_query(query: str) -> wavelink.Search:
    """Prefer Deezer, then SoundCloud; mixes skip YouTube."""
    stripped = query.strip()
    if URL_RE.match(stripped):
        return await wavelink.Playable.search(normalize_play_query(stripped))

    mix = is_mix_query(stripped)
    search_query = MIX_QUERY_RE.sub(" ", stripped).strip() if mix else stripped
    search_query = re.sub(r"\s+", " ", search_query).strip() or stripped

    dz_tracks = await search_source_tracks("dzsearch", search_query)
    if dz_tracks:
        return dz_tracks

    sc_tracks = await search_source_tracks("scsearch", search_query)
    if sc_tracks:
        return sc_tracks

    # Mixes: do not fall back to YouTube (too many blocks on datacenter IPs).
    if mix:
        return []

    return await wavelink.Playable.search(stripped, source="ytsearch:")


def format_ms(ms: int | float | None) -> str:
    if ms is None or ms < 0:
        return "ao vivo"
    total = int(ms) // 1000
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def track_embed(title: str, track: wavelink.Playable, *, requester: discord.abc.User | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=EMBED_COLOR)
    embed.description = f"**[{track.title}]({track.uri})**\npor `{track.author}`"
    embed.add_field(name="Duração", value=format_ms(track.length), inline=True)
    if requester is not None:
        embed.add_field(name="Pedido por", value=requester.mention, inline=True)
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    return embed


def track_requester_id(track: wavelink.Playable | None) -> int | None:
    if track is None:
        return None
    extras = getattr(track, "extras", None)
    if extras is None:
        return None
    try:
        value = dict(extras).get("requester_id")
    except Exception:
        value = getattr(extras, "requester_id", None)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


class SkipNowView(discord.ui.View):
    def __init__(self, cog: Music) -> None:
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="Pular agora", style=discord.ButtonStyle.danger)
    async def skip_now(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self.cog._defer(interaction, ephemeral=False)
        skipped = await self.cog.handle_skip(interaction, deferred=True)
        if not skipped:
            return
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass


class PlayerPanelView(discord.ui.View):
    """Interactive now-playing controls."""

    def __init__(self, cog: Music, *, paused: bool = False) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "music:pause":
                child.emoji = "▶️" if paused else "⏸️"
                child.label = "Continuar" if paused else "Pausar"

    @discord.ui.button(emoji="⏮️", label="Voltar", style=discord.ButtonStyle.secondary, custom_id="music:prev", row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_previous(interaction)

    @discord.ui.button(emoji="⏸️", label="Pausar", style=discord.ButtonStyle.primary, custom_id="music:pause", row=0)
    async def pause_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_pause_toggle(interaction)

    @discord.ui.button(emoji="⏭️", label="Pular", style=discord.ButtonStyle.secondary, custom_id="music:skip", row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_skip(interaction)

    @discord.ui.button(emoji="⏹️", label="Parar", style=discord.ButtonStyle.danger, custom_id="music:stop", row=1)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_stop(interaction)

    @discord.ui.button(emoji="🚪", label="Sair", style=discord.ButtonStyle.danger, custom_id="music:leave", row=1)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.panel_leave(interaction)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._skip_streak: dict[int, int] = {}
        self._manual_skip_streak: dict[int, int] = {}
        # guild_id -> (channel_id, message_id)
        self._player_panels: dict[int, tuple[int, int]] = {}
        # Avoid spamming alternate-source fallback for the same track.
        self._fallback_busy: set[int] = set()
        self._fallback_done: dict[int, set[str]] = {}

    @staticmethod
    def _player(interaction: discord.Interaction) -> wavelink.Player | None:
        if interaction.guild is None:
            return None
        return cast(wavelink.Player | None, interaction.guild.voice_client)

    @staticmethod
    def _display_name(user: discord.abc.User) -> str:
        if isinstance(user, discord.Member):
            return user.display_name
        return user.name

    @staticmethod
    def _requester_name(track: wavelink.Playable | None, guild: discord.Guild | None) -> str:
        requester_id = track_requester_id(track)
        if requester_id is None or guild is None:
            return "quem pediu antes"
        member = guild.get_member(requester_id)
        if member is not None:
            return member.display_name
        return "quem pediu antes"

    def _is_current_requester(self, player: wavelink.Player, user: discord.abc.User) -> bool:
        requester_id = track_requester_id(player.current)
        if requester_id is None:
            return True
        return requester_id == user.id

    def _wait_for_requester_message(self, actor: str, track: wavelink.Playable | None, guild: discord.Guild | None) -> str:
        first_requester = self._requester_name(track, guild)
        return (
            f'Opa "{actor}" espera acabar a playlist mais podre do "{first_requester}" '
            "assim que acabar a sua vai tocar!"
        )

    def _same_requester_queue_message(self, actor: str) -> str:
        return (
            f'Calma aí "{actor}", se quiser outra música digite `/skip` ou clique aqui.'
        )

    def _queue_feedback(
        self,
        interaction: discord.Interaction,
        *,
        actor: str,
        player: wavelink.Player,
        was_playing: bool,
        idle_content: str,
    ) -> tuple[str, discord.ui.View | None]:
        if was_playing:
            if self._is_current_requester(player, interaction.user):
                return self._same_requester_queue_message(actor), SkipNowView(self)
            return (
                self._wait_for_requester_message(actor, player.current, interaction.guild),
                None,
            )
        return idle_content, None

    async def _send(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        ephemeral: bool = True,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)

    async def _defer(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = True,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=ephemeral)

    async def _followup(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
    ) -> None:
        kwargs: dict[str, object] = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)

    async def _require_player(
        self,
        interaction: discord.Interaction,
        *,
        connect: bool = False,
    ) -> wavelink.Player | None:
        if interaction.guild is None:
            await self._send(interaction, "Isso aqui só roda num servidor.")
            return None

        player = self._player(interaction)
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await self._send(interaction, "Entre em um canal de voz primeiro.")
            return None

        channel = member.voice.channel

        if player is None:
            if not connect:
                await self._send(interaction, "Nada tocando no momento.")
                return None
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    player = await channel.connect(cls=wavelink.Player, self_deaf=True, timeout=45)
                    break
                except ChannelTimeoutException as exc:
                    last_error = exc
                    logger.warning(
                        "Voice connect timed out in guild %s channel %s (attempt %s/2)",
                        interaction.guild.id,
                        channel.id,
                        attempt + 1,
                    )
                    stale = self._player(interaction)
                    if stale is not None:
                        try:
                            await stale.disconnect(force=True)
                        except Exception:
                            pass
                    if attempt == 0:
                        await asyncio.sleep(2)
                        continue
                except discord.ClientException as exc:
                    last_error = exc
                    logger.exception("Voice ClientException in guild %s channel %s", interaction.guild.id, channel.id)
                    await self._send(interaction, "Não consegui entrar na voz.")
                    return None
                except Exception as exc:
                    last_error = exc
                    logger.exception("Voice connect failed in guild %s channel %s", interaction.guild.id, channel.id)
                    await self._send(
                        interaction,
                        "Não consegui entrar na voz. Confere se o Lavalink está ativo e se eu tenho permissão de Conectar e Falar nesse canal.",
                    )
                    return None
            else:
                logger.exception(
                    "Voice connect timed out in guild %s channel %s after retries",
                    interaction.guild.id,
                    channel.id,
                )
                await self._send(
                    interaction,
                    "Não consegui entrar na voz a tempo. O Lavalink pode ter reiniciado — tente de novo em alguns segundos.",
                )
                return None
            player.autoplay = wavelink.AutoPlayMode.partial
        elif player.channel != channel:
            if not connect:
                await self._send(
                    interaction,
                    f"Já estou em {player.channel.mention}. Entre nesse canal para controlar a música.",
                )
                return None
            try:
                await player.move_to(channel)
            except Exception:
                await self._send(
                    interaction,
                    f"Não consegui ir para {channel.mention}. Me dê permissão de Conectar e Falar lá.",
                )
                return None

        return player

    async def _leave_if_idle(self, player: wavelink.Player, *, notify: bool = False) -> None:
        if player.playing or not player.queue.is_empty:
            return
        channel = player.channel
        try:
            await player.disconnect()
        except Exception:
            logger.exception("Failed to disconnect idle player in guild %s", player.guild.id)
            return
        if notify and channel is not None:
            try:
                await channel.send("Saí da voz porque não havia mais nada tocando.")
            except Exception:
                pass

    def player_embed(self, player: wavelink.Player) -> discord.Embed:
        track = player.current
        if track is None:
            embed = discord.Embed(
                title="Player",
                description="Nada tocando no momento.",
                color=EMBED_COLOR,
            )
            return embed

        status = "⏸ Pausado" if player.paused else "▶ Tocando"
        embed = discord.Embed(
            title="Player",
            description=f"**[{track.title}]({track.uri})**\npor `{track.author}`",
            color=EMBED_COLOR,
        )
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(
            name="Progresso",
            value=f"`{format_ms(player.position)} / {format_ms(track.length)}`",
            inline=True,
        )
        embed.add_field(name="Fila", value=f"`{len(player.queue)}` faixa(s)", inline=True)
        requester = self._requester_name(track, player.guild)
        embed.add_field(name="Pedido por", value=requester, inline=True)
        embed.add_field(name="Fonte", value=source_label(track), inline=True)
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        return embed

    async def refresh_player_panel(
        self,
        player: wavelink.Player,
        *,
        channel: discord.abc.Messageable | None = None,
        disabled: bool = False,
    ) -> None:
        guild = player.guild
        if guild is None:
            return

        embed = self.player_embed(player)
        view: discord.ui.View | None
        if disabled or player.current is None:
            view = None
        else:
            view = PlayerPanelView(self, paused=player.paused)

        existing = self._player_panels.get(guild.id)
        message: discord.Message | None = None
        if existing is not None:
            channel_id, message_id = existing
            text_channel = guild.get_channel(channel_id)
            if text_channel is not None and hasattr(text_channel, "fetch_message"):
                try:
                    message = await text_channel.fetch_message(message_id)
                except Exception:
                    message = None

        if message is not None:
            try:
                kwargs: dict[str, object] = {"embed": embed}
                if view is not None:
                    kwargs["view"] = view
                else:
                    kwargs["view"] = None
                await message.edit(**kwargs)
                if disabled or player.current is None:
                    self._player_panels.pop(guild.id, None)
                return
            except Exception:
                self._player_panels.pop(guild.id, None)

        if disabled or player.current is None:
            return

        target = channel
        if target is None and existing is not None:
            target = guild.get_channel(existing[0])
        if target is None:
            return
        try:
            sent = await target.send(embed=embed, view=view)
        except Exception:
            logger.exception("Failed to send player panel in guild %s", guild.id)
            return
        self._player_panels[guild.id] = (sent.channel.id, sent.id)

    async def panel_previous(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        player = await self._require_player(interaction)
        if player is None:
            return
        history = player.queue.history
        if history.is_empty:
            await self._send(interaction, "Não tem música anterior.")
            return

        try:
            previous = list(history)[-1]
            history.delete(len(history) - 1)
        except Exception:
            logger.exception("Failed to read history for previous track")
            await self._send(interaction, "Não consegui voltar a faixa.")
            return

        current = player.current
        if current is not None:
            player.queue.put_at(0, current)

        try:
            await player.play(previous)
        except Exception:
            logger.exception("Failed to play previous track")
            await self._send(interaction, "Não consegui tocar a faixa anterior.")
            return

        await self._send(interaction, "Voltei a faixa anterior.")
        await self.refresh_player_panel(player, channel=interaction.channel)

    async def panel_pause_toggle(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        player = await self._require_player(interaction)
        if player is None:
            return
        if not player.playing and not player.paused:
            await self._send(interaction, "Nada tocando no momento.")
            return
        await player.pause(not player.paused)
        label = "Pausado." if player.paused else "Continuando."
        await self._send(interaction, label)
        await self.refresh_player_panel(player, channel=interaction.channel)

    async def panel_skip(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction, ephemeral=False)
        skipped = await self.handle_skip(interaction, deferred=True)
        if skipped and interaction.guild is not None:
            player = self._player(interaction)
            if player is not None:
                await self.refresh_player_panel(player, channel=interaction.channel)

    async def panel_stop(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        player = await self._require_player(interaction)
        if player is None:
            return
        player.queue.clear()
        await player.stop()
        if interaction.guild is not None:
            self._manual_skip_streak.pop(interaction.guild.id, None)
        await self._send(interaction, "Parado e fila limpa.")
        await self.refresh_player_panel(player, channel=interaction.channel, disabled=True)

    async def panel_leave(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        player = await self._require_player(interaction)
        if player is None:
            return
        guild_id = interaction.guild.id if interaction.guild else None
        await self.refresh_player_panel(player, channel=interaction.channel, disabled=True)
        player.queue.clear()
        await player.disconnect()
        if guild_id is not None:
            self._manual_skip_streak.pop(guild_id, None)
            self._player_panels.pop(guild_id, None)
        await self._send(interaction, "Saí da voz.")

    async def _queue_mix_tracks(
        self,
        player: wavelink.Player,
        tracks: list[wavelink.Playable],
        *,
        requester_id: int,
    ) -> int:
        """Queue top Deezer/search hits for a mix request (no YouTube radio lists)."""
        if not tracks:
            return 0

        batch = list(tracks[:MIX_TRACK_LIMIT])
        for track in batch:
            track.extras = {"requester_id": requester_id}
        try:
            return await player.queue.put_wait(batch)
        except Exception:
            logger.exception("Failed to queue mix batch")
            return 0

    @app_commands.command(name="play", description="Toca música/mix (Deezer primeiro, SoundCloud/YouTube se precisar)")
    @app_commands.describe(query="Link, nome da música, ou 'mix artista' para várias faixas")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self._require_player(interaction, connect=True)
        if player is None:
            return

        try:
            tracks: wavelink.Search = await search_play_query(query)
        except Exception:
            logger.exception("Track search failed for %s", query)
            await interaction.followup.send(
                "Falha ao buscar a música. Verifique se o link está online e tente de novo."
            )
            await self._leave_if_idle(player, notify=True)
            return

        if not tracks:
            await interaction.followup.send("Não achei nada nessa busca.")
            await self._leave_if_idle(player, notify=True)
            return

        actor = self._display_name(interaction.user)
        was_playing = player.playing
        origin = source_label(tracks)

        if isinstance(tracks, wavelink.Playlist):
            tracks.extras = {"requester_id": interaction.user.id}
            try:
                added = await player.queue.put_wait(tracks)
            except Exception:
                logger.exception("Failed to load playlist %s", query)
                await interaction.followup.send(
                    "Não consegui carregar essa playlist. Cole o link da playlist (youtube.com/playlist?list=...) ou um vídeo com ?list= na URL."
                )
                await self._leave_if_idle(player, notify=True)
                return
            if added <= 0:
                await interaction.followup.send(
                    "Essa playlist veio vazia. Tente outro link."
                )
                await self._leave_if_idle(player, notify=True)
                return
            embed = discord.Embed(
                title="Playlist adicionada",
                description=(
                    f"**{tracks.name}** — `{added}` faixa(s) na fila (`{origin}`).\n"
                    "Se alguma faixa falhar, eu pulo e sigo a playlist."
                ),
                color=EMBED_COLOR,
            )
            if was_playing:
                content, view = self._queue_feedback(
                    interaction,
                    actor=actor,
                    player=player,
                    was_playing=True,
                    idle_content="",
                )
                await self._followup(interaction, content=content, embed=embed, view=view)
            else:
                content = (
                    f'Olha só o "{actor}" pediu uma playlist que coisa mais linda, '
                    "esperamos que não seja uma playlist de gay."
                )
                await interaction.followup.send(content=content, embed=embed)
        else:
            mix_mode = is_mix_query(query)
            if mix_mode:
                added = await self._queue_mix_tracks(
                    player,
                    tracks,
                    requester_id=interaction.user.id,
                )
                if added <= 0:
                    await interaction.followup.send(
                        "Não consegui montar esse mix. Tenta de novo ou cola um link."
                    )
                    await self._leave_if_idle(player, notify=True)
                    return
                embed = discord.Embed(
                    title="Mix adicionado",
                    description=(
                        f"Busca: `{query.strip()}` via `{origin}`\n"
                        f"Enfileirei `{added}` faixa(s) relacionadas.\n"
                        "Se alguma falhar, eu pulo e sigo o mix."
                    ),
                    color=EMBED_COLOR,
                )
                content, view = self._queue_feedback(
                    interaction,
                    actor=actor,
                    player=player,
                    was_playing=was_playing,
                    idle_content=(
                        f'Olha só o "{actor}" pediu um mix que coisa mais linda, '
                        "vamos ver se aguenta a playlist toda."
                    ),
                )
                await self._followup(interaction, content=content, embed=embed, view=view)
            else:
                track = tracks[0]
                track.extras = {"requester_id": interaction.user.id}
                try:
                    await player.queue.put_wait(track)
                except Exception:
                    logger.exception("Failed to queue track from %s", query)
                    await interaction.followup.send(
                        "Não consegui enfileirar essa faixa. Tente outro link."
                    )
                    await self._leave_if_idle(player, notify=True)
                    return
                if was_playing:
                    content, view = self._queue_feedback(
                        interaction,
                        actor=actor,
                        player=player,
                        was_playing=True,
                        idle_content="",
                    )
                    await self._followup(
                        interaction,
                        content=content,
                        embed=track_embed("Adicionado à fila", track, requester=interaction.user),
                        view=view,
                    )
                else:
                    await interaction.followup.send(
                        content=(
                            f'Olha só o "{actor}" pediu uma música no {origin} que coisa mais linda, '
                            "esperamos que não seja uma música de gay."
                        ),
                        embed=track_embed("Tocando agora", track, requester=interaction.user),
                    )

        if not player.playing:
            try:
                await player.play(player.queue.get(), volume=50)
                self._skip_streak.pop(interaction.guild.id, None)
                self._manual_skip_streak.pop(interaction.guild.id, None)
                self._fallback_done.pop(interaction.guild.id, None)
                self._fallback_busy.discard(interaction.guild.id)
            except Exception:
                logger.exception("Failed to start playback in guild %s", interaction.guild.id)
                player.queue.clear()
                await interaction.followup.send(
                    "Não consegui tocar essa faixa. Tente outra busca ou outro link."
                )
                await self._leave_if_idle(player, notify=True)
                return
            await self.refresh_player_panel(player, channel=interaction.channel)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        channel = None
        if player.guild is not None:
            existing = self._player_panels.get(player.guild.id)
            if existing is not None:
                channel = player.guild.get_channel(existing[0])
        await self.refresh_player_panel(player, channel=channel)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        if player.playing:
            return
        channel = player.channel
        try:
            await player.disconnect()
        except Exception:
            return
        if channel is None:
            return
        try:
            await channel.send("Fiquei parado e saí da voz, seu Macaco!")
        except Exception:
            return

    @staticmethod
    def _track_key(track: wavelink.Playable | None) -> str:
        if track is None:
            return ""
        return (getattr(track, "identifier", None) or track.uri or track.title or "").strip()

    async def _alternate_source_fallback(
        self,
        player: wavelink.Player,
        failed: wavelink.Playable | None,
    ) -> tuple[wavelink.Playable | None, str | None]:
        if failed is None:
            return None, None

        query = f"{failed.title} {failed.author}".strip()
        if not query:
            return None, None

        failed_domain = track_source_domain(failed)
        fallbacks: tuple[tuple[str, str], ...] = (
            ("dzsearch:", "deezer.com"),
            ("scsearch:", "soundcloud.com"),
        )

        for source, domain in fallbacks:
            if failed_domain == domain:
                continue
            tracks = await search_source_tracks(source.rstrip(":"), query)
            if not tracks:
                continue
            track = tracks[0]
            if domain not in ((track.uri or "").lower()):
                continue
            requester_id = track_requester_id(failed)
            if requester_id is not None:
                track.extras = {"requester_id": requester_id}
            return track, SOURCE_DOMAINS[domain]

        return None, None

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: object) -> None:
        player = getattr(payload, "player", None)
        if player is None:
            return
        channel = getattr(player, "channel", None)
        current = getattr(player, "current", None)
        title = current.title if current else "faixa"
        guild_id = player.guild.id
        track_key = self._track_key(current)
        logger.warning("Track exception: %s", getattr(payload, "exception", payload))

        if guild_id in self._fallback_busy:
            return
        done = self._fallback_done.setdefault(guild_id, set())
        if track_key and track_key in done:
            # Already tried fallback for this item — skip ahead once.
            if not player.queue.is_empty:
                try:
                    await player.skip(force=True)
                except Exception:
                    pass
            return

        self._fallback_busy.add(guild_id)
        try:
            fallback, source_name = await self._alternate_source_fallback(player, current)
            if fallback is not None and source_name is not None:
                if track_key:
                    done.add(track_key)
                try:
                    await player.play(fallback)
                    self._skip_streak.pop(guild_id, None)
                    if channel is not None:
                        await channel.send(
                            f"Não rolou tocar **{title}**. Tô tocando no {source_name}: **{fallback.title}**."
                        )
                    await self.refresh_player_panel(player, channel=channel)
                    return
                except Exception:
                    logger.exception("%s fallback play failed", source_name)

            streak = self._skip_streak.get(guild_id, 0) + 1
            self._skip_streak[guild_id] = streak
            if channel is not None:
                try:
                    if not player.queue.is_empty:
                        if streak == 1:
                            await channel.send(
                                "Algumas faixas podem falhar, então vou pulando automaticamente."
                            )
                    else:
                        self._skip_streak.pop(guild_id, None)
                        await channel.send(
                            f"Não consegui reproduzir **{title}**. Deezer/SoundCloud/YouTube falharam."
                        )
                except Exception:
                    pass
            if streak >= 20:
                self._skip_streak.pop(guild_id, None)
                done.clear()
                player.queue.clear()
                if channel is not None:
                    try:
                        await channel.send(
                            "Bloqueios demais seguidos. Vou parar a playlist."
                        )
                    except Exception:
                        pass
                await self._leave_if_idle(player)
                return
            if not player.queue.is_empty:
                try:
                    await player.skip(force=True)
                except Exception:
                    logger.exception("Failed to skip broken track in guild %s", player.guild.id)
                    player.queue.clear()
                    await self._leave_if_idle(player, notify=True)
                return
            await self._leave_if_idle(player, notify=True)
        finally:
            self._fallback_busy.discard(guild_id)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        # With AutoPlayMode.partial, the next queued track starts after this event.
        # On /skip, playing is briefly False and disconnecting here kicks the bot mid-queue.
        if player.playing or not player.queue.is_empty:
            return
        reason = str(getattr(payload, "reason", "") or "").lower()
        if reason in {"replaced", "stopped"}:
            return
        # Only update the panel; inactive_player handles disconnect after idle timeout.
        await self.refresh_player_panel(player, disabled=True)

    @app_commands.command(name="pause", description="Pausa a música atual")
    async def pause(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction, ephemeral=True)
        player = await self._require_player(interaction)
        if player is None:
            return
        if player.paused:
            await self._send(interaction, "Já está pausado.")
            return
        await player.pause(True)
        await self._send(interaction, "Pausado.")
        await self.refresh_player_panel(player, channel=interaction.channel)

    @app_commands.command(name="resume", description="Continua a música pausada")
    async def resume(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction, ephemeral=True)
        player = await self._require_player(interaction)
        if player is None:
            return
        if not player.paused:
            await self._send(interaction, "Não está pausado.")
            return
        await player.pause(False)
        await self._send(interaction, "Continuando.")
        await self.refresh_player_panel(player, channel=interaction.channel)

    @app_commands.command(name="skip", description="Pula a faixa atual")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction, ephemeral=False)
        await self.handle_skip(interaction, deferred=True)

    async def handle_skip(self, interaction: discord.Interaction, *, deferred: bool = False) -> bool:
        if not deferred:
            await self._defer(interaction, ephemeral=False)
        player = await self._require_player(interaction)
        if player is None:
            return False
        if not player.playing:
            await self._send(interaction, "Nada tocando para pular.")
            return False

        actor = self._display_name(interaction.user)
        if not self._is_current_requester(player, interaction.user):
            await self._send(
                interaction,
                self._wait_for_requester_message(actor, player.current, interaction.guild),
                ephemeral=False,
            )
            return False

        await player.skip(force=True)
        guild_id = interaction.guild.id if interaction.guild else 0
        streak = self._manual_skip_streak.get(guild_id, 0) + 1
        self._manual_skip_streak[guild_id] = streak
        if streak == 1:
            message = f'Pow "{actor}" deixa a música tocar esta tão boa.....'
        elif streak == 2:
            message = (
                f'Ôloco "{actor}" já é a segunda vez que pulou música, '
                "tem certeza que gosta de ouvir música?"
            )
        else:
            message = "Desisto, pula essa merda aí mesmo, é ruim pra caralho!"
        await self._send(interaction, message, ephemeral=False)
        return True

    @app_commands.command(name="stop", description="Para a música e limpa a fila")
    async def stop(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction, ephemeral=True)
        player = await self._require_player(interaction)
        if player is None:
            return
        player.queue.clear()
        await player.stop()
        if interaction.guild is not None:
            self._manual_skip_streak.pop(interaction.guild.id, None)
        await self._send(interaction, "Parado e fila limpa.")
        await self.refresh_player_panel(player, channel=interaction.channel, disabled=True)

    @app_commands.command(name="queue", description="Mostra a fila de músicas")
    async def queue(self, interaction: discord.Interaction) -> None:
        player = self._player(interaction)
        if player is None or (not player.playing and player.queue.is_empty):
            await interaction.response.send_message("A fila está vazia.", ephemeral=True)
            return

        embed = discord.Embed(title="Fila", color=EMBED_COLOR)
        if player.current:
            embed.add_field(
                name="Tocando agora",
                value=f"**[{player.current.title}]({player.current.uri})** — `{format_ms(player.current.length)}`",
                inline=False,
            )

        if player.queue.is_empty:
            embed.description = "Nenhuma faixa na fila."
        else:
            lines: list[str] = []
            for index, track in enumerate(list(player.queue)[:15], start=1):
                lines.append(f"`{index}.` [{track.title}]({track.uri}) (`{format_ms(track.length)}`)")
            remaining = len(player.queue) - 15
            if remaining > 0:
                lines.append(f"... e mais `{remaining}` faixa(s).")
            embed.description = "\n".join(lines)

        mode = player.queue.mode
        if mode is wavelink.QueueMode.loop:
            embed.set_footer(text="Loop: faixa")
        elif mode is wavelink.QueueMode.loop_all:
            embed.set_footer(text="Loop: fila")

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Ajusta o volume (0-100)")
    @app_commands.describe(level="Volume entre 0 e 100")
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
        player = await self._require_player(interaction)
        if player is None:
            return
        await player.set_volume(int(level))
        await interaction.response.send_message(f"Volume em **{level}%**.")

    @app_commands.command(name="loop", description="Alterna o modo de loop (off → faixa → fila)")
    async def loop(self, interaction: discord.Interaction) -> None:
        player = await self._require_player(interaction)
        if player is None:
            return

        current = player.queue.mode
        if current is wavelink.QueueMode.normal:
            player.queue.mode = wavelink.QueueMode.loop
            label = "faixa atual"
        elif current is wavelink.QueueMode.loop:
            player.queue.mode = wavelink.QueueMode.loop_all
            label = "fila inteira"
        else:
            player.queue.mode = wavelink.QueueMode.normal
            label = "desligado"

        await interaction.response.send_message(f"Loop: **{label}**.")

    @app_commands.command(name="nowplaying", description="Mostra o player com a música atual e botões")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        await self._open_player_panel(interaction)

    @app_commands.command(name="player", description="Abre o painel do player com botões no chat")
    async def player_cmd(self, interaction: discord.Interaction) -> None:
        await self._open_player_panel(interaction)

    async def _open_player_panel(self, interaction: discord.Interaction) -> None:
        player = self._player(interaction)
        if player is None or player.current is None:
            await interaction.response.send_message("Nada tocando no momento.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.refresh_player_panel(player, channel=interaction.channel)
        await interaction.followup.send("Player atualizado no chat.", ephemeral=True)

    @app_commands.command(name="leave", description="Sai do canal de voz")
    async def leave(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction, ephemeral=True)
        player = await self._require_player(interaction)
        if player is None:
            return
        player.queue.clear()
        await player.disconnect()
        if interaction.guild is not None:
            self._manual_skip_streak.pop(interaction.guild.id, None)
            self._player_panels.pop(interaction.guild.id, None)
        await self._send(interaction, "Saí da voz.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
