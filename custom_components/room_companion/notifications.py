"""Notifications on a panel: messages with locally served images, and KDE forwarding.

Replaces the notifications_on_esphome_display blueprint: KDE Connect
notifications shown by KDE for Home Assistant go to the panel and are cleared
when removed on the phone. No Text helper is needed; the integration remembers
which notification is on screen.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import urljoin

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from . import panel
from .assets import AssetStore
from .const import ACTION_DISMISS_MESSAGE, ACTION_SHOW_MESSAGE, KDE_EVENT

_LOGGER = logging.getLogger(__name__)

# The panel's notification image box (3.5" panel; the 2.8" scales it down).
IMAGE_BOX = 240


def internal_url(hass: HomeAssistant) -> str | None:
    """Home Assistant's address as seen from the LAN."""
    try:
        return get_url(hass, allow_external=False, allow_cloud=False, prefer_external=False)
    except NoURLAvailableError:
        return None


class Messenger:
    """Shows and dismisses messages on one panel."""

    def __init__(self, hass: HomeAssistant, store: AssetStore, prefix: str, base: str | None,
                 timeout: int) -> None:
        self.hass = hass
        self.store = store
        self.prefix = prefix
        self.base = base  # .../api/room_companion/<token>, or None without a reachable URL.
        self.timeout = timeout
        self.active_kde: str | None = None

    def local_image(self, url: str, key: str | None = None) -> str:
        """Serve an image through Home Assistant, sized for the panel."""
        if not url:
            return ""
        if url.startswith("/"):
            origin = internal_url(self.hass)
            if origin is None:
                return ""
            url = urljoin(origin + "/", url.lstrip("/"))
        if not url.startswith(("http://", "https://")) or self.base is None:
            return url if url.startswith(("http://", "https://")) else ""
        key = self.store.add_image(url, key)
        return f"{self.base}/image/{key}.png?w={IMAGE_BOX}&h={IMAGE_BOX}"

    async def show(self, title: str, message: str, image_url: str = "",
                   timeout: int | None = None, *, local: bool = False) -> bool:
        return await panel.call(self.hass, self.prefix, ACTION_SHOW_MESSAGE, {
            "title": title[:200],
            "message": message[:1000],
            "image_url": (image_url if local else self.local_image(image_url))[:1024],
            "timeout_seconds": self.timeout if timeout is None else max(0, int(timeout)),
        })

    async def dismiss(self) -> bool:
        return await panel.call(self.hass, self.prefix, ACTION_DISMISS_MESSAGE)

    # KDE for Home Assistant forwarding.
    @callback
    def listen_kde(self, devices: list[str]):
        """Forward kdeforhomeassistant_notification events; returns the unsubscribe."""
        allowed = set(devices)

        @callback
        def _filter(event_data) -> bool:
            return not allowed or event_data.get("device_id") in allowed

        async def _handle(event: Event) -> None:
            data = event.data
            key = f"{data.get('device_id')}/{data.get('id')}"
            action = data.get("action")
            if action in ("added", "updated"):
                app = str(data.get("app") or "")
                title = str(data.get("title") or app)
                # The panel joins cards with the same title and picture URL.
                # KDE for Home Assistant serves every icon under a new token,
                # so each message of a joined desktop chat (one stable id)
                # would get a card of its own. One notification keeps one
                # picture URL, pointed at its newest icon; the app and title
                # keep a reused phone id from showing another app's icon.
                image_key = hashlib.sha256(
                    "\0".join(("kde", key, app, title)).encode()).hexdigest()[:24]
                if await self.show(
                    title,
                    str(data.get("text") or data.get("ticker") or app),
                    self.local_image(str(data.get("icon_url") or ""), image_key),
                    local=True,
                ):
                    self.active_kde = key
            elif action == "removed" and key == self.active_kde:
                await self.dismiss()
                self.active_kde = None

        return self.hass.bus.async_listen(KDE_EVENT, _handle, event_filter=_filter)
