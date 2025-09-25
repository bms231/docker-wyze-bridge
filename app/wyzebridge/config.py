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

'''
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
'''
import os
import json
from typing import Dict, Tuple, Optional

# existing:
# TOKEN_PATH = ...
IP_OVERRIDES_FILE = os.path.join(TOKEN_PATH, "ip_overrides.json")


def _parse_overrides_text(text: str) -> Dict[str, Tuple[str, Optional[int]]]:
    """
    Accepts nickname:ip[:p2p_type] or nickname=ip[:p2p_type],
    separated by newlines/commas/semicolons.
    Returns {nickname: (ip, p2p_type_or_None)}.
    """
    mapping: Dict[str, Tuple[str, Optional[int]]] = {}
    if not text:
        return mapping

    text = text.replace(",", "\n").replace(";", "\n")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # split nickname from value using first ':' or '='
        if ":" in line:
            nick, val = line.split(":", 1)
        elif "=" in line:
            nick, val = line.split("=", 1)
        else:
            # malformed line; ignore
            continue

        nick = nick.strip()
        val = val.strip()
        if not nick or not val:
            continue

        # Now parse val: either "ip" or "ip:p2p_type"
        ip = val
        p2p_val: Optional[int] = None
        if ":" in val:
            ip_part, maybe_p2p = val.split(":", 1)
            ip_part = ip_part.strip()
            maybe_p2p = maybe_p2p.strip()
            if ip_part:
                ip = ip_part
            if maybe_p2p:
                try:
                    p2p_val = int(maybe_p2p)
                except ValueError:
                    p2p_val = None  # ignore bad p2p token

        if ip:
            mapping[nick] = (ip, p2p_val)

    return mapping


def _coerce_file_value(v) -> Tuple[str, Optional[int]]:
    """
    Backward/forward compatible coercion of a JSON value from ip_overrides.json:
    - "192.168.0.1"                     -> ("192.168.0.1", None)
    - "192.168.0.1:3"                   -> ("192.168.0.1", 3)
    - {"ip": "...", "p2p_type": 3}      -> ("...", 3)
    - {"ip": "..."}                     -> ("...", None)
    Anything else -> ignored by caller.
    """
    if isinstance(v, str):
        parsed = _parse_overrides_text(f"n:{v}")  # fake nickname to reuse parser
        # will be {"n": (ip, p2p?)} or {}
        if parsed:
            return next(iter(parsed.values()))
    elif isinstance(v, dict):
        ip = str(v.get("ip", "")).strip()
        p2p = v.get("p2p_type", None)
        try:
            p2p_int = int(p2p) if p2p is not None else None
        except (TypeError, ValueError):
            p2p_int = None
        if ip:
            return (ip, p2p_int)
    raise ValueError("uncoercible override value")


def load_ip_overrides() -> Dict[str, Tuple[str, Optional[int]]]:
    """
    Return merged {nickname: (ip, p2p_type_or_None)} from file + env (env wins).
    - File path: /config/ip_overrides.json (if present)
    - Env var : WB_IP_OVERRIDES (from HA add-on Configuration), supports :p2p_type
    """
    file_map: Dict[str, Tuple[str, Optional[int]]] = {}

    # Read JSON file (supports both string values and {ip,p2p_type} objects)
    try:
        with open(IP_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                k_str = str(k).strip()
                try:
                    file_map[k_str] = _coerce_file_value(v)
                except ValueError:
                    # ignore malformed entries
                    continue
    except Exception:
        file_map = {}

    # Parse env text
    env_text = os.getenv("WB_IP_OVERRIDES", "")
    env_map = _parse_overrides_text(env_text)

    # env overrides file on conflicts
    merged = {**file_map, **env_map}
    return merged


def save_ip_overrides(mapping: Dict[str, Tuple[str, Optional[int]]]) -> bool:
    """
    Persist overrides to /config/ip_overrides.json.
    File format stays simple and backward compatible:
      { "Nickname": "ip" } or { "Nickname": "ip:p2p" }
    """
    try:
        os.makedirs(TOKEN_PATH, exist_ok=True)
        serializable: Dict[str, str] = {}
        for k, (ip, p2p) in mapping.items():
            serializable[str(k)] = f"{ip}:{p2p}" if p2p is not None else ip
        with open(IP_OVERRIDES_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False
