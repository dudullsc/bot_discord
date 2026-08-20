"""Slash commands for music playback via Wavelink/Lavalink."""

from __future__ import annotations

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
EMBED_COLOR = discord.Color.blurple()


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
        skipped = await self.cog.handle_skip(interaction)
        if not skipped:
            return
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)  # type: ignore[union-attr]
        except Exception:
            pass


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._skip_streak: dict[int, int] = {}
        self._manual_skip_streak: dict[int, int] = {}

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
            try:
                player = await channel.connect(cls=wavelink.Player, self_deaf=True, timeout=45)
            except ChannelTimeoutException:
                logger.exception(
                    "Voice connect timed out in guild %s channel %s",
                    interaction.guild.id,
                    channel.id,
                )
                stale = self._player(interaction)
                if stale is not None:
                    try:
                        await stale.disconnect(force=True)
                    except Exception:
                        pass
                await self._send(
                    interaction,
                    "Não consegui entrar na voz a tempo. O Lavalink pode ter reiniciado, então tente de novo em alguns segundos.",
                )
                return None
            except discord.ClientException:
                logger.exception("Voice ClientException in guild %s channel %s", interaction.guild.id, channel.id)
                await self._send(interaction, "Não consegui entrar na voz.")
                return None
            except Exception:
                logger.exception("Voice connect failed in guild %s channel %s", interaction.guild.id, channel.id)
                await self._send(
                    interaction,
                    "Não consegui entrar na voz. Confere se o Lavalink está ativo e se eu tenho permissão de Conectar e Falar nesse canal.",
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

    @app_commands.command(name="play", description="Toca música, playlist ou mix do YouTube (link ou nome)")
    @app_commands.describe(query="Link do YouTube (vídeo ou playlist) ou nome da música")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer()
        player = await self._require_player(interaction, connect=True)
        if player is None:
            return

        search_query = normalize_play_query(query)

        try:
            tracks: wavelink.Search = await wavelink.Playable.search(search_query)
        except Exception:
            logger.exception("Track search failed for %s", search_query)
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

        if isinstance(tracks, wavelink.Playlist):
            tracks.extras = {"requester_id": interaction.user.id}
            try:
                added = await player.queue.put_wait(tracks)
            except Exception:
                logger.exception("Failed to load playlist %s", search_query)
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
                    f"**{tracks.name}** — `{added}` faixa(s) na fila.\n"
                    "Se alguma faixa falhar, eu pulo e sigo a playlist."
                ),
                color=EMBED_COLOR,
            )
            if was_playing:
                same_person = self._is_current_requester(player, interaction.user)
                if same_person:
                    content = self._same_requester_queue_message(actor)
                    view: discord.ui.View | None = SkipNowView(self)
                else:
                    content = self._wait_for_requester_message(
                        actor, player.current, interaction.guild
                    )
                    view = None
                await interaction.followup.send(content=content, embed=embed, view=view)
            else:
                content = (
                    f'Olha só o "{actor}" pediu uma playlist que coisa mais linda, '
                    "esperamos que não seja uma playlist de gay."
                )
                await interaction.followup.send(content=content, embed=embed)
        else:
            track = tracks[0]
            track.extras = {"requester_id": interaction.user.id}
            try:
                await player.queue.put_wait(track)
            except Exception:
                logger.exception("Failed to queue track from %s", search_query)
                await interaction.followup.send(
                    "Não consegui enfileirar essa faixa. Tente outro link."
                )
                await self._leave_if_idle(player, notify=True)
                return
            if was_playing:
                same_person = self._is_current_requester(player, interaction.user)
                if same_person:
                    content = self._same_requester_queue_message(actor)
                    view: discord.ui.View | None = SkipNowView(self)
                else:
                    content = self._wait_for_requester_message(
                        actor, player.current, interaction.guild
                    )
                    view = None
                await interaction.followup.send(
                    content=content,
                    embed=track_embed("Adicionado à fila", track, requester=interaction.user),
                    view=view,
                )
            else:
                await interaction.followup.send(
                    content=(
                        f'Olha só o "{actor}" pediu uma música que coisa mais linda, '
                        "esperamos que não seja uma música de gay."
                    ),
                    embed=track_embed("Tocando agora", track, requester=interaction.user),
                )

        if not player.playing:
            try:
                await player.play(player.queue.get(), volume=50)
                self._skip_streak.pop(interaction.guild.id, None)
                self._manual_skip_streak.pop(interaction.guild.id, None)
            except Exception:
                logger.exception("Failed to start playback in guild %s", interaction.guild.id)
                player.queue.clear()
                await interaction.followup.send(
                    "Não consegui tocar essa faixa. O YouTube está bloqueando o áudio, então tente outro vídeo."
                )
                await self._leave_if_idle(player, notify=True)
                return

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

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: object) -> None:
        player = getattr(payload, "player", None)
        if player is None:
            return
        channel = getattr(player, "channel", None)
        current = getattr(player, "current", None)
        title = current.title if current else "faixa"
        guild_id = player.guild.id
        streak = self._skip_streak.get(guild_id, 0) + 1
        self._skip_streak[guild_id] = streak
        logger.warning("Track exception: %s", getattr(payload, "exception", payload))
        if channel is not None:
            try:
                if not player.queue.is_empty:
                    if streak == 1:
                        await channel.send(
                            "Algumas faixas podem ser bloqueadas, então vou pulando automaticamente."
                        )
                else:
                    self._skip_streak.pop(guild_id, None)
                    await channel.send(
                        f"Não consegui reproduzir **{title}**. O YouTube recusou o áudio."
                    )
            except Exception:
                pass
        if streak >= 20:
            self._skip_streak.pop(guild_id, None)
            player.queue.clear()
            if channel is not None:
                try:
                    await channel.send(
                        "O YouTube bloqueou faixas demais seguidas. Vou parar a playlist."
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

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if player.playing or not player.queue.is_empty:
            return
        await self._leave_if_idle(player)

    @app_commands.command(name="pause", description="Pausa a música atual")
    async def pause(self, interaction: discord.Interaction) -> None:
        player = await self._require_player(interaction)
        if player is None:
            return
        if player.paused:
            await interaction.response.send_message("Já está pausado.", ephemeral=True)
            return
        await player.pause(True)
        await interaction.response.send_message("Pausado.")

    @app_commands.command(name="resume", description="Continua a música pausada")
    async def resume(self, interaction: discord.Interaction) -> None:
        player = await self._require_player(interaction)
        if player is None:
            return
        if not player.paused:
            await interaction.response.send_message("Não está pausado.", ephemeral=True)
            return
        await player.pause(False)
        await interaction.response.send_message("Continuando.")

    @app_commands.command(name="skip", description="Pula a faixa atual")
    async def skip(self, interaction: discord.Interaction) -> None:
        await self.handle_skip(interaction)

    async def handle_skip(self, interaction: discord.Interaction) -> bool:
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
        player = await self._require_player(interaction)
        if player is None:
            return
        player.queue.clear()
        await player.stop()
        if interaction.guild is not None:
            self._manual_skip_streak.pop(interaction.guild.id, None)
        await interaction.response.send_message("Parado e fila limpa.")

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

    @app_commands.command(name="nowplaying", description="Mostra a música que está tocando")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = self._player(interaction)
        if player is None or player.current is None:
            await interaction.response.send_message("Nada tocando no momento.", ephemeral=True)
            return

        track = player.current
        embed = track_embed("Tocando agora", track)
        position = format_ms(player.position)
        duration = format_ms(track.length)
        embed.add_field(name="Progresso", value=f"`{position} / {duration}`", inline=True)

        mode = player.queue.mode
        if mode is wavelink.QueueMode.loop:
            embed.add_field(name="Loop", value="faixa", inline=True)
        elif mode is wavelink.QueueMode.loop_all:
            embed.add_field(name="Loop", value="fila", inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Sai do canal de voz")
    async def leave(self, interaction: discord.Interaction) -> None:
        player = await self._require_player(interaction)
        if player is None:
            return
        player.queue.clear()
        await player.disconnect()
        if interaction.guild is not None:
            self._manual_skip_streak.pop(interaction.guild.id, None)
        await interaction.response.send_message("Saí da voz.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
