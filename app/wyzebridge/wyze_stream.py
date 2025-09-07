import contextlib
import json
import multiprocessing as mp
import traceback
import zoneinfo
from collections import namedtuple
from ctypes import c_int
from datetime import datetime
from enum import IntEnum
from queue import Empty, Full
from subprocess import PIPE, Popen
from threading import Thread
from time import sleep, time
from typing import Optional

from wyzecam.iotc import WyzeIOTC, WyzeIOTCSession
from wyzecam.tutk.tutk import TutkError
from wyzecam.api_models import WyzeAccount, WyzeCamera
from wyzebridge.wyze_stream_options import WyzeStreamOptions
from wyzebridge.stream import Stream
from wyzebridge.bridge_utils import env_bool, env_cam
from wyzebridge.config import COOLDOWN, DISABLE_CONTROL, MQTT_TOPIC
from wyzebridge.ffmpeg import get_ffmpeg_cmd
from wyzebridge.logging import logger, isDebugEnabled
from wyzebridge.mqtt import publish_discovery, publish_messages, update_mqtt_state
from wyzebridge.webhooks import send_webhook
from wyzebridge.wyze_api import WyzeApi
from wyzebridge.wyze_commands import GET_CMDS, PARAMS, SET_CMDS
from wyzebridge.wyze_control import camera_control

NET_MODE = {0: "P2P", 1: "RELAY", 2: "LAN"}

StreamTuple = namedtuple("stream", ["user", "camera", "options"])
QueueTuple = namedtuple("queue", ["cam_resp", "cam_cmd"])

class StreamStatus(IntEnum):
    OFFLINE = -90
    INITIALIZING = -2
    STOPPING = -1
    DISABLED = 0
    STOPPED = 1
    CONNECTING = 2
    CONNECTED = 3

class WyzeStream(Stream):
    __slots__ = (
        "api",
        "cam_cmd",
        "cam_resp",
        "camera",
        "motion_ts",
        "options",
        "rtsp_fw_enabled",
        "start_time",
        "tutk_stream_process",
        "uri",
        "user",
        "_motion",
        "_state",
    )

    def __init__(self, user: WyzeAccount, api: WyzeApi, camera: WyzeCamera, options: WyzeStreamOptions) -> None:
        self.api: WyzeApi = api
        self.cam_cmd: mp.Queue
        self.cam_resp: mp.Queue
        self.camera: WyzeCamera = camera
        self.motion_ts: float = 0
        self.options: WyzeStreamOptions = options
        self.rtsp_fw_enabled: bool = False
        self.start_time: float = 0
        self.tutk_stream_process: Optional[mp.Process] = None
        self.uri: str = camera.name_uri + ("-sub" if options.substream else "")
        self.user: WyzeAccount = user
        self._motion: bool = False
        self._state: c_int = mp.Value("i", StreamStatus.STOPPED, lock=False)
        
        self.setup()

    def setup(self):
        if self.camera.ip is None or self.camera.ip == "":
            logger.warning(
                f"⚠︎ [{self.camera.product_model}] {self.camera.nickname} has no IP"
            )
            self.state = StreamStatus.OFFLINE
            return

        if self.camera.is_gwell or self.camera.product_model == "LD_CFP":
            logger.info(
                f"⚠︎ [{self.camera.product_model}] {self.camera.nickname} may not be supported"
            )
            self.state = StreamStatus.DISABLED

        if self.options.substream and not self.camera.can_substream:
            logger.error(f"❗ {self.camera.nickname} may not support multiple streams!")
            self.state = StreamStatus.DISABLED

        # 5 = 4K, 4 = floodlight special, 3 = 2K/QHD, 0 = 1080p
        hq_size = (
            5 if getattr(self.camera, "is_4k", False)
            else 4 if self.camera.is_floodlight
            else 3 if self.camera.is_2k
            else 0
        )

        # Override default quality of hd180 for 4K cams
        if getattr(self.camera, "is_4k", False) and not self.options.quality:
            self.options.quality = "hd350"

        self.options.update_quality(hq_size)


        publish_discovery(self.uri, self.camera)

    @property
    def state(self) -> int:
        return self._state.value

    @state.setter
    def state(self, value) -> None:
        value = value.value if isinstance(value, StreamStatus) else value
        if self._state.value != value:
            self._state.value = value
            update_mqtt_state(self.uri, self.status())

    @property
    def motion(self) -> bool:
        state = time() - self.motion_ts < 20
        if self._motion and not state:
            self._motion = state
            publish_messages([(f"{MQTT_TOPIC}/{self.uri}/motion", 2, 0, True)])
        return state

    @motion.setter
    def motion(self, value: float):
        self._motion = True
        self.motion_ts = value
        publish_messages(
            [
                (f"{MQTT_TOPIC}/{self.uri}/motion", 1, 0, True),
                (f"{MQTT_TOPIC}/{self.uri}/motion_ts", value, 0, True),
            ]
        )

    @property
    def connected(self) -> bool:
        return self.state == StreamStatus.CONNECTED

    @property
    def enabled(self) -> bool:
        return self.state != StreamStatus.DISABLED

    def init(self) -> bool:
        self.state = StreamStatus.INITIALIZING
        logger.info(
            f"🪄 MediaMTX Initializing WyzeCam {self.camera.model_name} - {self.camera.nickname} on {self.camera.ip}"
        )
        self.state = StreamStatus.STOPPED
        return True

    def start(self) -> bool:
        if self.health_check(False) != StreamStatus.STOPPED:
            return False
        self.state = StreamStatus.CONNECTING
        logger.info(
            f"🎉 Connecting to WyzeCam {self.camera.model_name} - {self.camera.nickname} on {self.camera.ip}"
        )
        self.start_time = time()
        self.cam_resp = mp.Queue(1)
        self.cam_cmd = mp.Queue(1)
        self.tutk_stream_process = mp.Process(
            target=start_tutk_stream,
            args=(
                self.uri,
                StreamTuple(self.user, self.camera, self.options),
                QueueTuple(self.cam_resp, self.cam_cmd),
                self._state,
            ),
            name=self.uri,
        )
        self.tutk_stream_process.start()
        return True

    def stop(self) -> bool:
        self._clear_mp_queue()
        self.start_time = 0
        self.state = StreamStatus.STOPPING
        if self.tutk_stream_process and self.tutk_stream_process.is_alive():
            with contextlib.suppress(ValueError, AttributeError, RuntimeError):
                if self.tutk_stream_process.is_alive():
                    self.tutk_stream_process.terminate()
                    self.tutk_stream_process.join(5)

        self.tutk_stream_process = None
        self.state = StreamStatus.STOPPED
        return True

    def enable(self) -> bool:
        if self.state == StreamStatus.DISABLED:
            logger.info(f"🔓 Enabling {self.uri}")
            self.state = StreamStatus.STOPPED

        return self.state > StreamStatus.DISABLED

    def disable(self) -> bool:
        if self.state != StreamStatus.DISABLED:
            logger.info(f"🔒 Disabling {self.uri}")
            if self.state != StreamStatus.STOPPED:
                self.stop()

            self.state = StreamStatus.DISABLED
        return True

    def health_check(self, should_start: bool = True) -> int:
        if self.state == StreamStatus.OFFLINE:
            if env_bool("IGNORE_OFFLINE"):
                logger.info(f"🪦 {self.uri} is offline. WILL ignore.")
                self.disable()
                return self.state
            logger.info(f"👻 {self.camera.nickname} is offline.")
        if self.state in {-13, -19, -68}:  # IOTC_ER_TIMEOUT, IOTC_ER_CAN_NOT_FIND_DEVICE, IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY
            self.refresh_camera()
        elif self.state < StreamStatus.DISABLED:
            state = self.state
            self.stop()
            if state < StreamStatus.STOPPING:
                self.start_time = time() + COOLDOWN
                logger.info(f"🌬️ {self.camera.nickname} will cooldown for {COOLDOWN}s.")
        elif (
            self.state == StreamStatus.STOPPED
            and self.options.reconnect
            and should_start
        ):
            self.start()
        elif self.state == StreamStatus.CONNECTING and is_timedout(self.start_time, 20):
            logger.warning(f"⏰ Timed out connecting to {self.camera.nickname}.")
            self.stop()

        if should_start and self.camera.is_battery and self.state == StreamStatus.STOPPED:
            return StreamStatus.DISABLED

        return self.state if self.start_time < time() else StreamStatus.DISABLED

    def refresh_camera(self):
        self.stop()
        if not (cam := self.api.get_camera(self.camera.name_uri)):
            return False
        self.camera = cam
        return True

    def status(self) -> str:
        try:
            return StreamStatus(self._state.value).name.lower()
        except ValueError:
            return "error"

    def get_info(self, item: Optional[str] = None) -> dict:
        if item == "boa_info":
            return self.boa_info()
        data = {
            "name_uri": self.uri,
            "status": self.state,
            "connected": self.connected,
            "enabled": self.enabled,
            "motion": self.motion,
            "motion_ts": self.motion_ts,
            "on_demand": not self.options.reconnect,
            "audio": self.options.audio,
            "record": self.options.record,
            "substream": self.options.substream,
            "model_name": self.camera.model_name,
            "is_2k": self.camera.is_2k,
            "rtsp_fw": self.camera.rtsp_fw,
            "rtsp_fw_enabled": self.rtsp_fw_enabled,
            "is_battery": self.camera.is_battery,
            "webrtc": self.camera.webrtc_support,
            "start_time": self.start_time,
            "req_frame_size": self.options.frame_size,
            "req_bitrate": self.options.bitrate,
        }
        if self.connected and not self.camera.camera_info:
            self.update_cam_info()
        if self.camera.camera_info and "boa_info" in self.camera.camera_info:
            data["boa_url"] = f"http://{self.camera.ip}/cgi-bin/hello.cgi?name=/"
        return data | self.camera.model_dump(exclude={"p2p_id", "enr", "parent_enr"})

    def update_cam_info(self) -> None:
        if not self.connected:
            return

        if (resp := self.send_cmd("caminfo")) and ("response" not in resp):
            self.camera.set_camera_info(resp)

    def boa_info(self) -> dict:
        self.update_cam_info()
        if not self.camera.camera_info:
            return {}
        return self.camera.camera_info.get("boa_info", {})

    def state_control(self, payload) -> dict:
        if payload in {"start", "stop", "disable", "enable"}:
            logger.info(f"[CONTROL] SET {self.uri} state={payload}")
            response = getattr(self, payload)()
            return {
                "status": "success" if response else "error",
                "response": payload if response else self.status(),
                "value": payload,
            }
        logger.info(f"[CONTROL] GET {self.uri} state")
        return {"status": "success", "response": self.status()}

    def power_control(self, payload: str) -> dict:
        if payload not in {"on", "off", "restart"}:
            resp = self.api.get_device_info(self.camera, "P3")
            resp["value"] = "on" if resp["value"] == "1" else "off"
            return resp
        run_cmd = payload if payload == "restart" else f"power_{payload}"

        return dict(
            self.api.run_action(self.camera, run_cmd),
            value="on" if payload == "restart" else payload,
        )

    def notification_control(self, payload: str) -> dict:
        if payload not in {"on", "off", "1", "2", "true", "false"}:
            return self.api.get_device_info(self.camera, "P1")

        pvalue = "1" if payload in {"on", "1", "true"} else "2"
        resp = self.api.set_property(self.camera, "P1", pvalue)
        value = None if resp.get("status") == "error" else pvalue

        return dict(resp, value=value)

    def tz_control(self, payload: str) -> dict:
        try:
            zone = zoneinfo.ZoneInfo(payload)
            offset = datetime.now(zone).utcoffset()
            assert offset is not None
        except (zoneinfo.ZoneInfoNotFoundError, AssertionError):
            return {"response": "invalid time zone"}

        return dict(
            self.api.set_device_info(self.camera, {"device_timezone_city": zone.key}),
            value=int(offset.total_seconds() / 3600),
        )

    def send_cmd(self, cmd: str, payload: str | list | dict = "") -> dict:
        if cmd in {"state", "start", "stop", "disable", "enable"}:
            return self.state_control(payload or cmd)

        if cmd == "device_info":
            return self.api.get_device_info(self.camera)
        if cmd == "device_setting":
            return self.api.get_device_info(self.camera, cmd="device_setting")

        if cmd == "battery":
            return self.api.get_device_info(self.camera, "P8")

        if cmd == "power":
            return self.power_control(str(payload).lower())

        if cmd == "notifications":
            return self.notification_control(str(payload).lower())

        if cmd in {"motion", "motion_ts"}:
            return {
                "status": "success",
                "response": {"motion": self.motion, "motion_ts": self.motion_ts},
                "value": self.motion if cmd == "motion" else self.motion_ts,
            }

        if self.state < StreamStatus.STOPPED:
            return {"response": self.status()}

        if DISABLE_CONTROL:
            return {"response": "control disabled"}

        if cmd == "time_zone" and payload and isinstance(payload, str):
            return self.tz_control(payload)

        if cmd == "bitrate" and isinstance(payload, (str, int)) and payload.isdigit():
            self.options.bitrate = int(payload)

        if cmd == "update_snapshot":
            return {"update_snapshot": True}

        if cmd == "cruise_point" and payload == "-":
            return {"status": "success", "value": "-"}

        if cmd not in GET_CMDS | SET_CMDS | PARAMS and cmd not in {"caminfo"}:
            return {"response": "invalid command"}

        if on_demand := not self.connected:
            logger.info(f"🖇 [CONTROL] Connecting to {self.uri}")
            self.start()
            while not self.connected and time() - self.start_time < 10:
                sleep(0.1)
        self._clear_mp_queue()
        try:
            self.cam_cmd.put_nowait((cmd, payload))
            cam_resp = self.cam_resp.get(timeout=10)
        except Full:
            return {"response": "camera busy"}
        except Empty:
            return {"response": "timed out"}
        finally:
            if on_demand:
                logger.info(f"⛓️‍💥 [CONTROL] Disconnecting from {self.uri}")
                self.stop()

        return cam_resp.pop(cmd, None) or {"response": "could not get result"}

    def check_rtsp_fw(self, force: bool = False) -> Optional[str]:
        """Check and add rtsp."""
        if not self.camera.rtsp_fw:
            return
        logger.info(f"🛃 Checking {self.camera.nickname} for firmware RTSP")
        try:
            with WyzeIOTC() as iotc, WyzeIOTCSession(
                iotc.tutk_platform_lib, self.user, self.camera
            ) as session:
                if session.session_check().mode != 2:  # 0: P2P mode, 1: Relay mode, 2: LAN mode
                    logger.warning(
                        f"⚠️ [{self.camera.nickname}] Camera is not on same LAN"
                    )
                    return
                return session.check_native_rtsp(start_rtsp=force)
        except TutkError:
            return

    def _clear_mp_queue(self):
        with contextlib.suppress(Empty, AttributeError):
            self.cam_cmd.get_nowait()
        with contextlib.suppress(Empty, AttributeError):
            self.cam_resp.get_nowait()


def start_tutk_stream(uri: str, stream: StreamTuple, queue: QueueTuple, state: c_int):
    """Connect and communicate with the camera using TUTK."""
    was_offline = state.value == StreamStatus.OFFLINE
    state.value = StreamStatus.CONNECTING
    exit_code = StreamStatus.STOPPING
    control_thread = audio_thread = None
    try:
        with WyzeIOTC() as iotc, iotc.session(stream, state) as sess:
            assert state.value >= StreamStatus.CONNECTING, "Stream Stopped"
            v_codec, audio = get_cam_params(sess, uri)
            control_thread = setup_control(sess, queue) if not stream.options.substream else None
            audio_thread = setup_audio(sess, uri) if sess.enable_audio else None

            ffmpeg_cmd = get_ffmpeg_cmd(uri, v_codec, audio, stream.camera.is_vertical)
            assert state.value >= StreamStatus.CONNECTING, "Stream Stopped"
            state.value = StreamStatus.CONNECTED
            with Popen(ffmpeg_cmd, stdin=PIPE, stderr=None) as ffmpeg:
                if ffmpeg.stdin is not None:
                    for frame, _ in sess.recv_bridge_data():
                        ffmpeg.stdin.write(frame)

    except TutkError as ex:
        trace = traceback.format_exc() if isDebugEnabled(logger) else ""
        logger.warning(f"𓁈‼️ [TUTK] {[ex.code]} {ex} {trace}")
        set_cam_offline(uri, ex, was_offline)
        if ex.code in {-10, -13, -19, -68, -90}: # IOTC_ER_UNLICENSE, IOTC_ER_TIMEOUT, IOTC_ER_CAN_NOT_FIND_DEVICE, IOTC_ER_DEVICE_REJECT_BY_WRONG_AUTH_KEY, IOTC_ER_DEVICE_OFFLINE
            exit_code = ex.code
    except ValueError as ex:
        trace = traceback.format_exc() if isDebugEnabled(logger) else ""
        logger.warning(f"𓁈⚠️ [TUTK] Error: [{type(ex).__name__}] {ex} {trace}")
        if ex.args[0] == "ENR_AUTH_FAILED":
            logger.warning("⏰ Expired ENR?")
            exit_code = -19 # IOTC_ER_CAN_NOT_FIND_DEVICE
    except BrokenPipeError:
        logger.warning("𓁈✋ [TUTK] FFMPEG stopped")
    except Exception as ex:
        trace = traceback.format_exc() if isDebugEnabled(logger) else ""
        logger.error(f"𓁈‼️ [TUTK] Exception: [{type(ex).__name__}] {ex} {trace}")
    else:
        logger.warning("𓁈🛑 [TUTK] Stream stopped")
    finally:
        state.value = exit_code

        if audio_thread is not None:
            stop_and_wait(audio_thread)
            audio_thread = None

        if control_thread is not None:
            stop_and_wait(control_thread)
            control_thread = None

def stop_and_wait(thread: Thread):
    with contextlib.suppress(ValueError, AttributeError, RuntimeError):
        if thread and thread.is_alive():
            thread.join(timeout=5)

def setup_audio(sess: WyzeIOTCSession, uri: str) -> Thread:
    audio_thread = Thread(target=sess.recv_audio_pipe, name=f"{uri}_audio")
    audio_thread.start()
    return audio_thread

def setup_control(sess: WyzeIOTCSession, queue: QueueTuple) -> Thread:
    control_thread = Thread(
        target=camera_control,
        args=(sess, queue.cam_resp, queue.cam_cmd),
        name=f"{sess.camera.name_uri}_control",
    )
    control_thread.start()
    return control_thread

def get_cam_params(sess: WyzeIOTCSession, uri: str) -> tuple[str, dict]:
    """Check session and return fps and audio codec from camera."""
    session_info = sess.session_check()
    net_mode = check_net_mode(session_info.mode, uri)
    v_codec, fps = get_video_params(sess)
    firmware, wifi = get_camera_info(sess)
    stream = (
        f"{sess.preferred_bitrate}kb/s {sess.resolution} stream ({v_codec}/{fps}fps)"
    )

    logger.info(f"📡 Getting {stream} via {net_mode} (WiFi: {wifi}%) FW: {firmware}")

    audio = get_audio_params(sess)
    mqtt = [
        (f"{MQTT_TOPIC}/{uri.lower()}/net_mode", net_mode),
        (f"{MQTT_TOPIC}/{uri.lower()}/wifi", wifi),
        (f"{MQTT_TOPIC}/{uri.lower()}/audio", json.dumps(audio) if audio else False),
        (f"{MQTT_TOPIC}/{uri.lower()}/ip", sess.camera.ip, 0, True),
    ]
    publish_messages(mqtt)
    return v_codec, audio

def get_camera_info(sess: WyzeIOTCSession) -> tuple[str, str]:
    if not (camera_info := sess.camera.camera_info):
        logger.warning("⚠️ cameraInfo is missing.")
        return "NA", "NA"
    logger.debug(f"[cameraInfo] {camera_info}")

    firmware = camera_info.get("basicInfo", {}).get("firmware", "NA")
    if sess.camera.dtls or sess.camera.parent_dtls:
        firmware += " 🔒"

    wifi = camera_info.get("basicInfo", {}).get("wifidb", "NA")
    if "netInfo" in camera_info:
        wifi = camera_info["netInfo"].get("signal", wifi)

    return firmware, wifi

def get_video_params(sess: WyzeIOTCSession) -> tuple[str, int]:
    cam_info = sess.camera.camera_info
    if not cam_info or not (video_param := cam_info.get("videoParm")):
        logger.warning("⚠️ camera_info is missing videoParm. Using default values.")
        video_param = {"type": "h264", "fps": 20}

    fps = int(video_param.get("fps", 0))

    if force_fps := int(env_cam("FORCE_FPS", sess.camera.name_uri, "0")):
        logger.info(f"🦾 Attempting to force fps={force_fps}")
        sess.update_frame_size_rate(fps=force_fps)
        fps = force_fps

    if fps % 5 != 0:
        logger.error(f"⚠️ Unusual FPS detected: {fps}")

    logger.debug(f"📽️ [videoParm] {video_param}")
    sess.preferred_frame_rate = fps

    return video_param.get("type", "h264"), fps

def get_audio_params(sess: WyzeIOTCSession) -> dict[str, str | int]:
    if not sess.enable_audio:
        return {}

    codec, rate = sess.identify_audio_codec()
    logger.info(f"🔊 Audio Enabled [Source={codec.upper()}/{rate:,}Hz]")

    if codec_out := env_bool("AUDIO_CODEC"):
        logger.info(f"🔊 [AUDIO] Re-Encode Enabled [AUDIO_CODEC={codec_out}]")
    elif rate > 8000 or codec.lower() == "s16le":
        codec_out = "pcm_mulaw"
        logger.info(f"🔊 [AUDIO] Re-Encode for RTSP compatibility [{codec_out=}]")

    return {"codec": codec, "rate": rate, "codec_out": codec_out.lower()}

def check_net_mode(session_mode: int, uri: str) -> str:
    """Check if the connection mode is allowed."""
    net_mode = env_cam("NET_MODE", uri, "any")
    
    if "p2p" in net_mode and session_mode == 1:
        raise RuntimeError("☁️ Connected via RELAY MODE! Reconnecting")
    
    if "lan" in net_mode and session_mode != 2:
        raise RuntimeError("☁️ Connected via NON-LAN MODE! Reconnecting")

    mode = f'{NET_MODE.get(session_mode, f"UNKNOWN ({session_mode})")} mode'
    if session_mode != 2:
        logger.warning(f"☁️ Camera is connected via {mode}!!")
        logger.warning("Stream may consume additional bandwidth!")
    return mode

def set_cam_offline(uri: str, error: TutkError, was_offline: bool) -> None:
    """Do something when camera goes offline."""
    state = "offline" if error.code == -90 else error.name # IOTC_ER_DEVICE_OFFLINE
    update_mqtt_state(uri.lower(), str(state))

    if str(error.code) not in env_bool("OFFLINE_ERRNO", "-90"):
        return
    if was_offline:  # Don't resend if previous state was offline.
        return

    send_webhook("offline", uri, f"{uri} is offline")

def is_timedout(start_time: float, timeout: int = 20) -> bool:
    return time() - start_time > timeout if start_time else False
