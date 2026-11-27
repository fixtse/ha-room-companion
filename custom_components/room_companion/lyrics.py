"""Synced lyrics: replaces the room_companion_lyrics blueprint.

When a chosen media player starts a new track, LRCLIB is asked once for timed
lyrics and the result goes to the panel's set_lyrics action. The panel keeps
the current line in time itself. Results are cached, so several panels and
repeats of a track cost one request.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging

from aiohttp import ClientError, ClientTimeout

from homeassistant.const import STATE_IDLE, STATE_OFF, STATE_STANDBY, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event

from . import panel
from .const import ACTION_SET_LYRICS, DOMAIN, LRCLIB_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)

CACHE_SIZE = 128
SILENT = {STATE_OFF, STATE_IDLE, STATE_STANDBY, STATE_UNAVAILABLE, STATE_UNKNOWN}

Track = tuple[str, str, str, int]  # title, artist, album, duration (s)


def track_of(state: State | None) -> Track | None:
    """The track a player state describes, or None when nothing plays."""
    if state is None or state.state in SILENT:
        return None
    title = str(state.attributes.get("media_title") or "")
    artist = str(state.attributes.get("media_artist") or "")
    if not title or not artist:
        return None
    album = str(state.attributes.get("media_album_name") or "")
    try:
        duration = round(float(state.attributes.get("media_duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    return title, artist, album, duration


class LyricsClient:
    """LRCLIB lookups shared by all panels: one at a time, as LRCLIB asks."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[Track, str] = OrderedDict()

    async def lookup(self, track: Track) -> str:
        """Timed LRC lyrics for a track, or '' when there are none."""
        if track in self._cache:
            self._cache.move_to_end(track)
            return self._cache[track]
        async with self._lock:
            if track in self._cache:
                return self._cache[track]
            lrc = await self._fetch(track)
            if lrc is None:
                return ""  # Network trouble: try again next time.
            self._cache[track] = lrc
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
            return lrc

    async def _fetch(self, track: Track) -> str | None:
        title, artist, album, duration = track
        params: dict[str, str] = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if duration > 0:
            params["duration"] = str(duration)
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                LRCLIB_URL, params=params, headers={"User-Agent": USER_AGENT},
                timeout=ClientTimeout(total=15),
            ) as response:
                if response.status == 404:
                    return ""
                if response.status != 200:
                    _LOGGER.debug("LRCLIB returned HTTP %s for %s", response.status, track)
                    return None
                body = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("LRCLIB lookup for %s failed: %s", track, err)
            return None
        return str(body.get("syncedLyrics") or "") if isinstance(body, dict) else ""


class PanelLyrics:
    """Sends lyrics for a panel's media players."""

    def __init__(self, hass: HomeAssistant, prefix: str, players: list[str]) -> None:
        self.hass = hass
        self.prefix = prefix
        self.players = players
        self._sent: dict[str, Track | None] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._unsub = None

    @property
    def client(self) -> LyricsClient:
        data = self.hass.data[DOMAIN]
        if "lyrics" not in data:
            data["lyrics"] = LyricsClient(self.hass)
        return data["lyrics"]

    @callback
    def start(self) -> None:
        if not self.players:
            return
        self._unsub = async_track_state_change_event(self.hass, self.players, self._changed)
        self.resend()

    @callback
    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    @callback
    def resend(self) -> None:
        """Send every player's lyrics again, e.g. after the panel restarted."""
        self._sent.clear()
        for player in self.players:
            self._schedule(player, self.hass.states.get(player))

    @callback
    def _changed(self, event: Event[EventStateChangedData]) -> None:
        self._schedule(event.data["entity_id"], event.data["new_state"])

    @callback
    def _schedule(self, player: str, state: State | None) -> None:
        track = track_of(state)
        if player in self._sent and self._sent[player] == track:
            return
        if (task := self._tasks.get(player)) is not None:
            task.cancel()  # Like the blueprint's mode: restart.
        self._tasks[player] = self.hass.async_create_background_task(
            self._send(player, track), f"{DOMAIN} lyrics {self.prefix} {player}"
        )

    async def _send(self, player: str, track: Track | None) -> None:
        self._sent[player] = track
        lrc = await self.client.lookup(track) if track else ""
        # The track may have changed while LRCLIB answered.
        if track != track_of(self.hass.states.get(player)):
            return
        ok = await panel.call(self.hass, self.prefix, ACTION_SET_LYRICS, {
            "entity_id": player, "title": track[0] if track else "", "lrc": lrc,
        })
        if not ok:
            self._sent.pop(player, None)
