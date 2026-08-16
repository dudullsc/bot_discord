"""Slash commands for music playback via Wavelink/Lavalink."""

from __future__ import annotations

import re
from typing import cast

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

URL_RE = re.compile(r"https?://", re.IGNORECASE)
EMBED_COLOR = discord.Color.blurple()


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


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _player(interaction: discord.Interaction) -> wavelink.Player | None:
        if interaction.guild is None:
            return None
        return cast(wavelink.Player | None, interaction.guild.voice_client)

    async def _require_player(
        self,
        interaction: discord.Interaction,
        *,
        connect: bool = False,
    ) -> wavelink.Player | None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Este comando só funciona em um servidor.",
                ephemeral=True,
            )
            return None

        player = self._player(interaction)
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                "Entre em um canal de voz primeiro.",
                ephemeral=True,
            )
            return None

        channel = member.voice.channel

        if player is None:
            if not connect:
                await interaction.response.send_message(
                    "Não há nada tocando no momento.",
                    ephemeral=True,
                )
                return None
            try:
                player = await channel.connect(cls=wavelink.Player, self_deaf=True)
            except discord.ClientException:
                await interaction.response.send_message(
                    "Não consegui entrar no canal de voz.",
                    ephemeral=True,
                )
                return None
            player.autoplay = wavelink.AutoPlayMode.partial
        elif player.channel != channel:
            await interaction.response.send_message(
                f"Já estou em {player.channel.mention}. Entre nesse canal para controlar a música.",
                ephemeral=True,
            )
            return None

        return player

    @app_commands.command(name="play", description="Toca uma música por link ou nome (YouTube)")
    @app_commands.describe(query="Link do YouTube ou nome da música")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        player = await self._require_player(interaction, connect=True)
        if player is None:
            return

        await interaction.response.defer()

        search_query = query.strip()
        if not URL_RE.match(search_query):
            search_query = f"ytsearch:{search_query}"

        try:
            tracks: wavelink.Search = await wavelink.Playable.search(search_query)
        except Exception:
            await interaction.followup.send(
                "Falha ao buscar a música. Verifique se o Lavalink está online e tente de novo."
            )
            return

        if not tracks:
            await interaction.followup.send("Nenhum resultado encontrado para essa busca.")
            return

        if isinstance(tracks, wavelink.Playlist):
            added = await player.queue.put_wait(tracks)
            embed = discord.Embed(
                title="Playlist adicionada",
                description=f"**{tracks.name}** — `{added}` faixa(s) na fila.",
                color=EMBED_COLOR,
            )
            await interaction.followup.send(embed=embed)
        else:
            track = tracks[0]
            track.extras = {"requester_id": interaction.user.id}
            await player.queue.put_wait(track)
            if player.playing:
                await interaction.followup.send(
                    embed=track_embed("Adicionado à fila", track, requester=interaction.user)
                )
            else:
                await interaction.followup.send(
                    embed=track_embed("Tocando agora", track, requester=interaction.user)
                )

        if not player.playing:
            await player.play(player.queue.get(), volume=50)

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
        player = await self._require_player(interaction)
        if player is None:
            return
        if not player.playing:
            await interaction.response.send_message("Nada tocando para pular.", ephemeral=True)
            return
        current = player.current
        await player.skip(force=True)
        title = current.title if current else "faixa"
        await interaction.response.send_message(f"Pulou **{title}**.")

    @app_commands.command(name="stop", description="Para a música e limpa a fila")
    async def stop(self, interaction: discord.Interaction) -> None:
        player = await self._require_player(interaction)
        if player is None:
            return
        player.queue.clear()
        await player.stop()
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
        await interaction.response.send_message("Sai do canal de voz.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
