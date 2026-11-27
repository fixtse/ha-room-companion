"""Constants for Room Companion."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "room_companion"
ESPHOME_DOMAIN: Final = "esphome"

# Config entry data.
CONF_PREFIX: Final = "prefix"  # The panel's ESPHome action prefix (its node name).
CONF_TOKEN: Final = "token"  # Secret path segment of the panel's local asset URLs.

# Options.
CONF_MEDIA_PLAYERS: Final = "media_players"
CONF_LYRICS: Final = "lyrics"
CONF_TIMEOUT: Final = "timeout"
CONF_KDE: Final = "kde_notifications"
CONF_KDE_DEVICES: Final = "kde_devices"
CONF_BASE_URL: Final = "base_url"
CONF_GLYPH_FONT: Final = "glyph_font"

DEFAULT_TIMEOUT: Final = 30

# ESPHome actions the Room Companion firmware provides, after "esphome.<prefix>_".
ACTION_SHOW_MESSAGE: Final = "show_message"
ACTION_DISMISS_MESSAGE: Final = "dismiss_message"
ACTION_SET_LYRICS: Final = "set_lyrics"
# Optional, newer firmware: receives the base URL of the panel's local assets.
ACTION_SET_COMPANION: Final = "set_companion"

URL_BASE: Final = "/api/room_companion"
API_VERSION: Final = 1

LRCLIB_URL: Final = "https://lrclib.net/api/get"
USER_AGENT: Final = (
    "Room Companion for Home Assistant (https://github.com/fixtse/ha-room-companion)"
)

KDE_EVENT: Final = "kdeforhomeassistant_notification"
KDE_DOMAIN: Final = "kdeforhomeassistant"

# Remote sources, used only on a cache miss.
EMOJI_PNG_URL: Final = (
    "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/128/emoji_u{stem}.png"
)
EMOJI_SVG_URL: Final = "https://fonts.gstatic.com/s/e/notoemoji/latest/{stem}/emoji.svg"
MDI_SVG_URL: Final = (
    "https://api.iconify.design/mdi/{name}.svg?color=%23ffffff&width={size}&height={size}"
)
MDI_SVG_BACKUP_URL: Final = (
    "https://raw.githubusercontent.com/Templarian/MaterialDesign/master/svg/{name}.svg"
)
