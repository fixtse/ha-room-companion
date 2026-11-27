"""Local assets for the panels: covers, backdrops, emoji, icons, glyphs and images.

The panel fetches everything over plain HTTP from Home Assistant on the LAN,
already sized and, for raw formats, already decoded. Remote sources are
downloaded once and kept in a disk cache; rendered results stay in memory.

URLs are /api/room_companion/<token>/<kind>/<name>. The token is a per-panel
secret (the panel cannot send Home Assistant credentials with each request).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
import hashlib
import hmac
import logging
from pathlib import Path
import re
import time
from typing import Any

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import cameras, imaging
from .cameras import Cameras, Snapshots, entry_token
from .const import (
    API_VERSION,
    CONF_GLYPH_FONT,
    CONF_TOKEN,
    DOMAIN,
    EMOJI_PNG_URL,
    EMOJI_SVG_URL,
    MDI_SVG_BACKUP_URL,
    MDI_SVG_URL,
    URL_BASE,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

REMOTE_LIMIT = 8 * 1024 * 1024
MEMORY_LIMIT = 48 * 1024 * 1024
MISS_RETRY_S = 6 * 3600
SOURCE_LIMIT = 32  # Notification images kept for the panel to fetch.

EMOJI_RE = re.compile(r"^[0-9a-f]{2,6}(?:_[0-9a-f]{2,6}){0,9}$")
MDI_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GLYPH_RE = re.compile(r"^[0-9a-f]{2,6}$")
KEY_RE = re.compile(r"^[0-9a-f]{24}$")


def _int(request: web.Request, name: str, default: int, low: int, high: int) -> int:
    try:
        return imaging.clamp(int(request.query.get(name, default)), low, high)
    except ValueError:
        return default


def _fmt(request: web.Request, default: str, allowed: tuple[str, ...] = imaging.FORMATS) -> str:
    value = request.query.get("fmt", default)
    return value if value in allowed else default


def _split_ext(name: str, default: str) -> tuple[str, str]:
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        return stem, ext
    return name, default


class AssetStore:
    """Downloads, caches and renders the panels' assets."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.cache_dir = Path(hass.config.path(".cache", DOMAIN))
        self._memory: OrderedDict[str, tuple[bytes, str, dict[str, str]]] = OrderedDict()
        self._memory_bytes = 0
        self._misses: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Notification images: key -> source URL, newest last.
        self._sources: OrderedDict[str, str] = OrderedDict()

    # Memory cache of rendered responses.
    def _get(self, key: str) -> tuple[bytes, str, dict[str, str]] | None:
        found = self._memory.get(key)
        if found is not None:
            self._memory.move_to_end(key)
        return found

    def _put(self, key: str, value: tuple[bytes, str, dict[str, str]]) -> None:
        old = self._memory.pop(key, None)
        if old is not None:
            self._memory_bytes -= len(old[0])
        self._memory[key] = value
        self._memory_bytes += len(value[0])
        while self._memory_bytes > MEMORY_LIMIT and len(self._memory) > 1:
            _, dropped = self._memory.popitem(last=False)
            self._memory_bytes -= len(dropped[0])

    async def _cached(
        self, key: str, make: Callable[[], Awaitable[tuple[bytes, str, dict[str, str]] | None]]
    ) -> tuple[bytes, str, dict[str, str]] | None:
        if (found := self._get(key)) is not None:
            return found
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                if (found := self._get(key)) is not None:
                    return found
                made = await make()
                if made is not None:
                    self._put(key, made)
                return made
        finally:
            if not lock.locked():
                self._locks.pop(key, None)

    # Remote sources with a disk cache.
    async def _download(self, url: str) -> bytes | None:
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=ClientTimeout(total=20)
            ) as response:
                if response.status != 200:
                    _LOGGER.debug("%s returned HTTP %s", url, response.status)
                    return None
                data = await response.content.read(REMOTE_LIMIT + 1)
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Downloading %s failed: %s", url, err)
            return None
        return data if 0 < len(data) <= REMOTE_LIMIT else None

    def _read(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _write(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)

    async def remote(self, cache_name: str, urls: list[str]) -> bytes | None:
        """A remote file, from the disk cache or the first URL that works."""
        path = self.cache_dir / cache_name
        data = await self.hass.async_add_executor_job(self._read, path)
        if data is not None:
            return data
        missed = self._misses.get(cache_name)
        if missed is not None and time.monotonic() - missed < MISS_RETRY_S:
            return None
        for url in urls:
            if (data := await self._download(url)) is not None:
                await self.hass.async_add_executor_job(self._write, path, data)
                self._misses.pop(cache_name, None)
                return data
        self._misses[cache_name] = time.monotonic()
        return None

    # Kinds.
    async def emoji(self, stem: str, size: int, fmt: str) -> tuple[bytes, str, dict[str, str]] | None:
        """Noto Color Emoji by file stem (lowercase code points joined by '_', no FE0F)."""
        if fmt == "svg":
            async def make_svg() -> tuple[bytes, str, dict[str, str]] | None:
                data = await self.remote(f"emoji/{stem}.svg", [EMOJI_SVG_URL.format(stem=stem)])
                return None if data is None else (data, "image/svg+xml", {})
            return await self._cached(f"emoji:{stem}:svg", make_svg)

        async def make() -> tuple[bytes, str, dict[str, str]] | None:
            data = await self.remote(f"emoji/{stem}.png", [EMOJI_PNG_URL.format(stem=stem)])
            if data is None:
                return None
            out = await self.hass.async_add_executor_job(imaging.square, data, size, fmt)
            return out.data, out.content_type, _size_headers(out)
        return await self._cached(f"emoji:{stem}:{size}:{fmt}", make)

    async def icon(self, name: str, size: int) -> tuple[bytes, str, dict[str, str]] | None:
        """A white Material Design Icon as SVG, sized for the panel's rasteriser."""
        async def make() -> tuple[bytes, str, dict[str, str]] | None:
            data = await self.remote(
                f"mdi/{name}-{size}.svg",
                [MDI_SVG_URL.format(name=name, size=size), MDI_SVG_BACKUP_URL.format(name=name)],
            )
            if data is None or b"<svg" not in data[:512]:
                return None
            return data, "image/svg+xml", {}
        return await self._cached(f"mdi:{name}:{size}", make)

    async def cover(self, entity_id: str, request: web.Request) -> tuple[bytes, str, dict[str, str]] | None:
        """A media player's art.

        mode=cover (default): cropped to fill w x h (the knob's round screen).
        mode=fit: scaled into w x h keeping its shape (the panels).
        mode=blur: the blurred, darkened lyrics backdrop filling w x h.
        """
        component = self.hass.data.get("media_player")
        entity = component.get_entity(entity_id) if component is not None else None
        if entity is None:
            return None
        width = _int(request, "w", 96, 16, imaging.MAX_SIDE)
        height = _int(request, "h", width, 16, imaging.MAX_SIDE)
        mode = request.query.get("mode", "cover")
        if mode not in ("cover", "fit", "blur"):
            mode = "cover"
        fmt = _fmt(request, "jpeg", ("jpeg", "png", "rgb565"))
        swap = request.query.get("swap") == "1"
        quality = _int(request, "q", 85, 30, 95)
        dim = _int(request, "dim", 62, 10, 100) / 100
        art = getattr(entity, "media_image_hash", None) or getattr(entity, "entity_picture", None)
        if not art:
            return None
        key = f"cover:{entity_id}:{art}:{mode}:{width}x{height}:{fmt}:{swap}:{quality}:{dim}"

        async def make() -> tuple[bytes, str, dict[str, str]] | None:
            try:
                data, _ = await entity.async_get_media_image()
            except Exception as err:  # noqa: BLE001 - player integrations raise anything.
                _LOGGER.debug("No art for %s: %s", entity_id, err)
                return None
            if not data:
                return None
            if mode == "blur":
                out = await self.hass.async_add_executor_job(
                    imaging.backdrop, data, width, height, dim, fmt, quality, swap
                )
            else:
                out = await self.hass.async_add_executor_job(
                    imaging.fit if mode == "fit" else imaging.cover, data, width, height, fmt, quality, swap
                )
            headers = _size_headers(out)
            headers["ETag"] = '"' + hashlib.sha1(key.encode()).hexdigest()[:16] + '"'
            return out.data, out.content_type, headers
        return await self._cached(key, make)

    def add_image(self, url: str, key: str | None = None) -> str:
        """Remember an image URL for the panel and return its key.

        A given key keeps the panel's URL when the source URL changes.
        """
        key = key or hashlib.sha256(url.encode()).hexdigest()[:24]
        self._sources.pop(key, None)
        self._sources[key] = url
        while len(self._sources) > SOURCE_LIMIT:
            self._sources.popitem(last=False)
        return key

    async def image(self, key: str, request: web.Request) -> tuple[bytes, str, dict[str, str]] | None:
        """A notification image, shrunk to fit the requested box."""
        url = self._sources.get(key)
        if url is None:
            return None
        width = _int(request, "w", 320, 16, imaging.MAX_SIDE)
        height = _int(request, "h", 320, 16, imaging.MAX_SIDE)
        fmt = _fmt(request, "png", ("jpeg", "png", "rgb565", "bgra"))

        async def make() -> tuple[bytes, str, dict[str, str]] | None:
            data = await self._download(url)
            if data is None:
                return None
            try:
                out = await self.hass.async_add_executor_job(imaging.contain, data, width, height, fmt)
            except OSError as err:
                _LOGGER.debug("Unreadable image %s: %s", url, err)
                return None
            return out.data, out.content_type, _size_headers(out)
        # A notification keeps its key while its picture changes (KDE chats).
        source = hashlib.sha256(url.encode()).hexdigest()[:16]
        return await self._cached(f"image:{key}:{source}:{width}x{height}:{fmt}", make)

    async def glyph(self, font: str | None, codepoint: int, size: int, fmt: str) -> tuple[bytes, str, dict[str, str]] | None:
        """A character the panel's fonts lack: the chosen font first, then the bundled ones."""
        fonts = ([font] if font else []) + imaging.BUNDLED_FONTS

        async def make() -> tuple[bytes, str, dict[str, str]] | None:
            try:
                out = await self.hass.async_add_executor_job(imaging.glyph, fonts, chr(codepoint), size, fmt)
            except OSError as err:
                _LOGGER.warning("A glyph font cannot be read (%s): %s", ", ".join(fonts), err)
                return None
            if out is None:
                return None
            return out.data, out.content_type, {
                "X-Width": str(out.width), "X-Height": str(out.height),
                "X-Advance": str(out.advance), "X-Ascent": str(out.ascent),
            }
        return await self._cached(f"glyph:{font}:{codepoint}:{size}:{fmt}", make)


def _size_headers(out: imaging.Encoded) -> dict[str, str]:
    return {"X-Width": str(out.width), "X-Height": str(out.height)}


def _entry_for_token(hass: HomeAssistant, token: str) -> Any | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and hmac.compare_digest(
            str(entry.data.get(CONF_TOKEN, "")), token
        ):
            return entry
    return None


class InfoView(HomeAssistantView):
    """What this integration serves, so the firmware can probe for it."""

    url = URL_BASE + "/{token}/info"
    name = "api:room_companion:info"
    requires_auth = False

    async def get(self, request: web.Request, token: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry = _entry_for_token(hass, token)
        if entry is None:
            return web.Response(status=404)
        return self.json({
            "api": API_VERSION,
            "panel": entry.title,
            "kinds": ["cover", "emoji", "icon", "image", "glyph", "camera", "snapshot", "stream"],
        })


class AssetView(HomeAssistantView):
    """Serves one asset."""

    url = URL_BASE + "/{token}/{kind}/{name}"
    name = "api:room_companion:asset"
    requires_auth = False

    def __init__(self, store: AssetStore, cameras: Cameras, snapshots: Snapshots) -> None:
        self.store = store
        self.cameras = cameras
        self.snapshots = snapshots
        self._sized: dict[tuple[str, int, int], tuple[float, bytes]] = {}

    async def get(self, request: web.Request, token: str, kind: str, name: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry = _entry_for_token(hass, token)
        if entry is None:
            return web.Response(status=404)
        if kind == "camera":
            return await self._camera(entry, request, name)
        if kind in ("snapshot", "stream"):
            if not cameras.ENTITY_RE.match(name):
                return web.Response(status=404)
            if kind == "snapshot":
                return await self._snapshot(entry, request, name)
            return await self._stream(entry, request, name)
        found = await self._find(entry, request, kind, name)
        if found is None:
            return web.Response(status=404, headers={"Cache-Control": "no-store"})
        data, content_type, headers = found
        etag = headers.get("ETag")
        if etag and request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        cache = "no-cache" if kind == "cover" else "max-age=86400"
        return web.Response(
            body=data, content_type=content_type, headers={"Cache-Control": cache, **headers}
        )

    async def _camera(self, entry: Any, request: web.Request, entity: str) -> web.Response:
        """The live camera page the panel's stream server opens."""
        if not entity.startswith("camera."):
            return web.Response(status=404)
        # The origin the server's browser used: the WebRTC embed page must be framed from it.
        origin = f"{request.scheme}://{request.host}"
        live = await self.cameras.resolve(
            dict(entry.options), entry_token(entry), origin, entity, request.query.get("stream") or None
        )
        if live is None:
            _LOGGER.info("No live stream found for %s (WebRTC Camera or go2rtc)", entity)
        return web.Response(
            text=cameras.page(live, dict(request.query)), content_type="text/html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    def _watch(self, entry: Any, request: web.Request, entity: str) -> tuple[int, int]:
        """Starts watching a camera; returns the requested frame size."""
        options, origin = dict(entry.options), f"{request.scheme}://{request.host}"
        # live=1: the panel plays the camera live through this integration next;
        # resolve it now (WebRTC link, go2rtc stream) so the live page loads at once.
        if request.query.get("live") == "1":
            entry.async_create_background_task(
                request.app["hass"], self.cameras.resolve(options, entry_token(entry), origin, entity),
                f"room_companion live {entity}")
        return _int(request, "w", 320, 16, imaging.MAX_SIDE), _int(request, "h", 320, 16, imaging.MAX_SIDE)

    async def _sized_frame(self, entity: str, frame: tuple[float, bytes], w: int, h: int) -> bytes | None:
        """The frame scaled to fit w x h (JPEG), converted once per frame and size."""
        key = (entity, w, h)
        cached = self._sized.get(key)
        if cached and cached[0] == frame[0]:
            return cached[1]
        hass = self.store.hass
        try:
            out = await hass.async_add_executor_job(imaging.fit, frame[1], w, h, "jpeg", 80)
        except Exception as err:  # noqa: BLE001 - an undecodable frame.
            _LOGGER.debug("Snapshot of %s not readable: %s", entity, err)
            return None
        if len(self._sized) > 64:
            self._sized.clear()
        self._sized[key] = (frame[0], out.data)
        return out.data

    async def _snapshot(self, entry: Any, request: web.Request, entity: str) -> web.Response:
        """The camera's latest frame at the camera frame's size."""
        w, h = self._watch(entry, request, entity)
        data = await self.snapshots.get(dict(entry.options), f"{request.scheme}://{request.host}", entity)
        frame = self.snapshots.latest(entity)
        sized = await self._sized_frame(entity, frame, w, h) if data and frame else None
        if sized is None:
            return web.Response(status=404, headers={"Cache-Control": "no-store"})
        return web.Response(body=sized, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def _stream(self, entry: Any, request: web.Request, entity: str) -> web.StreamResponse:
        """MJPEG of the camera's frames at the camera frame's size, while the panel reads it."""
        w, h = self._watch(entry, request, entity)
        options, origin = dict(entry.options), f"{request.scheme}://{request.host}"
        await self.snapshots.get(options, origin, entity)
        response = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace;boundary=frame", "Cache-Control": "no-store"})
        await response.prepare(request)
        sent_at, last_write = None, 0.0
        loop = asyncio.get_running_loop()
        try:
            for _ in range(int(cameras.STREAM_LIMIT_S / cameras.STREAM_TICK_S)):
                self.snapshots.touch(entity)
                frame = self.snapshots.latest(entity)
                now = loop.time()
                # New frames as they come, the last one again now and then so the panel
                # sees the stream is alive.
                if frame and (frame[0] != sent_at or now - last_write > cameras.STREAM_KEEPALIVE_S):
                    sized = await self._sized_frame(entity, frame, w, h)
                    if sized:
                        await response.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(sized)).encode() + b"\r\n\r\n" + sized + b"\r\n")
                        sent_at, last_write = frame[0], now
                await asyncio.sleep(cameras.STREAM_TICK_S)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    async def _find(self, entry: Any, request: web.Request, kind: str, name: str) -> tuple[bytes, str, dict[str, str]] | None:
        store = self.store
        if kind == "cover":
            return await store.cover(name, request) if name.startswith("media_player.") else None
        if kind == "emoji":
            stem, ext = _split_ext(name, "png")
            if not EMOJI_RE.match(stem) or ext not in ("png", "bgra", "svg"):
                return None
            return await store.emoji(stem, _int(request, "size", 48, 8, 256), ext)
        if kind == "icon":
            stem, _ = _split_ext(name, "svg")
            stem = stem.removeprefix("mdi:").removeprefix("mdi-")
            if not MDI_RE.match(stem):
                return None
            return await store.icon(stem, _int(request, "size", 80, 8, 256))
        if kind == "image":
            stem, _ = _split_ext(name, "png")
            return await store.image(stem, request) if KEY_RE.match(stem) else None
        if kind == "glyph":
            font = entry.options.get(CONF_GLYPH_FONT)
            stem, ext = _split_ext(name, "a8")
            if not GLYPH_RE.match(stem) or ext not in ("a8", "a8h", "png") or not 0x20 <= int(stem, 16) <= 0x10FFFF:
                return None
            return await store.glyph(font, int(stem, 16), _int(request, "size", 24, 8, 128), ext)
        return None
