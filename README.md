# Room Companion for Home Assistant

<img src="icon.png" alt="" width="96" align="right">

The Home Assistant side of [ESPHome Room Companion](https://github.com/fixtse/ESPHome-Room-Companion)
panels (ONX3248G035 3.5" and ONX2432G028 2.8"). It takes over work the panels
and separate blueprints do today:

- **Synced lyrics.** When a chosen media player starts a track, Home Assistant
  asks [LRCLIB](https://lrclib.net) once and sends the timed lyrics to the
  panel. This replaces the *Room Companion synced lyrics* blueprint and its
  `rest_command`. Results are cached, so several panels cost one lookup.
- **Notifications.** A notify entity per panel (`notify.send_message`), a
  `room_companion.show_message` action whose picture Home Assistant downloads
  and serves to the panel at the right size, and optional forwarding of
  [KDE for Home Assistant](https://github.com/Radeaus/kdeforhomeassistant)
  notifications. This replaces the *notifications on an ESPHome display*
  blueprint and its Text helper.
- **Local images.** Covers sized for the panel, the blurred lyrics backdrop,
  Noto Color Emoji, Material Design Icons and characters the panel's fonts
  lack, all served from Home Assistant over the local network. Remote files
  are downloaded once and cached on disk. See [Asset API](#asset-api).
- **Setup check.** A repair appears when the panel's ESPHome option **Allow
  the device to perform Home Assistant actions** is off.

## Installation

### HACS

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=fixtse&repository=ha-room-companion&category=integration)

1. Open the button, or in HACS add `https://github.com/fixtse/ha-room-companion`
   as a custom repository of type **Integration**.
2. Download **Room Companion** and restart Home Assistant.

### Manual

Copy `custom_components/room_companion` into your configuration's
`custom_components` folder and restart Home Assistant.

## Setup

1. Add the panel with the **ESPHome** integration first.
2. Go to **Settings → Devices & services → Add integration → Room Companion**
   and pick the panel. Repeat for each panel.
3. Open **Configure** on the panel to choose:
   - **Media players**: the players the panel shows, for lyrics.
   - **Send synced lyrics**: on by default.
   - **Notification time**: default seconds a message stays (0 until dismissed).
   - **Show KDE Connect notifications**, optionally only from some devices.
   - **Home Assistant address for the panel**: only needed when Home
     Assistant's local network address is one the panel cannot reach (for
     example a VPN address).
   - **Font for missing characters**: a TTF/OTF file on the Home Assistant
     host, for example `/config/fonts/NotoSansSC-Regular.otf`, used to draw
     characters the panel's fonts do not have.

If you used the blueprints before, delete those automations (and the
`room_companion_lrclib` `rest_command` and the Text helper) so messages and
lyrics are not sent twice.

## Actions

| Action | Fields |
| --- | --- |
| `room_companion.show_message` | `panel`, `message`, optional `title`, `image_url`, `timeout` |
| `room_companion.dismiss_message` | `panel` |
| `room_companion.resend_lyrics` | `panel` |

`panel` is the Room Companion entry of the panel (pick it in the action editor).

## Asset API

Every panel gets a secret base URL:

```text
http://<home assistant>:8123/api/room_companion/<token>
```

The panel cannot send Home Assistant credentials with each image request, so
the unguessable token stands in for them. It only grants these images. Newer
firmware receives the base URL through its `set_companion(url)` ESPHome
action when the panel connects; `GET <base>/info` lists what is served.

| Path | Query | Returns |
| --- | --- | --- |
| `/cover/<media_player>` | `w`, `h`, `mode=cover\|blur`, `fmt=jpeg\|png\|rgb565`, `swap=1`, `q`, `dim` (percent, blur) | The player's art cropped to `w`×`h`, or the blurred, darkened lyrics backdrop. |
| `/emoji/<stem>.png\|.bgra\|.svg` | `size` (default 48) | Noto Color Emoji. `stem` is the code points in lowercase hex joined by `_`, without `fe0f` (`1f468_200d_1f469`). `.bgra` is raw LVGL ARGB8888. |
| `/icon/<name>.svg` | `size` (default 80) | A white Material Design Icon, e.g. `/icon/weather-sunny.svg`. |
| `/image/<key>.png` | `w`, `h`, `fmt` | A picture passed to `show_message`, shrunk to fit. |
| `/glyph/<codepoint>.a8\|.png` | `size` (default 24) | One character from the configured font, white. `.a8` is 8-bit alpha; headers `X-Width`, `X-Height`, `X-Advance`, `X-Ascent`. |

Raw formats (`rgb565`, `bgra`, `a8`) carry `X-Width` and `X-Height` headers.
`rgb565` is little endian, LVGL's native order; `swap=1` gives big endian.

Covers change with the track, so they are sent with `Cache-Control:
no-cache` and an `ETag`; the rest may be cached for a day.

## Status

0.1.0 is a first release. Its tests run against Home Assistant's test
framework. The panel firmware still downloads covers, emoji and icons itself;
using the asset API needs a firmware update that is not released yet.

## License

AGPL-3.0-or-later, like ESPHome Room Companion. Noto Color Emoji are under the
SIL Open Font License 1.1 and Material Design Icons under the Apache License
2.0; both are downloaded at run time, not shipped here.
