"""Live camera pages for the panels.

The panel shows live cameras through its Remote WebView server: a browser that
opens a page and sends the picture to the panel. Here Home Assistant works out
which page plays a camera and serves a small page placing that player where the
panel's camera frame is:

- WebRTC Camera integration: registers a link for the camera (no sign-in, no
  time limit) and frames the integration's /webrtc/embed page. Both pages come
  from Home Assistant, so the embed page may be framed.
- go2rtc (the address in the options, Frigate's, or Home Assistant's own when
  it listens on the network): frames go2rtc's player for the camera's stream,
  found by name or by its source address. A camera go2rtc does not know yet is
  added from Home Assistant's stream source.

Sound: play_camera_audio sends go2rtc's RTSP output of the camera to a media
player (Home Assistant's ffmpeg reads any audio codec from it).

Previews: the panel shows snapshots (or an MJPEG stream) until the live video
plays. Snapshots serves them at the camera frame's size, from go2rtc's running
stream when a go2rtc has the camera (including WebRTC Camera's and Home
Assistant's own, reached locally), else from Home Assistant's camera image.
While a panel looks at a camera, frames come in the background, so a request
is answered from the latest frame at once; panels showing the same camera share
them. With an RTSP source (go2rtc's restream, else the camera's own), ffmpeg
reads the stream once and decodes only its key frames: a new frame every second
or two at little cost, with no reconnect per frame. Otherwise (no ffmpeg, no
RTSP) single frames are fetched in turn.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import html
import json
import logging
import re
import shutil
import time
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from aiohttp import ClientError, ClientTimeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_CAMERA_SOURCE, CONF_GO2RTC_URL, CONF_TOKEN

_LOGGER = logging.getLogger(__name__)

SOURCE_AUTO = "auto"
SOURCE_WEBRTC = "webrtc"
SOURCE_GO2RTC = "go2rtc"
SOURCES = (SOURCE_AUTO, SOURCE_WEBRTC, SOURCE_GO2RTC)

WEBRTC_DOMAIN = "webrtc"
FRIGATE_DOMAIN = "frigate"
GO2RTC_PORT = 1984
HA_GO2RTC_PORT = 11984  # Home Assistant's own go2rtc (reachable only with debug_ui on).
# Reached by Home Assistant itself (frames): WebRTC Camera's and Home Assistant's go2rtc.
LOCAL_GO2RTC = ("http://127.0.0.1:1984", f"http://127.0.0.1:{HA_GO2RTC_PORT}")
SNAPSHOT_REFRESH_S = 1.0  # Background fetch interval while a panel watches.
SNAPSHOT_WATCH_S = 12.0  # Keep fetching this long after the last request.
SNAPSHOT_STALE_S = 30.0  # Older than this: fetch before answering.
READER_FIRST_S = 10.0  # ffmpeg: the first key frame must come within this.
READER_GAP_S = 8.0  # ffmpeg: and the next ones within this.
READER_WIDTH = 960  # ffmpeg: frames are scaled down to at most this width.
READER_BUFFER = 4 * 1024 * 1024
STREAM_TICK_S = 0.2  # MJPEG: how often new frames are looked for.
STREAM_KEEPALIVE_S = 3.0  # MJPEG: resend the last frame after this long.
STREAM_LIMIT_S = 1800.0  # MJPEG: the panel reconnects after this.
LIVE_TTL_S = 300.0  # Resolved live sources are reused (WebRTC links survive until a restart).
RTSP_PORT = 8554
STREAMS_TTL_S = 60
ENTITY_RE = re.compile(r"^camera\.[a-z0-9_]+$")
# Snapshots also serve image entities (e.g. a vacuum map), which have no stream.
SNAPSHOT_RE = re.compile(r"^(camera|image)\.[a-z0-9_]+$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

PLAYER = "&mode=webrtc,mse&media=video"
# The embed page's header and the video controls are not wanted on the panel.
EMBED_STYLE = ".header{display:none}video::-webkit-media-controls{display:none!important}"


@dataclass(frozen=True)
class Live:
    """Where a camera plays."""

    kind: str  # SOURCE_WEBRTC or SOURCE_GO2RTC
    page: str  # The player page to frame.
    audio: str | None  # RTSP address of its sound, when known.


def _host(url: str) -> str:
    return urlsplit(url).hostname or ""


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class Cameras:
    """Finds the live source of cameras; shared by every panel."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._streams: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._lock = asyncio.Lock()
        self._live: dict[tuple[str, str, str, str | None], tuple[float, Live]] = {}

    # ------------------------------------------------------------ go2rtc
    async def _list(self, server: str) -> dict[str, Any] | None:
        """go2rtc's streams (name -> producers), cached for a minute; None when unreachable."""
        now = time.monotonic()
        cached = self._streams.get(server)
        if cached and now - cached[0] < STREAMS_TTL_S:
            return cached[1]
        session = async_get_clientsession(self.hass)
        streams: dict[str, Any] | None = None
        try:
            async with session.get(f"{server}/api/streams", timeout=ClientTimeout(total=2)) as response:
                if response.status == 200:
                    data = json.loads(await response.text())
                    streams = data if isinstance(data, dict) else {}
        except (ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("go2rtc at %s not reachable: %s", server, err)
        self._streams[server] = (now, streams)
        return streams

    def _servers(self, options: dict[str, Any], origin: str, local: bool = False) -> list[str]:
        """go2rtc servers to try, the configured one first.

        local: for Home Assistant's own requests (frames), which also reach the
        go2rtc instances listening only on this host.
        """
        servers: list[str] = []
        configured = (options.get(CONF_GO2RTC_URL) or "").strip().rstrip("/")
        if configured:
            servers.append(configured)
        for entry in self.hass.config_entries.async_entries(FRIGATE_DOMAIN):
            host = _host(str(entry.data.get("url", "")))
            if host:
                servers.append(f"http://{host}:{GO2RTC_PORT}")
        host = _host(origin)
        if host:
            servers.append(f"http://{host}:{HA_GO2RTC_PORT}")
        if local:
            servers.extend(LOCAL_GO2RTC)
        return list(dict.fromkeys(servers))

    async def _source_url(self, entity: str) -> str | None:
        from homeassistant.components import camera  # noqa: PLC0415 - loaded with its turbojpeg only when used.

        try:
            return await camera.async_get_stream_source(self.hass, entity)
        except Exception:  # noqa: BLE001 - any camera platform error means no source.
            return None

    def _names(self, entity: str) -> list[str]:
        """Stream names a camera is likely to have: its object id, then its names."""
        object_id = entity.split(".", 1)[1]
        names = [object_id, entity]
        entry = er.async_get(self.hass).async_get(entity)
        if entry is not None:
            if entry.platform == FRIGATE_DOMAIN and entry.unique_id:
                # Frigate: "<entry id>:camera:<camera name>".
                names.append(entry.unique_id.rsplit(":", 1)[-1])
            if entry.original_name:
                names.append(entry.original_name)
        state = self.hass.states.get(entity)
        if state is not None and state.name:
            names.append(state.name)
        return names

    async def _stream(self, server: str, streams: dict[str, Any], entity: str, hint: str | None,
                      add: bool = True) -> str | None:
        """The go2rtc stream for a camera, adding one from its source when none matches (add)."""
        if hint and hint in streams:
            return hint
        names = self._names(entity)
        for name in names:
            if name in streams:
                return name
        wanted = {_norm(n) for n in names}
        for name in streams:
            if _norm(name) in wanted:
                return name
        source = await self._source_url(entity)
        if not source:
            return None
        bare = source.split("#", 1)[0]
        for name, info in streams.items():
            producers = (info or {}).get("producers") or []
            if any(str(p.get("url", "")).split("#", 1)[0] == bare for p in producers if isinstance(p, dict)):
                return name
        if not add:
            return None
        # Not in go2rtc yet: add it under the entity's name.
        name = entity.replace(".", "_")
        session = async_get_clientsession(self.hass)
        try:
            async with session.put(
                f"{server}/api/streams", params={"name": name, "src": source}, timeout=ClientTimeout(total=4)
            ) as response:
                if response.status >= 400:
                    return None
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not add %s to go2rtc at %s: %s", entity, server, err)
            return None
        self._streams.pop(server, None)
        return name

    async def _go2rtc(self, options: dict[str, Any], origin: str, entity: str, hint: str | None) -> Live | None:
        for server in self._servers(options, origin):
            streams = await self._list(server)
            if streams is None:
                continue
            name = await self._stream(server, streams, entity, hint)
            if name:
                return Live(
                    SOURCE_GO2RTC,
                    f"{server}/stream.html?src={quote(name, safe='')}{PLAYER}",
                    f"rtsp://{_host(server)}:{RTSP_PORT}/{name}",
                )
        return None

    # ------------------------------------------------------------ WebRTC Camera
    async def _webrtc(self, token: str, origin: str, entity: str, hint: str | None) -> Live | None:
        if not self.hass.services.has_service(WEBRTC_DOMAIN, "create_link"):
            return None
        # Links play without sign-in, so their ids must not be guessable.
        link = "rc_" + hashlib.sha256(f"{token}:{entity}".encode()).hexdigest()[:24]
        data: dict[str, Any] = {"link_id": link, "open_limit": 0, "time_to_live": 0}
        if hint and "://" in hint:
            data["url"] = hint
        else:
            data["entity"] = entity
        try:
            await self.hass.services.async_call(WEBRTC_DOMAIN, "create_link", data, blocking=True)
        except Exception as err:  # noqa: BLE001 - the integration's own errors.
            _LOGGER.warning("webrtc.create_link failed for %s: %s", entity, err)
            return None
        query = urlencode({"url": link, "style": EMBED_STYLE})
        page = (f"{origin}/webrtc/embed?{query}{PLAYER}&muted=true&intersection=0"
                "&background=true&digital_ptz=false")
        # WebRTC Camera's go2rtc names the stream after the entity while it plays.
        return Live(SOURCE_WEBRTC, page, f"rtsp://{_host(origin)}:{RTSP_PORT}/{hint or entity}")

    async def resolve(self, options: dict[str, Any], token: str, origin: str, entity: str,
                      hint: str | None = None) -> Live | None:
        """Where the camera plays, or None."""
        if not ENTITY_RE.match(entity):
            return None
        if hint is not None and not (NAME_RE.match(hint) or hint.startswith(("rtsp://", "rtsps://", "rtmp://"))):
            hint = None
        choice = options.get(CONF_CAMERA_SOURCE, SOURCE_AUTO)
        key = (token, origin, entity, hint)
        async with self._lock:
            cached = self._live.get(key)
            if cached and time.monotonic() - cached[0] < LIVE_TTL_S:
                return cached[1]
            found = None
            if choice in (SOURCE_AUTO, SOURCE_WEBRTC):
                found = await self._webrtc(token, origin, entity, hint)
            if found is None and choice != SOURCE_WEBRTC:
                found = await self._go2rtc(options, origin, entity, hint)
            if found is not None:
                self._live[key] = (time.monotonic(), found)
            return found

    async def frame(self, options: dict[str, Any], origin: str, entity: str) -> bytes | None:
        """A JPEG frame of the camera's running go2rtc stream, or None."""
        session = async_get_clientsession(self.hass)
        for server in self._servers(options, origin, local=True):
            streams = await self._list(server)
            if streams is None:
                continue
            # Only streams go2rtc already has: frames do not add cameras to it.
            name = await self._stream(server, streams, entity, None, add=False)
            if not name:
                continue
            try:
                async with session.get(f"{server}/api/frame.jpeg", params={"src": name},
                                       timeout=ClientTimeout(total=6)) as response:
                    if response.status == 200:
                        data = await response.read()
                        if data:
                            return data
            except (ClientError, TimeoutError) as err:
                _LOGGER.debug("No frame of %s from %s: %s", entity, server, err)
        return None


    async def rtsp(self, options: dict[str, Any], origin: str, entity: str) -> str | None:
        """An RTSP address of the camera: go2rtc's restream when one has it, else its own."""
        for server in self._servers(options, origin, local=True):
            streams = await self._list(server)
            if streams is None:
                continue
            name = await self._stream(server, streams, entity, None, add=False)
            if name:
                return f"rtsp://{_host(server)}:{RTSP_PORT}/{quote(name, safe='.-_')}"
        source = await self._source_url(entity)
        return source if source and source.startswith(("rtsp://", "rtsps://")) else None


class Snapshots:
    """Latest frames of the cameras panels look at, fetched ahead."""

    def __init__(self, hass: HomeAssistant, cams: Cameras) -> None:
        self.hass = hass
        self.cameras = cams
        self._latest: dict[str, tuple[float, bytes]] = {}
        self._wanted: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._fetching: dict[str, asyncio.Task[bytes | None]] = {}
        self._sources: dict[str, tuple[dict[str, Any], str]] = {}
        self._image_versions: dict[str, str | None] = {}

    async def _grab(self, entity: str) -> bytes | None:
        options, origin = self._sources[entity]
        if entity.startswith("image."):
            # An image entity's state is when its picture last changed: read the
            # picture again only then, and keep serving the same frame otherwise
            # (same frame time, so the sized copy is reused too).
            state = self.hass.states.get(entity)
            version = state.state if state else None
            kept = self._latest.get(entity)
            if kept is not None and version is not None and self._image_versions.get(entity) == version:
                return kept[1]
            from homeassistant.components import image  # noqa: PLC0415

            try:
                data = (await image.async_get_image(self.hass, entity, timeout=10)).content
            except Exception as err:  # noqa: BLE001 - unavailable images raise various errors.
                _LOGGER.debug("No picture of %s: %s", entity, err)
                return None
            self._image_versions[entity] = version
            self._latest[entity] = (time.monotonic(), data)
            return data
        data = await self.cameras.frame(options, origin, entity)
        if data is None:
            from homeassistant.components import camera  # noqa: PLC0415

            try:
                image = await camera.async_get_image(self.hass, entity, timeout=10)
                data = image.content
            except Exception as err:  # noqa: BLE001 - unavailable cameras raise various errors.
                _LOGGER.debug("No snapshot of %s: %s", entity, err)
                return None
        self._latest[entity] = (time.monotonic(), data)
        return data

    async def _fetch(self, entity: str) -> bytes | None:
        """One fetch at a time per camera; concurrent callers share it."""
        pending = self._fetching.get(entity)
        if pending is None:
            pending = self.hass.async_create_task(self._grab(entity), eager_start=False)
            self._fetching[entity] = pending
            pending.add_done_callback(lambda _: self._fetching.pop(entity, None))
        return await asyncio.shield(pending)

    def _watched(self, entity: str) -> bool:
        return time.monotonic() - self._wanted.get(entity, 0) < SNAPSHOT_WATCH_S

    def _ffmpeg(self) -> str | None:
        try:
            from homeassistant.components.ffmpeg import get_ffmpeg_manager  # noqa: PLC0415

            return get_ffmpeg_manager(self.hass).binary
        except Exception:  # noqa: BLE001 - not installed or not set up.
            return shutil.which("ffmpeg")

    async def _read(self, entity: str) -> bool:
        """Key frames from ffmpeg while the camera is watched; False when that cannot run."""
        binary = self._ffmpeg()
        options, origin = self._sources[entity]
        url = await self.cameras.rtsp(options, origin, entity) if binary else None
        if not binary or not url:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-hide_banner", "-loglevel", "error", "-nostdin",
                *(["-rtsp_transport", "tcp"] if url.startswith("rtsp") else []),
                "-skip_frame", "nokey", "-i", url, "-an", "-sn", "-dn",
                "-fps_mode", "passthrough", "-vf", f"scale=w=min({READER_WIDTH}\\,iw):h=-2",
                "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as err:
            _LOGGER.debug("ffmpeg could not start for %s: %s", entity, err)
            return False
        frames, buffer = 0, b""
        try:
            while self._watched(entity):
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), READER_GAP_S if frames else READER_FIRST_S)
                except TimeoutError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if end < 0:
                        buffer = buffer[start:] if 0 <= start and len(buffer) - start < READER_BUFFER else b""
                        break
                    self._latest[entity] = (time.monotonic(), buffer[start:end + 2])
                    frames += 1
                    buffer = buffer[end + 2:]
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        _LOGGER.debug("ffmpeg read %d key frames of %s", frames, entity)
        return frames > 0

    async def _watch(self, entity: str) -> None:
        try:
            # ffmpeg until it stops; single frames for the rest of the time.
            if await self._read(entity) and not self._watched(entity):
                return
            while self._watched(entity):
                started = time.monotonic()
                await self._fetch(entity)
                await asyncio.sleep(max(0.1, SNAPSHOT_REFRESH_S - (time.monotonic() - started)))
        finally:
            self._tasks.pop(entity, None)

    def _keep_fresh(self, entity: str) -> None:
        if entity not in self._tasks:
            self._tasks[entity] = self.hass.async_create_background_task(
                self._watch(entity), f"room_companion snapshots {entity}")

    def latest(self, entity: str) -> tuple[float, bytes] | None:
        """The newest frame (monotonic time, JPEG) without fetching."""
        return self._latest.get(entity)

    async def get(self, options: dict[str, Any], origin: str, entity: str) -> bytes | None:
        """The latest frame, fetched now only when there is none recent."""
        self._wanted[entity] = time.monotonic()
        self._sources[entity] = (options, origin)
        latest = self._latest.get(entity)
        if latest is None or time.monotonic() - latest[0] > SNAPSHOT_STALE_S:
            data = await self._fetch(entity)
        else:
            data = latest[1]
        self._keep_fresh(entity)
        return data

    def touch(self, entity: str) -> None:
        """A stream still shows the camera: keep fetching."""
        self._wanted[entity] = time.monotonic()
        self._keep_fresh(entity)

    async def async_stop(self) -> None:
        """Cancel the background fetches."""
        for task in list(self._tasks.values()):
            task.cancel()


def page(live: Live | None, query: dict[str, str]) -> str:
    """The page the panel's stream server opens: the player placed over the camera frame.

    x, y, w, h: the frame in percent of the screen; r: full screen on a portrait
    screen, the player turned by r degrees.
    """
    if live is None:
        body = '<p class="none">No live stream for this camera</p>'
    else:
        body = f'<iframe id="f" allow="autoplay" scrolling="no" src="{html.escape(live.page)}"></iframe>'

    def number(name: str) -> float | None:
        try:
            value = float(query.get(name, ""))
        except ValueError:
            return None
        return value if -1000 < value < 1000 else None

    style = ""
    turn = number("r")
    if turn:
        style = ("left:50%;top:50%;width:100vh;height:100vw;"
                 f"transform:translate(-50%,-50%) rotate({turn:g}deg)")
    else:
        for key, prop in (("x", "left"), ("y", "top"), ("w", "width"), ("h", "height")):
            value = number(key)
            if value is not None:
                style += f"{prop}:{value:g}%;"
    return (
        "<!doctype html><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1"><title>Camera</title>'
        "<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}"
        "#f,.none{position:fixed;left:0;top:0;width:100%;height:100%;border:0;background:#000;margin:0}"
        ".none{display:flex;align-items:center;justify-content:center;color:#888;font:16px sans-serif}"
        f"#f,.none{{{style}}}</style>{body}"
    )


def entry_token(entry: Any) -> str:
    """The panel's secret token (used to name its WebRTC links)."""
    return str(entry.data[CONF_TOKEN])
