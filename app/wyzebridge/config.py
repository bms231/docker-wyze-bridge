import os, json
from typing import Dict
from os import environ, getenv

from wyzebridge.build_config import BUILD_STR
from wyzebridge.bridge_utils import env_bool, split_int_str
from wyzebridge.hass import setup_hass

HASS_TOKEN: str = getenv("SUPERVISOR_TOKEN", "")

setup_hass(HASS_TOKEN)

MQTT: bool = bool(env_bool("MQTT", style="bool"))
MQTT_HOST: str = env_bool("MQTT_HOST", "", style="original")
MQTT_DISCOVERY: str = env_bool("MQTT_DTOPIC")
MQTT_TOPIC: str = env_bool("MQTT_TOPIC", "wyzebridge").strip("/")

MQTT_ENABLED = bool(env_bool("MQTT_HOST"))
MQTT_USER, _, MQTT_PASS = getenv("MQTT_AUTH", ":").partition(":")
MQTT_HOST, _, MQTT_PORT = getenv("MQTT_HOST", ":").partition(":")
MQTT_RETRIES: int = int(getenv("MQTT_RETRIES", "3"))

ON_DEMAND: bool = bool(env_bool("on_demand") if getenv("ON_DEMAND") else True)
CONNECT_TIMEOUT: int = env_bool("CONNECT_TIMEOUT", "20", style="int")

# TODO: change TOKEN_PATH  to /config for all:
TOKEN_PATH: str = "/config/" if HASS_TOKEN else "/tokens/"
IP_OVERRIDES_FILE = os.path.join(TOKEN_PATH, "ip_overrides.json")
IMG_PATH: str = f'/{env_bool("IMG_DIR", r"/media/wyze/img").strip("/")}/'

LATITUDE: float = float(getenv("LATITUDE", "0"))
LONGITUDE: float = float(getenv("LONGITUDE", "0"))
SNAPSHOT_CAMERAS: list[str] = [cam.strip() for cam in getenv("SNAPSHOT_CAMERAS", "").split(",") if cam.strip()]
SNAPSHOT_TYPE, SNAPSHOT_INT = split_int_str(env_bool("SNAPSHOT"), min=15, default=180)
SNAPSHOT_FORMAT: str = env_bool("SNAPSHOT_FORMAT", style="original").strip("/")
IMG_TYPE: str = env_bool("IMG_TYPE", "jpg", style="original")

BRIDGE_IP: str = env_bool("WB_IP")
HLS_URL: str = env_bool("WB_HLS_URL").strip("/")
RTMP_URL: str = env_bool("WB_RTMP_URL").strip("/")
RTSP_URL: str = env_bool("WB_RTSP_URL").strip("/")
WEBRTC_URL: str = env_bool("WB_WEBRTC_URL").strip("/")
LLHLS: bool = env_bool("LLHLS", style="bool")
SUBJECT_ALT_NAME: str = env_bool("SUBJECT_ALT_NAME", style="original")
COOLDOWN: int = env_bool("OFFLINE_TIME", "10", style="int")
DISABLE_CONTROL: bool = env_bool("DISABLE_CONTROL", style="bool")

MOTION: bool = env_bool("MOTION_API", style="bool")
MOTION_INT: int = max(env_bool("MOTION_INT", "1.5", style="float"), 1.1)
MOTION_START: bool = env_bool("MOTION_START", style="bool")

WB_AUTH: bool = bool(env_bool("WB_AUTH") if getenv("WB_AUTH") else True)
STREAM_AUTH: str = env_bool("STREAM_AUTH", style="original")

RECORD_PATH: str = env_bool("RECORD_PATH", r"/media/wyze/recordings/{cam_name}/%Y/%m/%d", style="original").strip("/")
RECORD_FILE: str = env_bool("RECORD_FILE_NAME", r"%Y-%m-%d-%H-%M-%S", style="original").strip("/")
RECORD_LENGTH: str = env_bool("RECORD_LENGTH", "60s")
RECORD_KEEP: str = env_bool("RECORD_KEEP", "0s")
RECORD_PATTERN: str = f"/{RECORD_PATH}/{RECORD_FILE}".removesuffix(".mp4").removesuffix(".fmp4").removesuffix(".ts")

URI_MAC: bool = bool(env_bool("URI_SEPARATOR", style="bool"))
URI_SEPARATOR: str = env_bool("URI_SEPARATOR", "-", style="original")

MTX_READTIMEOUT: str = env_bool("MTX_READTIMEOUT", "30s", style="original")
MTX_HLSVARIANT: str = env_bool("MTX_HLSVARIANT", "mpegts", style="original")
MTX_WRITEQUEUESIZE: int = env_bool("MTX_WRITEQUEUESIZE", "4096", style="int")

STUN_SERVER: str = env_bool("STUN_SERVER", "", style="original")

FORCE_IOTC_DETAIL: bool = bool(env_bool("FORCE_IOTC_DETAIL", style="bool") or False)

SDK_KEY: str = env_bool("SDK_KEY", style="original")
FRESH_DATA: bool = env_bool("FRESH_DATA", style="bool")

BOA_ENABLED: bool = env_bool("BOA_ENABLED", style="bool")
BOA_INTERVAL: int = env_bool("BOA_INTERVAL", "20", style="int")
BOA_TAKE_PHOTO: bool = env_bool("BOA_TAKE_PHOTO", style="bool")
BOA_PHOTO: bool = env_bool("BOA_PHOTO", style="bool")
BOA_ALARM: bool = env_bool("BOA_ALARM", style="bool")
BOA_MOTION: str = env_bool("BOA_MOTION", style="original")
BOA_COOLDOWN: int = env_bool("BOA_COOLDOWN", "20", style="int")

DEPRECATED = {"DEBUG_FFMPEG", "OFFLINE_IFTTT", "TOTP_KEY", "MFA_TYPE"}

for env in DEPRECATED:
    if getenv(env):
        print(f"\n\n[!] WARNING: {env} is deprecated\n\n")

for key in environ:
    if not MOTION and key.startswith("MOTION_WEBHOOKS"):
        print(f"[!] WARNING: {key} will not trigger because MOTION_API is not set")

for key, value in environ.items():
    if key.startswith("WEB_"):
        new_key = key.replace("WEB", "WB")
        print(f"\n[!] WARNING: In {BUILD_STR}, {key} is deprecated! Please use {new_key} instead\n")
        environ.pop(key, None)
        environ[new_key] = value


def _parse_overrides_text(text: str) -> Dict[str, str]:
    """Accept nickname:ip or nickname=ip, separated by newlines/commas/semicolons."""
    mapping: Dict[str, str] = {}
    if not text:
        return mapping
    text = text.replace(",", "\n").replace(";", "\n")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            k, v = line.split(":", 1)
        elif "=" in line:
            k, v = line.split("=", 1)
        else:
            continue
        k, v = k.strip(), v.strip()
        if k and v:
            mapping[k] = v
    return mapping

def load_ip_overrides() -> Dict[str, str]:
    """
    Return merged {nickname: ip} from file + env (env wins).
    - File path: /config/ip_overrides.json (if present)
    - Env var : WB_IP_OVERRIDES (from HA add-on Configuration)
    """
    file_map: Dict[str, str] = {}
    try:
        with open(IP_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            file_map = {str(k).strip(): str(v).strip() for k, v in raw.items()}
    except Exception:
        file_map = {}

    env_map = _parse_overrides_text(os.getenv("WB_IP_OVERRIDES", ""))
    return {**file_map, **env_map}

def save_ip_overrides(mapping: Dict[str, str]) -> bool:
    """Persist overrides from any UI path that writes to disk."""
    try:
        os.makedirs(TOKEN_PATH, exist_ok=True)
        with open(IP_OVERRIDES_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False
