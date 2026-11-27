"""KDE notifications on the panel."""

from homeassistant.core import HomeAssistant

from custom_components.room_companion.assets import AssetStore
from custom_components.room_companion.const import KDE_EVENT
from custom_components.room_companion.notifications import Messenger

from .conftest import PREFIX

BASE = "http://ha:8123/api/room_companion/tok"
ICONS = "http://ha:8123/api/kdeforhomeassistant/notification_icon"


async def _send(hass: HomeAssistant, **data) -> None:
    hass.bus.async_fire(KDE_EVENT, {"device_id": "laptop", "action": "added", **data})
    await hass.async_block_till_done()


async def test_joined_chat_keeps_one_picture_url(hass: HomeAssistant, panel_calls) -> None:
    """Every message of a joined chat brings a new icon token; the panel URL stays."""
    store = AssetStore(hass)
    messenger = Messenger(hass, store, PREFIX, BASE, 10)
    unsub = messenger.listen_kde([])

    await _send(hass, id="chat:a", app="Signal", title="Ana", text="hi", icon_url=f"{ICONS}/one.png")
    await _send(hass, id="chat:a", app="Signal", title="Ana", text="hi\nthere",
                icon_url=f"{ICONS}/two.png", action="updated")
    await _send(hass, id="chat:b", app="Signal", title="Bo", text="yo", icon_url=f"{ICONS}/three.png")
    # A phone reusing an id for another app gets its own picture.
    await _send(hass, id="chat:a", app="Mail", title="Ana", text="mail", icon_url=f"{ICONS}/four.png")
    await _send(hass, id="chat:a", app="Signal", title="Ana", text="no icon")
    unsub()

    urls = [call.data["image_url"] for call in panel_calls]
    assert urls[0] == urls[1] and urls[0].startswith(BASE + "/image/")
    assert len({urls[0], urls[2], urls[3]}) == 3
    assert urls[4] == ""
    # The stable key serves the newest icon.
    key = urls[1].split("/image/")[1].split(".png")[0]
    assert store._sources[key] == f"{ICONS}/two.png"
