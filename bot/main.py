from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord
import wavelink
from discord.ext import commands

# ---------------------------- Configuration ----------------------------
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID_TEXT = os.getenv("GUILD_ID", "").strip()
LAVALINK_HOST = os.getenv("LAVALINK_HOST", "lavalink.railway.internal").strip()
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2333"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "").strip()
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/state.json"))
MAX_PLAYLIST_TRACKS = 100
MAX_QUEUE_TRACKS = 250
MAX_URL_LENGTH = 2048

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN. Add your Discord bot token in Railway Variables.")
if not GUILD_ID_TEXT.isdigit():
    raise RuntimeError("Missing or invalid GUILD_ID. Set it to the ID of your private Discord server.")
if not LAVALINK_PASSWORD:
    raise RuntimeError("Missing LAVALINK_PASSWORD. Set the same random password on both Railway services.")

GUILD_ID = int(GUILD_ID_TEXT)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("private-music-bot")


def valid_http_url(value: str) -> bool:
    if not value or len(value) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def track_url(track: wavelink.Playable) -> str | None:
    candidate = getattr(track, "uri", None)
    if isinstance(candidate, str) and valid_http_url(candidate):
        return candidate
    return None


def format_duration(track: wavelink.Playable | None) -> str:
    if track is None:
        return "—"
    if getattr(track, "is_stream", False):
        return "LIVE / STREAM"
    length_ms = getattr(track, "length", None)
    if not isinstance(length_ms, int) or length_ms < 0:
        return "Unknown"
    total = length_ms // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class MusicState:
    """Runtime playback state plus restart-safe session data for one private guild."""

    def __init__(self) -> None:
        self.queue: deque[wavelink.Playable] = deque()
        self.current: wavelink.Playable | None = None
        self.current_url: str | None = None
        self.loop_enabled = False
        self.voice_channel_id: int | None = None
        self.text_channel_id: int | None = None
        self.panel_message_id: int | None = None
        self.pending_queue_urls: list[str] = []
        self.lock = asyncio.Lock()
        self.save_lock = asyncio.Lock()
        self.panel_lock = asyncio.Lock()
        self.restore_lock = asyncio.Lock()

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if int(raw.get("guild_id", 0)) != GUILD_ID:
                log.warning("Ignoring persisted state for a different guild ID.")
                return
            self.voice_channel_id = self._as_int(raw.get("voice_channel_id"))
            self.text_channel_id = self._as_int(raw.get("text_channel_id"))
            self.panel_message_id = self._as_int(raw.get("panel_message_id"))
            self.current_url = raw.get("current_url") if valid_http_url(str(raw.get("current_url") or "")) else None
            self.loop_enabled = bool(raw.get("loop_enabled", False))
            self.pending_queue_urls = [
                url for url in raw.get("queue_urls", [])
                if isinstance(url, str) and valid_http_url(url)
            ][:MAX_QUEUE_TRACKS]
            log.info("Loaded saved session state. Voice channel ID: %s", self.voice_channel_id)
        except Exception:
            log.exception("Could not read saved state; starting with clean playback state.")

    async def save(self) -> None:
        # Separate save lock prevents concurrent UI/event writes from racing on the same temp file.
        async with self.save_lock:
            # The pending list is retained until startup has resolved it into Playable objects.
            queued = list(self.pending_queue_urls)
            for item in self.queue:
                url = track_url(item)
                if url:
                    queued.append(url)
            data = {
                "guild_id": GUILD_ID,
                "voice_channel_id": self.voice_channel_id,
                "text_channel_id": self.text_channel_id,
                "panel_message_id": self.panel_message_id,
                "current_url": self.current_url,
                "loop_enabled": self.loop_enabled,
                "queue_urls": queued[:MAX_QUEUE_TRACKS],
            }
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                temporary = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
                temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
                temporary.replace(STATE_FILE)
            except Exception:
                log.exception("Could not persist session state. Confirm Railway Volume is mounted at /data.")


class PrivateMusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix=".", intents=intents, help_command=None, case_insensitive=True)
        self.music = MusicState()
        self.lavalink_ready = asyncio.Event()
        self.restore_task: asyncio.Task[None] | None = None
        self._reconnect_pending = False
        self._startup_restore_done = False
        self._auto_paused_for_empty = False
        self._auto_paused_for_server_mute = False
        self._activity_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self.music.load()
        # Fixed custom IDs + timeout=None make the buttons survive bot restarts.
        self.add_view(MusicControls(self))
        asyncio.create_task(self._connect_lavalink(), name="connect-lavalink")
        if self._activity_task is None or self._activity_task.done():
            self._activity_task = asyncio.create_task(self._rotate_funny_activity(), name="funny-bot-activity")

    async def _rotate_funny_activity(self) -> None:
        """Cycle through lighthearted presence messages without spamming chat."""
        activities = [
            ("listening", "your questionable music taste 🎀"),
            ("playing", "DJ in a tiny pink room 🌸"),
            ("watching", "the queue like a hawk 🐣"),
            ("playing", "pretending to be productive ✨"),
            ("listening", "sad songs at 3 AM 💗"),
        ]
        await self.wait_until_ready()
        index = 0
        while not self.is_closed():
            kind, message = activities[index % len(activities)]
            activity_type = {
                "listening": discord.ActivityType.listening,
                "watching": discord.ActivityType.watching,
                "playing": discord.ActivityType.playing,
            }[kind]
            try:
                await self.change_presence(activity=discord.Activity(type=activity_type, name=message))
            except (discord.HTTPException, discord.Forbidden):
                log.debug("Could not update bot activity.", exc_info=True)
            index += 1
            await asyncio.sleep(900)

    async def _connect_lavalink(self) -> None:
        uri = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}"
        node = wavelink.Node(
            identifier="railway-private-node",
            uri=uri,
            password=LAVALINK_PASSWORD,
            client=self,
            retries=None,
            resume_timeout=60,
            # Keep the bot connected indefinitely when idle or when the channel is empty.
            inactive_player_timeout=0,
            inactive_channel_tokens=0,
        )
        try:
            log.info("Connecting to private Lavalink node at %s", uri)
            await wavelink.Pool.connect(nodes=[node], client=self)
        except Exception:
            log.exception("Lavalink connection task failed. Check the Railway service name and password.")

    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("Lavalink is ready (session resumed: %s).", payload.resumed)
        self.lavalink_ready.set()
        if self.restore_task is None or self.restore_task.done():
            self.restore_task = asyncio.create_task(self.restore_saved_session(), name="restore-music-session")

    async def on_ready(self) -> None:
        log.info("Discord connected as %s (ID %s).", self.user, self.user.id if self.user else "unknown")
        log.info("Locked to configured guild ID %s; commands: .join, .play, .stop.", GUILD_ID)
        if self.lavalink_ready.is_set() and (self.restore_task is None or self.restore_task.done()):
            self.restore_task = asyncio.create_task(self.restore_saved_session(), name="restore-music-session")

    def target_guild(self) -> discord.Guild | None:
        return self.get_guild(GUILD_ID)

    def player(self) -> wavelink.Player | None:
        guild = self.target_guild()
        if not guild:
            return None
        voice = guild.voice_client
        return voice if isinstance(voice, wavelink.Player) else None

    async def wait_for_audio_engine(self) -> bool:
        try:
            await asyncio.wait_for(self.lavalink_ready.wait(), timeout=20)
            return True
        except asyncio.TimeoutError:
            return False

    async def _get_tracks(self, query_or_url: str) -> list[wavelink.Playable]:
        """Resolve a direct URL, or search multiple supported sources for a song name."""
        if not await self.wait_for_audio_engine():
            raise RuntimeError("The audio engine is still starting. Try again in a few seconds.")

        value = query_or_url.strip().strip("<>")
        if not value:
            return []

        # Keep direct URLs intact so playlists and provider-specific URL resolvers work.
        # For plain text, try YouTube Music first, then YouTube, then SoundCloud.
        searches = [value] if valid_http_url(value) else [
            f"ytmsearch:{value}",
            f"ytsearch:{value}",
            f"scsearch:{value}",
        ]

        for search in searches:
            try:
                result = await wavelink.Playable.search(search)
                if isinstance(result, wavelink.Playlist):
                    tracks = list(result.tracks)
                elif isinstance(result, list):
                    tracks = list(result)
                elif result:
                    tracks = [result]  # Defensive for future Wavelink return variants.
                else:
                    tracks = []
                playable = [track for track in tracks if isinstance(track, wavelink.Playable)]
                if playable:
                    if not valid_http_url(value):
                        log.info("Song search succeeded using %s.", search.split(":", 1)[0])
                    return playable
            except Exception as exc:
                # One provider failing must not prevent trying the next configured search source.
                log.debug("Search source %s failed: %s", search.split(":", 1)[0], exc)

        return []

    async def ensure_player(self, channel: discord.VoiceChannel | discord.StageChannel) -> wavelink.Player:
        if not await self.wait_for_audio_engine():
            raise RuntimeError("The audio engine is still starting. Try again in a few seconds.")
        guild = channel.guild
        voice = guild.voice_client
        if isinstance(voice, wavelink.Player) and voice.connected:
            if voice.channel and voice.channel.id != channel.id:
                await voice.move_to(channel, self_deaf=True)
            player = voice
        else:
            if voice is not None:
                try:
                    await voice.disconnect(force=False)
                except Exception:
                    log.debug("Ignoring stale voice-client cleanup error.", exc_info=True)
            player = await channel.connect(cls=wavelink.Player, self_deaf=False, reconnect=True)
        # Reapply these in case a library default changes in a future release.
        player.inactive_timeout = 0
        player.inactive_channel_tokens = 0
        self.music.voice_channel_id = channel.id
        await self.music.save()
        return player    async def start_track(self, player: wavelink.Player, track: wavelink.Playable) -> None:
        self.music.current = track
        self.music.current_url = track_url(track) or self.music.current_url
        await self.music.save()
        await player.play(track, replace=True, volume=100)

    async def enqueue(self, player: wavelink.Player, tracks: list[wavelink.Playable]) -> tuple[int, wavelink.Playable | None]:
        async with self.music.lock:
            room = MAX_QUEUE_TRACKS - len(self.music.queue)
            accepted = tracks[:max(0, room)]
            if not accepted:
                return 0, None
            self.music.queue.extend(accepted)
            first_started: wavelink.Playable | None = None
            if self.music.current is None:
                first_started = self.music.queue.popleft()
                self.music.current = first_started
                self.music.current_url = track_url(first_started)
                await player.play(first_started, replace=True, volume=100)
            await self.music.save()
            return len(accepted), first_started

    async def advance(self, *, natural_end: bool = False, from_error: bool = False) -> None:
        player = self.player()
        if player is None or not player.connected:
            return
        async with self.music.lock:
            old = self.music.current
            if natural_end and self.music.loop_enabled and old is not None and not from_error:
                next_track = old
            else:
                next_track = self.music.queue.popleft() if self.music.queue else None
            self.music.current = next_track
            self.music.current_url = track_url(next_track) if next_track else None
            await self.music.save()
            if next_track is not None:
                # A replacement produces an end event with reason=replaced, which our
                # natural-end listener intentionally ignores to prevent double-skips.
                try:
                    await player.play(next_track, replace=True, volume=100)
                except Exception:
                    log.exception("Could not start the next queued track.")
                    self.music.current = None
                    self.music.current_url = None
                    await self.music.save()
            else:
                try:
                    await player.stop(force=True)
                except Exception:
                    log.debug("Player was already stopped during queue advance.", exc_info=True)
        await self.refresh_panel(force_new=next_track is not None or natural_end)

    async def stop_music(self) -> None:
        player = self.player()
        async with self.music.lock:
            self.music.current = None
            self.music.current_url = None
            self.music.queue.clear()
            self.music.pending_queue_urls.clear()
            self.music.loop_enabled = False
            await self.music.save()
            if player and player.connected:
                try:
                    await player.stop(force=True)
                except Exception:
                    log.debug("Player was already stopped.", exc_info=True)
        await self.refresh_panel()

    async def toggle_pause(self) -> bool:
        player = self.player()
        if not player or not player.connected or self.music.current is None:
            raise RuntimeError("Nothing is currently playing.")
        paused = not player.paused
        await player.pause(paused)
        await self.refresh_panel()
        return paused

    async def toggle_loop(self) -> bool:
        self.music.loop_enabled = not self.music.loop_enabled
        await self.music.save()
        await self.refresh_panel()
        return self.music.loop_enabled

    async def refresh_panel(
        self,
        preferred_channel: discord.abc.Messageable | None = None,
        *,
        force_new: bool = False,
    ) -> None:
        async with self.music.panel_lock:
            if preferred_channel is not None and hasattr(preferred_channel, "id"):
                new_channel_id = int(preferred_channel.id)
                if self.music.text_channel_id and self.music.text_channel_id != new_channel_id and self.music.panel_message_id:
                    old_channel = self.get_channel(self.music.text_channel_id)
                    if old_channel is None:
                        try:
                            old_channel = await self.fetch_channel(self.music.text_channel_id)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            old_channel = None
                    if old_channel is not None:
                        try:
                            old_message = await old_channel.fetch_message(self.music.panel_message_id)
                            await old_message.delete()
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                            pass
                    self.music.panel_message_id = None
                self.music.text_channel_id = new_channel_id

            if not self.music.text_channel_id:
                await self.music.save()
                return

            channel = self.get_channel(self.music.text_channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(self.music.text_channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    await self.music.save()
                    return

            if force_new and self.music.panel_message_id:
                try:
                    old_message = await channel.fetch_message(self.music.panel_message_id)
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
                    log.debug("Could not replace previous music panel message.", exc_info=True)
                self.music.panel_message_id = None

            embed = self.make_embed()
            view = MusicControls(self)
            message: discord.Message | None = None
            if self.music.panel_message_id:
                try:
                    message = await channel.fetch_message(self.music.panel_message_id)
                    await message.edit(embed=embed, view=view)
                except discord.NotFound:
                    message = None
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    log.debug("Could not edit saved control panel; attempting to post a fresh one.", exc_info=True)
                    message = None
            if message is None:
                try:
                    message = await channel.send(embed=embed, view=view)
                    self.music.panel_message_id = message.id
                except (discord.Forbidden, discord.HTTPException, AttributeError):
                    log.warning("Cannot post control panel in the selected text channel. Check Send Messages and Embed Links permissions.")
            await self.music.save()

    def make_embed(self) -> discord.Embed:
        player = self.player()
        current = self.music.current
        embed = discord.Embed(
            title="🎀  ABN's Cozy Music Corner  🎀",
            colour=discord.Colour.from_rgb(255, 105, 180),
            description="૮ ˶ᵔ ᵕ ᵔ˶ ა  your tiny pink DJ is on duty!\nUse the buttons below to control the vibes. I'll stay in voice while idle. 💗",
        )
        if current is not None:
            uri = track_url(current)
            title = (getattr(current, "title", None) or "Untitled track")[:256]
            if uri:
                embed.add_field(name="▶ Now Playing", value=f"[{discord.utils.escape_markdown(title)}]({uri})", inline=False)
            else:
                embed.add_field(name="▶ Now Playing", value=discord.utils.escape_markdown(title), inline=False)
            embed.add_field(name="Duration", value=format_duration(current), inline=True)
            embed.add_field(name="Status", value="⏸ Paused" if player and player.paused else "▶ Playing", inline=True)
            artwork = getattr(current, "artwork", None)
            if isinstance(artwork, str) and artwork.startswith("https://"):
                embed.set_thumbnail(url=artwork)
        else:
            embed.add_field(name="Status", value="🌸 Waiting for a song... give me a cute one!", inline=False)

        voice_channel = player.channel if player and player.connected else None
        if voice_channel is None and self.music.voice_channel_id:
            guild = self.target_guild()
            voice_channel = guild.get_channel(self.music.voice_channel_id) if guild else None
        embed.add_field(name="Voice Channel", value=voice_channel.mention if voice_channel else "Not connected", inline=True)
        embed.add_field(name="Loop", value="🔁 On" if self.music.loop_enabled else "Off", inline=True)
        embed.add_field(name="Tiny DJ tip", value="Type `.play song name` or `.play link` · `.stop` clears the queue 🧸", inline=False)

        upcoming = list(self.music.queue)[:5]
        if upcoming:
            lines = []
            for number, track in enumerate(upcoming, 1):
                name = discord.utils.escape_markdown((getattr(track, "title", None) or "Untitled")[:80])
                lines.append(f"`{number}.` {name} · `{format_duration(track)}`")
            remaining = len(self.music.queue) - len(upcoming)
            if remaining > 0:
                lines.append(f"…and {remaining} more")
            embed.add_field(name=f"Up Next · {len(self.music.queue)} queued", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Up Next", value="Queue is empty", inline=False)
        embed.set_footer(text="🎀 .join  ·  .play song name/link  ·  .stop  |  made with pink vibes & questionable taste 💗")
        return embed

    async def restore_saved_session(self) -> None:
        await self.wait_until_ready()
        async with self.music.restore_lock:
            guild = self.get_guild(GUILD_ID)
            if guild is None:
                log.warning("Configured server ID %s is not in the bot's cache. Confirm the bot is invited to that server.", GUILD_ID)
                return
            if not self.music.voice_channel_id:
                return
            channel = guild.get_channel(self.music.voice_channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                log.warning("Saved voice channel is missing or the bot cannot see it; use .join from the desired voice channel.")
                return
            try:
                player = await self.ensure_player(channel)
            except Exception:
                log.exception("Could not restore the saved voice channel. Check bot permissions and Railway Lavalink logs.")
                return

            # Re-resolve the saved current track and queue URLs after deployments/restarts.
            if self.music.current is None and self.music.current_url:
                old_url = self.music.current_url
                try:
                    tracks = await self._get_tracks(old_url)
                    if tracks:
                        self.music.current = tracks[0]
                        self.music.current_url = track_url(tracks[0]) or old_url
                        await player.play(tracks[0], replace=True, volume=100)
                    else:
                        log.warning("Saved current link no longer resolves; clearing it.")
                        self.music.current_url = None
                except Exception:
                    log.exception("Could not resume the saved track. It may no longer be available.")
                    self.music.current_url = None

            remaining: list[str] = []
            for url in list(self.music.pending_queue_urls):
                try:
                    tracks = await self._get_tracks(url)
                    if tracks:
                        self.music.queue.append(tracks[0])
                    else:
                        log.warning("Skipping a saved queue link that no longer resolves: %s", url)
                except Exception:
                    log.warning("Skipping an unavailable saved queue link during restore: %s", url, exc_info=True)
            self.music.pending_queue_urls = remaining

            # If a previous deployment ended between tracks, start the first restored queued item.
            if self.music.current is None and self.music.queue:
                first = self.music.queue.popleft()
                self.music.current = first
                self.music.current_url = track_url(first)
                try:
                    await player.play(first, replace=True, volume=100)
                except Exception:
                    log.exception("Could not start the first restored queued track.")
                    self.music.current = None
                    self.music.current_url = None

            await self.music.save()
            await self.refresh_panel()
            log.info("Saved voice session restore finished.")

    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        if not payload.player or not payload.player.guild or payload.player.guild.id != GUILD_ID:
            return
        reason = str(payload.reason).lower()
        # Ignore replacements and explicit stops: they are handled by the button/command.
        if reason not in {"finished", "loadfailed"}:
            return
        current = self.music.current
        if current is None:
            return
        current_id = getattr(current, "identifier", None)
        ended_id = getattr(payload.track, "identifier", None)
        if current_id and ended_id and current_id != ended_id:
            return
        await self.advance(natural_end=(reason == "finished"), from_error=(reason == "loadfailed"))

    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        if payload.player and payload.player.guild and payload.player.guild.id == GUILD_ID:
            log.warning("Playback exception for %s: %s", getattr(payload.track, "title", "track"), payload.exception)

    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        if payload.player and payload.player.guild and payload.player.guild.id == GUILD_ID:
            log.warning("Track got stuck; moving to the next queue item.")
            await self.advance(from_error=True)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.guild.id != GUILD_ID:
            return

        # Handle the bot's own voice state: disconnect recovery and server mute/unmute.
        if self.user and member.id == self.user.id:
            if before.channel is not None and after.channel is None and self.music.voice_channel_id:
                if not self._reconnect_pending:
                    self._reconnect_pending = True
                    asyncio.create_task(self._recover_voice(), name="recover-voice")
                return

            player = self.player()            if player and player.connected and self.music.current is not None:
                if after.mute and not self._auto_paused_for_server_mute:
                    try:
                        if not player.paused:
                            await player.pause(True)
                        self._auto_paused_for_server_mute = True
                        await self.refresh_panel()
                        log.info("Paused music because the bot was server-muted.")
                    except Exception:
                        log.exception("Could not pause music after the bot was server-muted.")
                elif not after.mute and self._auto_paused_for_server_mute:
                    self._auto_paused_for_server_mute = False
                    humans_present = bool(player.channel and any(not person.bot for person in player.channel.members))
                    if humans_present and not self._auto_paused_for_empty and player.paused:
                        try:
                            await player.pause(False)
                            await self.refresh_panel()
                            log.info("Resumed music because the bot was server-unmuted.")
                        except Exception:
                            log.exception("Could not resume music after the bot was server-unmuted.")
            return

        # Ignore other bots; only real humans determine whether the room is empty.
        if member.bot:
            return
        player = self.player()
        if not player or not player.connected or not player.channel:
            return
        voice_channel = player.channel
        if before.channel != voice_channel and after.channel != voice_channel:
            return

        humans_present = any(not person.bot for person in voice_channel.members)
        if not humans_present and self.music.current is not None:
            # Bots do not count as people. Remember the reason even if another condition
            # (such as server mute) has already paused the track.
            self._auto_paused_for_empty = True
            if not player.paused:
                try:
                    await player.pause(True)
                    await self.refresh_panel()
                    log.info("Paused music because no human remains in the voice channel.")
                except Exception:
                    log.exception("Could not auto-pause playback for an empty voice channel.")
        elif humans_present and self._auto_paused_for_empty:
            self._auto_paused_for_empty = False
            if not self._auto_paused_for_server_mute and player.paused:
                try:
                    await player.pause(False)
                    await self.refresh_panel()
                    log.info("Resumed music because a human returned to voice.")
                except Exception:
                    log.exception("Could not resume playback when a human returned.")
    async def _recover_voice(self) -> None:
        try:
            await asyncio.sleep(4)
            await self.lavalink_ready.wait()
            guild = self.get_guild(GUILD_ID)
            if guild is None or not self.music.voice_channel_id:
                return
            player = self.player()
            if player and player.connected:
                return
            channel = guild.get_channel(self.music.voice_channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                return
            log.warning("Bot was disconnected from voice; reconnecting to %s.", channel.id)
            new_player = await self.ensure_player(channel)
            if self.music.current_url:
                try:
                    tracks = await self._get_tracks(self.music.current_url)
                    if tracks:
                        self.music.current = tracks[0]
                        await new_player.play(tracks[0], replace=True, volume=100)
                except Exception:
                    log.exception("Could not resume track after voice reconnect.")
            await self.refresh_panel()
        except Exception:
            log.exception("Voice auto-reconnect failed; use .join to retry.")
        finally:
            self._reconnect_pending = False


class MusicControls(discord.ui.View):
    def __init__(self, bot: PrivateMusicBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.toggle_pause.label = "Resume" if (bot.player() and bot.player().paused) else "Pause"
        self.toggle_loop.label = "Loop: ON" if bot.music.loop_enabled else "Loop: OFF"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != GUILD_ID:
            await interaction.response.send_message("This private music player is only enabled in its configured server.", ephemeral=True)
            return False
        player = self.bot.player()
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not player or not player.connected:
            await interaction.response.send_message("Join the bot's voice channel first to use these controls.", ephemeral=True)
            return False
        if not player.channel or member.voice.channel.id != player.channel.id:
            await interaction.response.send_message("You must be in the same voice channel as the bot to control playback.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏯️", custom_id="private_music:pause")
    async def toggle_pause(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            paused = await self.bot.toggle_pause()
            await interaction.followup.send("Playback paused." if paused else "Playback resumed.", ephemeral=True)
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, emoji="⏭️", custom_id="private_music:next")
    async def next_track(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot.advance(natural_end=False)
        await interaction.followup.send("Skipped to the next track (or stopped if the queue was empty).", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", custom_id="private_music:stop")
    async def stop_tracks(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await self.bot.stop_music()
        await interaction.followup.send("Playback stopped and the queue was cleared. The bot will stay in voice.", ephemeral=True)

    @discord.ui.button(label="Loop: OFF", style=discord.ButtonStyle.secondary, emoji="🔁", custom_id="private_music:loop")
    async def toggle_loop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        enabled = await self.bot.toggle_loop()
        await interaction.followup.send("Repeat current track is now ON." if enabled else "Repeat current track is now OFF.", ephemeral=True)


bot = PrivateMusicBot()


def configured_guild_check():
    async def predicate(ctx: commands.Context[Any]) -> bool:
        if ctx.guild is None or ctx.guild.id != GUILD_ID:
            return False
        return True
    return commands.check(predicate)


@bot.command(name="join")
@commands.cooldown(3, 10, commands.BucketType.user)
@configured_guild_check()
async def join_command(ctx: commands.Context[Any]) -> None:
    """Join or move to the voice channel you are currently in; remain there while idle."""
    if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("Join the voice channel you want first, then run `.join`.", mention_author=False)
        return
    channel = ctx.author.voice.channel
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        await ctx.reply("Please use a normal voice channel.", mention_author=False)
        return
    try:
        player = await bot.ensure_player(channel)
        await ctx.reply(f"Connected to {channel.mention}. I will stay there while idle and reconnect after restarts.", mention_author=False)
        await bot.refresh_panel(preferred_channel=ctx.channel)
    except discord.Forbidden:
        await ctx.reply("I need **Connect** and **Speak** permissions in that voice channel, plus **Send Messages** and **Embed Links** here.", mention_author=False)
    except Exception as exc:
        log.exception(".join failed")
        await ctx.reply(f"Could not join the voice channel: `{str(exc)[:250]}`", mention_author=False)


@bot.command(name="play")
@commands.cooldown(5, 10, commands.BucketType.user)
@configured_guild_check()
async def play_command(ctx: commands.Context[Any], *, link: str | None = None) -> None:
    """Play a supported URL/playlist or search for a song by name."""
    if not link:
        await ctx.reply("Usage: `.play song name` or `.play https://link-to-a-track-or-playlist`", mention_author=False)
        return
    link = link.strip().strip("<>")
    if len(link) > MAX_URL_LENGTH:
        await ctx.reply("That song name or link is too long. Please shorten it and try again.", mention_author=False)
        return
    if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("Join the voice channel first, then run `.play song name` or `.play <link>`.", mention_author=False)
        return
    channel = ctx.author.voice.channel
    if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        await ctx.reply("Please use a normal voice channel.", mention_author=False)
        return
    try:
        player = bot.player()
        if player and player.connected and player.channel and player.channel.id != channel.id:
            await ctx.reply(
                f"I'm already playing in {player.channel.mention}. Use `.join` while you are in {channel.mention} to move me there first.",
                mention_author=False,
            )
            return
        player = await bot.ensure_player(channel)
        await ctx.typing()
        tracks = await bot._get_tracks(link)
        if not tracks:
            await ctx.reply(
                "I couldn't find a playable result. Try another song name or a public supported link. Some private, region-locked, DRM-protected, or source-blocked media cannot be played.",
                mention_author=False,
            )
            return
        if len(tracks) > MAX_PLAYLIST_TRACKS:
            tracks = tracks[:MAX_PLAYLIST_TRACKS]
        added, started = await bot.enqueue(player, tracks)
        if added == 0:
            await ctx.reply(f"The queue is full (maximum {MAX_QUEUE_TRACKS} queued tracks).", mention_author=False)
            return
        await bot.refresh_panel(preferred_channel=ctx.channel)
        if started:
            title = (getattr(started, "title", None) or "Track")[:180]
            await ctx.reply(f"▶️ Now playing **{discord.utils.escape_markdown(title)}** · `{format_duration(started)}`", mention_author=False)
        else:
            await ctx.reply(f"➕ Added **{added}** track{'s' if added != 1 else ''} to the queue.", mention_author=False)
    except discord.Forbidden:
        await ctx.reply("I need **Connect** and **Speak** in voice, plus **Send Messages** and **Embed Links** in this text channel.", mention_author=False)
    except RuntimeError as exc:
        await ctx.reply(str(exc), mention_author=False)
    except wavelink.LavalinkLoadException:
        await ctx.reply("That source failed to load this link. Try the public YouTube/SoundCloud URL for the same item.", mention_author=False)
    except Exception as exc:
        log.exception(".play failed")
        await ctx.reply(f"Something went wrong loading that link: `{str(exc)[:250]}`", mention_author=False)


@bot.command(name="stop")
@commands.cooldown(3, 10, commands.BucketType.user)
@configured_guild_check()
async def stop_command(ctx: commands.Context[Any]) -> None:
    """Stop the current track and clear the queue without leaving voice."""
    player = bot.player()
    if not player or not player.connected:
        await ctx.reply("I'm not in voice right now, cutie. Use `.join` first. 🎀", mention_author=False)
        return
    if not isinstance(ctx.author, discord.Member) or not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("Join my voice channel first so I know you're really the DJ. 💗", mention_author=False)
        return
    if not player.channel or ctx.author.voice.channel.id != player.channel.id:
        await ctx.reply("You need to be in my voice channel to stop the music. 🌸", mention_author=False)
        return
    await bot.stop_music()
    await ctx.reply("⏹️ Music stopped and the queue is cleared! I'll stay here for the next vibe. 🎀", mention_author=False)


@bot.event
async def on_command_error(ctx: commands.Context[Any], error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"Slow down a little; try again in {error.retry_after:.1f} seconds.", mention_author=False)
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.reply("This bot is restricted to its configured private server.", mention_author=False)
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply("Usage: `.play song name` or `.play https://link-to-a-track-or-playlist`", mention_author=False)
        return
    log.error("Unhandled command error", exc_info=error)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)