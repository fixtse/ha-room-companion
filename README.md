# Room Companion for Home Assistant

<img src="icon.png" alt="" width="96" align="right">

The Home Assistant side of [ESPHome Room Companion](https://github.com/fixtse/ESPHome-Room-Companion)
panels (ONX3248G035 3.5" and ONX2432G028 2.8") and the
[OpenNextion KNOB](https://github.com/fixtse/OpenNextion-KNOB-ESPHome)
(ONX2424G013). It takes over work the devices and separate blueprints do
today:

- **Synced lyrics.** When a chosen media player starts a track, Home Assistant
  asks [LRCLIB](https://lrclib.net) once and sends the timed lyrics to the
  panel. This replaces the *Room Companion synced lyrics* blueprint and its
  `rest_command`. Results are cached, so several panels cost one lookup. A
  panel that opens a player mid-song asks for its lyrics, and a **Lyrics
  offset** number entity shifts them live (panels only; the knob has no
  lyrics).
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
- **Live cameras.** For panels whose camera source is *Room Companion*, Home
  Assistant finds each camera's live stream (the WebRTC Camera integration, or
  go2rtc: yours, Frigate's or Home Assistant's) and serves the panel's stream
  server a page that plays it in the camera frame. Camera sound goes through
  `room_companion.play_camera_audio`. See [Live cameras](#live-cameras).
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
   - **Live camera source**: *Automatic* (WebRTC Camera when installed, else
     go2rtc), *WebRTC Camera integration* or *go2rtc*.
   - **go2rtc address** (optional), e.g. `http://192.168.1.10:1984`.
   - **Font for missing characters** (optional): characters the device's
     fonts lack are drawn with the bundled Noto Sans (Latin, Greek, Cyrillic),
     Noto Sans SC (Chinese, Japanese kana) and Noto Sans KR (Korean). Enter the path of a
     TTF/OTF file on the Home Assistant host to prefer another font; the
     bundled ones still cover what it lacks.

If you used the blueprints before, delete those automations (and the
`room_companion_lrclib` `rest_command` and the Text helper) so messages and
lyrics are not sent twice.

## Firmware

Lyrics and notifications work with any Room Companion firmware. Local images,
the mid-song lyrics request and the lyrics offset need panel firmware
2026.11.1.25 or later, or knob firmware 2026.11.1.4 or later (missing
characters: 2026.11.1.26 and 2026.11.1.5). When the device
connects, the integration calls its `set_companion` action with its asset URL;
the device keeps it and falls back to the internet sources for five minutes
whenever Home Assistant does not answer.

With that firmware, characters the device's fonts lack (for example Chinese,
Japanese, Korean, Greek or Cyrillic song titles) are drawn from `/glyph`.

## Live cameras

Panel firmware 2026.11.1.29 or later. On the panel's settings page, **Streamed
page** tab, set the Remote WebView server and choose **Camera source → Room
Companion integration**. When a camera screen opens, the server's browser opens
`<base>/camera/<camera entity>` (with the frame's position) from Home
Assistant, which:

1. With the [WebRTC Camera](https://github.com/AlexxIT/WebRTC) integration
   installed (and the source *Automatic* or *WebRTC Camera*), registers a link
   for the camera (`webrtc.create_link`, no time or open limit, an
   unguessable id) and frames the integration's `/webrtc/embed` page. Both
   pages come from Home Assistant, so the embed page may be framed, and the
   panel no longer needs to call that action itself.
2. Otherwise looks for go2rtc: the address in the options, then Frigate's
   (port 1984 on the Frigate host), then Home Assistant's own go2rtc when it
   listens on the network (port 11984, `debug_ui`). The stream is found by
   name (the entity's object id, Frigate's camera name, the camera's name) or
   by its source address; a camera go2rtc does not have yet is added from Home
   Assistant's stream source (`PUT /api/streams`).

The panel's **Other stream names** still apply (sent as `stream=`). Camera
sound: the panel calls `room_companion.play_camera_audio`, which plays go2rtc's
RTSP output of the camera (port 8554) on the panel's speaker.

## Actions

| Action | Fields |
| --- | --- |
| `room_companion.show_message` | `panel`, `message`, optional `title`, `image_url`, `timeout` |
| `room_companion.dismiss_message` | `panel` |
| `room_companion.resend_lyrics` | `panel` |
| `room_companion.play_camera_audio` | `camera`, `media_player`, optional `device` (the panel's ESPHome name, for its options), `stream` |

`panel` is the Room Companion entry of the panel (pick it in the action editor).

## Asset API

Every panel gets a secret base URL:

```text
http://<home assistant>:8123/api/room_companion/<token>
```

The panel cannot send Home Assistant credentials with each image request, so
the unguessable token stands in for them. It only grants these images and the camera pages. Newer
firmware receives the base URL through its `set_companion(url)` ESPHome
action when the panel connects; `GET <base>/info` lists what is served.

| Path | Query | Returns |
| --- | --- | --- |
| `/cover/<media_player>` | `w`, `h`, `mode=cover\|fit\|blur`, `fmt=jpeg\|png\|rgb565`, `swap=1`, `q`, `dim` (percent, blur) | The player's art: `cover` crops it to fill `w`×`h` (the knob), `fit` scales it inside `w`×`h` keeping its shape (the panels), `blur` is the blurred, darkened lyrics backdrop. |
| `/emoji/<stem>.png\|.bgra\|.svg` | `size` (default 48) | Noto Color Emoji. `stem` is the code points in lowercase hex joined by `_`, without `fe0f` (`1f468_200d_1f469`). `.bgra` is raw LVGL ARGB8888. |
| `/icon/<name>.svg` | `size` (default 80) | A white Material Design Icon, e.g. `/icon/weather-sunny.svg`. |
| `/image/<key>.png` | `w`, `h`, `fmt` | A picture passed to `show_message`, shrunk to fit. |
| `/camera/<camera>` | `x`, `y`, `w`, `h` (percent of the screen), `r` (degrees, full screen), `stream` | An HTML page playing the camera's live stream in that rectangle, for the panel's Remote WebView server. |
| `/glyph/<codepoint>.a8\|.png` | `size` (default 24) | One character the device's fonts lack, white, from the optional chosen font or the bundled Noto Sans, Noto Sans SC or Noto Sans KR. `.a8` is 8-bit alpha with headers `X-Width`, `X-Height`, `X-Advance`, `X-Ascent`; `.a8h` puts those four as little-endian uint16 before the alpha rows (what the firmware uses). |

Raw formats (`rgb565`, `bgra`, `a8`) carry `X-Width` and `X-Height` headers.
`rgb565` is little endian, LVGL's native order; `swap=1` gives big endian.

Covers change with the track, so they are sent with `Cache-Control:
no-cache` and an `ETag`; the rest may be cached for a day.

## Status

Tests run against Home Assistant's test framework. Hardware use with the
firmware above has not been verified yet.

## License

AGPL-3.0-or-later, like ESPHome Room Companion. The bundled fonts in
`custom_components/room_companion/fonts/` (Noto Sans, Noto Sans SC and Noto Sans KR) are
under the SIL Open Font License 1.1; their licenses are in that folder. Noto Color Emoji are under the
SIL Open Font License 1.1 and Material Design Icons under the Apache License
2.0; both are downloaded at run time, not shipped here.
