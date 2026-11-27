"""Setup, config flow, services, lyrics and assets."""

from http import HTTPStatus
import io
from unittest.mock import patch

from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.room_companion.const import (
    CONF_LYRICS,
    CONF_MEDIA_PLAYERS,
    CONF_PREFIX,
    CONF_TOKEN,
    DOMAIN,
    EMOJI_PNG_URL,
    LRCLIB_URL,
)

from .conftest import PREFIX

TOKEN = "test-token-123"


async def _setup(hass: HomeAssistant, options=None) -> MockConfigEntry:
    await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=PREFIX, title="Panel",
        data={CONF_PREFIX: PREFIX, CONF_TOKEN: TOKEN}, options=options or {},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_flow_finds_panel(hass: HomeAssistant, panel_calls) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PREFIX: PREFIX})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PREFIX] == PREFIX
    assert len(result["data"][CONF_TOKEN]) >= 24


async def test_flow_without_panels(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_panels"


async def test_show_message_and_notify(hass: HomeAssistant, panel_calls) -> None:
    entry = await _setup(hass)
    await hass.services.async_call(DOMAIN, "show_message", {
        "panel": entry.entry_id, "title": "Hi", "message": "There", "timeout": 5,
        "image_url": "http://example.com/a.png",
    }, blocking=True)
    call = panel_calls[-1]
    assert call.service == f"{PREFIX}_show_message"
    assert call.data["title"] == "Hi" and call.data["timeout_seconds"] == 5
    assert f"/api/room_companion/{TOKEN}/image/" in call.data["image_url"]

    await hass.services.async_call("notify", "send_message", {
        "entity_id": hass.states.async_entity_ids("notify")[0], "message": "Hello",
    }, blocking=True)
    assert panel_calls[-1].data["message"] == "Hello"


async def test_lyrics_sent_on_track(hass: HomeAssistant, panel_calls, aioclient_mock) -> None:
    aioclient_mock.get(LRCLIB_URL, json={"syncedLyrics": "[00:01.00] la"})
    await _setup(hass, {CONF_MEDIA_PLAYERS: ["media_player.spotify"], CONF_LYRICS: True})
    hass.states.async_set("media_player.spotify", "playing", {
        "media_title": "Song", "media_artist": "Band", "media_duration": 200,
    })
    await hass.async_block_till_done()
    sent = [c for c in panel_calls if c.service.endswith("_set_lyrics")]
    assert sent[-1].data == {"entity_id": "media_player.spotify", "title": "Song", "lrc": "[00:01.00] la"}
    assert aioclient_mock.call_count == 1

    hass.states.async_set("media_player.spotify", "off", {})
    await hass.async_block_till_done()
    sent = [c for c in panel_calls if c.service.endswith("_set_lyrics")]
    assert sent[-1].data["lrc"] == "" and sent[-1].data["title"] == ""


async def test_emoji_served_and_cached(hass: HomeAssistant, hass_client_no_auth, aioclient_mock, tmp_path) -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (128, 128), (255, 200, 0, 255)).save(buffer, "PNG")
    aioclient_mock.get(EMOJI_PNG_URL.format(stem="1f600"), content=buffer.getvalue())
    with patch.object(hass.config, "config_dir", str(tmp_path)):
        await _setup(hass)
        client = await hass_client_no_auth()
        response = await client.get(f"/api/room_companion/{TOKEN}/emoji/1f600.bgra?size=48")
        assert response.status == HTTPStatus.OK
        assert len(await response.read()) == 48 * 48 * 4
        response = await client.get(f"/api/room_companion/{TOKEN}/emoji/1f600.png?size=32")
        assert response.status == HTTPStatus.OK
        assert aioclient_mock.call_count == 1  # The second size came from the disk cache.
        assert (await client.get(f"/api/room_companion/wrong/emoji/1f600.png")).status == HTTPStatus.NOT_FOUND
        assert (await client.get(f"/api/room_companion/{TOKEN}/emoji/..%2Fx.png")).status in (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND)
        assert (await client.get(f"/api/room_companion/{TOKEN}/emoji/zz.png")).status == HTTPStatus.NOT_FOUND
        info = await (await client.get(f"/api/room_companion/{TOKEN}/info")).json()
        assert info["api"] == 1


async def test_flow_finds_knob(hass: HomeAssistant, knob_calls) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PREFIX: "onx2424g013_ab12cd"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_announces_assets(hass: HomeAssistant, panel_calls) -> None:
    await _setup(hass)
    sent = [c for c in panel_calls if c.service.endswith("_set_companion")]
    assert sent and sent[-1].data["url"].endswith(f"/api/room_companion/{TOKEN}")


async def test_lyrics_offset_number(hass: HomeAssistant, panel_calls) -> None:
    await _setup(hass)
    entity_id = hass.states.async_entity_ids("number")[0]
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": 1.5}, blocking=True)
    sent = [c for c in panel_calls if c.service.endswith("_set_lyrics_offset")]
    assert sent[-1].data == {"offset_ms": 1500}
    assert hass.states.get(entity_id).state == "1.5"


async def test_mid_song_request(hass: HomeAssistant, panel_calls, aioclient_mock) -> None:
    """A panel that opens a player mid-song asks for its lyrics."""
    from homeassistant.helpers import device_registry as dr
    from custom_components.room_companion.const import LYRICS_REQUEST_EVENT

    aioclient_mock.get(LRCLIB_URL, json={"syncedLyrics": "[00:01.00] mid"})
    esphome = MockConfigEntry(domain="esphome", data={"device_name": "onx3248g035-dbbc54"})
    esphome.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome.entry_id, identifiers={("esphome", "aa:bb")}, name="ONX3248G035 dbbc54",
        model="ESPHome Room Companion ONX3248G035",
    )
    hass.states.async_set("media_player.radio", "playing", {"media_title": "Song", "media_artist": "Band"})
    await _setup(hass)  # The radio is not one of the configured players.
    hass.bus.async_fire(LYRICS_REQUEST_EVENT, {"device_id": "other", "entity_id": "media_player.radio"})
    await hass.async_block_till_done()
    assert not [c for c in panel_calls if c.service.endswith("_set_lyrics")]
    hass.bus.async_fire(LYRICS_REQUEST_EVENT, {"device_id": device.id, "entity_id": "media_player.radio"})
    await hass.async_block_till_done()
    sent = [c for c in panel_calls if c.service.endswith("_set_lyrics")]
    assert sent[-1].data == {"entity_id": "media_player.radio", "title": "Song", "lrc": "[00:01.00] mid"}
