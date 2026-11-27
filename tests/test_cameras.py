"""Live camera pages and camera sound."""

import asyncio
from http import HTTPStatus
import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.room_companion import cameras
from custom_components.room_companion.const import CONF_CAMERA_SOURCE, CONF_GO2RTC_URL

from .conftest import PREFIX
from .test_init import TOKEN, _setup

GO2RTC = "http://192.168.1.10:1984"


def test_page_places_the_player() -> None:
    live = cameras.Live("go2rtc", f"{GO2RTC}/stream.html?src=a&mode=webrtc,mse", None)
    page = cameras.page(live, {"x": "10", "y": "20.5", "w": "50", "h": "40"})
    assert "left:10%;top:20.5%;width:50%;height:40%;" in page
    assert 'src="http://192.168.1.10:1984/stream.html?src=a&amp;mode=webrtc,mse"' in page
    turned = cameras.page(live, {"x": "1", "r": "90"})
    assert "rotate(90deg)" in turned and "left:1%" not in turned
    assert "No live stream" in cameras.page(None, {"x": "javascript:1"})
    assert "javascript" not in cameras.page(None, {"x": "javascript:1"})


async def test_webrtc_camera(hass: HomeAssistant, hass_client_no_auth, panel_calls) -> None:
    links: list[ServiceCall] = []

    async def create_link(call: ServiceCall) -> None:
        links.append(call)

    hass.services.async_register("webrtc", "create_link", create_link)
    await _setup(hass)
    client = await hass_client_no_auth()
    response = await client.get(f"/api/room_companion/{TOKEN}/camera/camera.front_door?x=0&y=10&w=100&h=50")
    assert response.status == HTTPStatus.OK
    page = await response.text()
    assert len(links) == 1
    link = links[0].data
    assert link["entity"] == "camera.front_door" and link["open_limit"] == 0 and link["time_to_live"] == 0
    assert TOKEN not in link["link_id"]  # Not guessable, and not the panel's token.
    assert f"/webrtc/embed?url={link['link_id']}" in page
    assert "top:10%" in page
    # Not a camera.
    assert (await client.get(f"/api/room_companion/{TOKEN}/camera/light.x")).status == HTTPStatus.NOT_FOUND


async def test_go2rtc_by_name_and_by_source(hass: HomeAssistant, hass_client_no_auth, aioclient_mock, panel_calls) -> None:
    aioclient_mock.get(f"{GO2RTC}/api/streams", json={
        "front_door": {"producers": [{"url": "rtsp://10.0.0.5/main"}]},
        "garden_hd": {"producers": [{"url": "rtsp://10.0.0.6/live#backchannel=0"}]},
    })
    aioclient_mock.put(f"{GO2RTC}/api/streams", status=200)
    await _setup(hass, {CONF_CAMERA_SOURCE: "go2rtc", CONF_GO2RTC_URL: GO2RTC})
    client = await hass_client_no_auth()
    page = await (await client.get(f"/api/room_companion/{TOKEN}/camera/camera.front_door")).text()
    assert f"{GO2RTC}/stream.html?src=front_door" in page
    with patch("homeassistant.components.camera.async_get_stream_source",
               return_value="rtsp://10.0.0.6/live"):
        page = await (await client.get(f"/api/room_companion/{TOKEN}/camera/camera.garden")).text()
    assert "src=garden_hd" in page  # Matched by its source address.
    with patch("homeassistant.components.camera.async_get_stream_source",
               return_value="rtsp://10.0.0.7/x"):
        page = await (await client.get(f"/api/room_companion/{TOKEN}/camera/camera.shed")).text()
    assert "src=camera_shed" in page  # Added to go2rtc.
    put = [c for c in aioclient_mock.mock_calls if c[0] == "PUT"]
    assert put and put[0][1].query["name"] == "camera_shed" and put[0][1].query["src"] == "rtsp://10.0.0.7/x"
    # An explicit stream name wins.
    page = await (await client.get(f"/api/room_companion/{TOKEN}/camera/camera.front_door?stream=garden_hd")).text()
    assert "src=garden_hd" in page


async def test_play_camera_audio(hass: HomeAssistant, aioclient_mock, panel_calls) -> None:
    aioclient_mock.get(f"{GO2RTC}/api/streams", json={"front_door": {}})
    played: list[ServiceCall] = []

    async def play(call: ServiceCall) -> None:
        played.append(call)

    hass.services.async_register("media_player", "play_media", play)
    await _setup(hass, {CONF_CAMERA_SOURCE: "go2rtc", CONF_GO2RTC_URL: GO2RTC})
    await hass.services.async_call("room_companion", "play_camera_audio", {
        "camera": "camera.front_door", "media_player": "media_player.panel_speaker", "device": PREFIX,
    }, blocking=True)
    await hass.async_block_till_done()
    assert played and played[0].data["media_content_id"] == "rtsp://192.168.1.10:8554/front_door"
    assert played[0].data["entity_id"] == ["media_player.panel_speaker"] or \
        played[0].data["entity_id"] == "media_player.panel_speaker"


def _jpeg(w: int, h: int) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


async def test_snapshot_from_go2rtc_sized(hass: HomeAssistant, hass_client_no_auth, aioclient_mock, panel_calls) -> None:
    import io

    from PIL import Image

    aioclient_mock.get(f"{GO2RTC}/api/streams", json={"front_door": {}})
    aioclient_mock.get(f"{GO2RTC}/api/frame.jpeg", content=_jpeg(1920, 1080))
    await _setup(hass, {CONF_CAMERA_SOURCE: "go2rtc", CONF_GO2RTC_URL: GO2RTC})
    client = await hass_client_no_auth()
    response = await client.get(f"/api/room_companion/{TOKEN}/snapshot/camera.front_door?w=427&h=427")
    assert response.status == HTTPStatus.OK and response.content_type == "image/jpeg"
    assert Image.open(io.BytesIO(await response.read())).size == (427, 240)  # Fits, keeps 16:9.
    frames = sum(1 for c in aioclient_mock.mock_calls if "frame.jpeg" in str(c[1]))
    # A second request is answered from the kept frame.
    assert (await client.get(f"/api/room_companion/{TOKEN}/snapshot/camera.front_door?w=200&h=200")).status == HTTPStatus.OK
    assert sum(1 for c in aioclient_mock.mock_calls if "frame.jpeg" in str(c[1])) == frames
    assert (await client.get(f"/api/room_companion/{TOKEN}/snapshot/light.x")).status == HTTPStatus.NOT_FOUND
    await hass.data["room_companion"]["snapshots"].async_stop()


async def test_snapshot_from_home_assistant(hass: HomeAssistant, hass_client_no_auth, aioclient_mock, panel_calls) -> None:
    from homeassistant.components.camera import Image as CameraImage

    await _setup(hass, {CONF_CAMERA_SOURCE: "go2rtc", CONF_GO2RTC_URL: GO2RTC})  # go2rtc not reachable.
    aioclient_mock.get(f"{GO2RTC}/api/streams", status=500)
    aioclient_mock.get("http://127.0.0.1:11984/api/streams", status=500)  # Home Assistant's own, from the request's host.
    client = await hass_client_no_auth()
    with patch("homeassistant.components.camera.async_get_image",
               return_value=CameraImage("image/jpeg", _jpeg(640, 480))), \
         patch("custom_components.room_companion.cameras.LOCAL_GO2RTC", ()):
        response = await client.get(f"/api/room_companion/{TOKEN}/snapshot/camera.porch?w=320&h=320")
        assert response.status == HTTPStatus.OK
        # The MJPEG stream starts with the kept frame.
        stream = await client.get(f"/api/room_companion/{TOKEN}/stream/camera.porch?w=320&h=320")
        assert stream.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        first = await stream.content.read(64)
        assert first.startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ")
        stream.close()
    await hass.data["room_companion"]["snapshots"].async_stop()


class _FakeStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def read(self, _n: int) -> bytes:
        await asyncio.sleep(0)
        return self.chunks.pop(0) if self.chunks else b""


class _FakeProc:
    def __init__(self, chunks: list[bytes]) -> None:
        self.stdout = _FakeStdout(chunks)
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


async def test_key_frames_from_ffmpeg(hass: HomeAssistant, aioclient_mock, panel_calls) -> None:
    aioclient_mock.get(f"{GO2RTC}/api/streams", json={"front_door": {}})
    aioclient_mock.get(f"{GO2RTC}/api/frame.jpeg", status=500)
    aioclient_mock.get("http://127.0.0.1:11984/api/streams", status=500)
    await _setup(hass, {CONF_CAMERA_SOURCE: "go2rtc", CONF_GO2RTC_URL: GO2RTC})
    one, two = _jpeg(64, 36), _jpeg(32, 18)
    # Frames split across reads, with noise between them.
    stream = one[:10] + b"", one[10:] + b"junk" + two[:5], two[5:]
    calls: list[tuple] = []

    async def spawn(*args, **_kwargs):
        calls.append(args)
        return _FakeProc(list(stream))

    snapshots = hass.data["room_companion"]["snapshots"]
    with patch.object(cameras.Snapshots, "_ffmpeg", return_value="ffmpeg"), \
         patch("custom_components.room_companion.cameras.LOCAL_GO2RTC", ()), \
         patch("asyncio.create_subprocess_exec", spawn):
        snapshots._wanted["camera.front_door"] = time.monotonic()
        snapshots._sources["camera.front_door"] = ({CONF_GO2RTC_URL: GO2RTC}, "http://ha.local:8123")
        assert await snapshots._read("camera.front_door")
    assert calls and "rtsp://192.168.1.10:8554/front_door" in calls[0]
    assert "-skip_frame" in calls[0]
    assert snapshots.latest("camera.front_door")[1] == two
    await snapshots.async_stop()
