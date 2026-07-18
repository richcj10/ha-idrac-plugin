import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("idrac_mqtt")

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

OPTIONS_PATH = "/data/options.json"


def load_options() -> dict:
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def mqtt_connect(opts: dict) -> mqtt.Client:
    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id="idrac_mqtt_poller")

    mqtt_user = opts.get("mqtt_user", "")
    mqtt_password = opts.get("mqtt_password", "")
    if mqtt_user:
        client.username_pw_set(mqtt_user, mqtt_password)
        log.info("MQTT auth: user=%s", mqtt_user)
    else:
        log.info("MQTT auth: anonymous")

    def on_connect(c, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected: reason_code=%s", reason_code)

    def on_disconnect(c, userdata, disconnect_flags, reason_code, properties=None):
        log.warning("MQTT disconnected: reason_code=%s", reason_code)

    def on_subscribe(c, userdata, mid, reason_code_list, properties=None):
        log.info("MQTT subscribed: mid=%s reason_codes=%s", mid, reason_code_list)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_subscribe = on_subscribe

    log.info("MQTT connecting to %s:%s", opts["mqtt_host"], opts["mqtt_port"])
    client.connect(opts["mqtt_host"], int(opts["mqtt_port"]), 60)
    client.loop_start()
    return client


def publish(client: mqtt.Client, topic: str, payload: Any, retain: bool = True) -> None:
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload)
    else:
        payload = str(payload)
    client.publish(topic, payload, qos=0, retain=retain)


def run_ipmitool(server: dict, args: List[str], timeout: int = 20) -> str:
    cmd = [
        "ipmitool",
        "-I", "lanplus",
        "-H", server["idrac_host"],
        "-U", server["idrac_user"],
        "-P", server["idrac_password"],
    ] + args

    # Redacted form for logs (password replaced with ***)
    redacted = list(cmd)
    try:
        pw_idx = redacted.index("-P") + 1
        redacted[pw_idx] = "***"
    except ValueError:
        pass
    log.debug("ipmitool exec: %s (timeout=%ss)", " ".join(redacted), timeout)

    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.monotonic() - start

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    log.debug("ipmitool rc=%s elapsed=%.2fs stdout=%r stderr=%r",
              proc.returncode, elapsed, stdout, stderr)

    if proc.returncode != 0:
        raise RuntimeError(stderr or stdout or f"ipmitool failed: {' '.join(args)}")

    return stdout


def check_os_status(host: str, timeout: int = 3) -> bool:
    try:
        urllib.request.urlopen(f"http://{host}/", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True  # Any HTTP response means OS is up
    except Exception:
        return False


def parse_power_status(text: str) -> str:
    text = text.strip().lower()
    if "on" in text:
        return "on"
    if "off" in text:
        return "off"
    return "unknown"


def parse_sensor_line(line: str) -> Optional[Dict[str, Any]]:
    # Typical ipmitool sdr line:
    # Ambient Temp     | 26 degrees C      | ok
    # FAN 1 RPM        | 5280 RPM          | ok
    # System Level     | 161 Watts         | ok
    # Fan Redundancy   | fully redundant   | ok
    if "|" not in line:
        return None

    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 3:
        return None

    return {
        "name": parts[0],
        "reading": parts[1],
        "status": parts[2].lower(),
    }


def parse_numeric_reading(reading: str) -> Optional[Dict[str, Any]]:
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s+(.+?)\s*$", reading)
    if not m:
        return None
    return {"value": float(m.group(1)), "units": m.group(2).strip()}


def extract_useful_sensors(sdr_text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}

    for raw_line in sdr_text.splitlines():
        parsed = parse_sensor_line(raw_line)
        if not parsed:
            continue

        name = parsed["name"]
        reading = parsed["reading"]
        status = parsed["status"]

        if status not in ("ok", "ns"):
            continue

        numeric = parse_numeric_reading(reading)

        if name == "Ambient Temp" and numeric and "degree" in numeric["units"].lower():
            result["ambient_temp_c"] = numeric["value"]
            continue

        if re.fullmatch(r"FAN\s+\d+\s+RPM", name) and numeric and numeric["units"].lower() == "rpm":
            fan_num = re.findall(r"\d+", name)[0]
            result[f"fan_{fan_num}_rpm"] = numeric["value"]
            continue

        if name == "System Level" and numeric and "watt" in numeric["units"].lower():
            result["system_power_w"] = numeric["value"]
            continue

        if name == "Voltage" and numeric and numeric["units"].lower() == "volts":
            result.setdefault("psu_input_voltage_v", numeric["value"])
            continue

        if name == "Current" and numeric and numeric["units"].lower() == "amps":
            result.setdefault("psu_input_current_a", numeric["value"])
            continue

        if name == "Fan Redundancy":
            result["fan_redundancy"] = reading
            continue

    return result


def summarize_health(sdr_text: str, sel_text: str, power_state: str = "on") -> str:
    if power_state == "on":
        for line in sdr_text.splitlines():
            parsed = parse_sensor_line(line)
            if parsed and parsed["status"] in ("cr", "nr", "critical", "failed"):
                return "critical"

    sel_lower = sel_text.lower()
    if any(x in sel_lower for x in ["critical", "fatal", "uncorrectable", "failure"]):
        return "critical"
    if any(x in sel_lower for x in ["warning", "correctable", "non-critical"]):
        return "warning"

    return "ok"


def publish_discovery(client: mqtt.Client, prefix: str, server: dict) -> None:
    server_name = server["name"]
    server_id = re.sub(r"[^a-z0-9_]", "_", server_name.lower())

    device = {
        "identifiers": [f"idrac_{server_id}"],
        "name": f"iDRAC {server_name}",
        "manufacturer": "Dell",
        "model": "iDRAC",
    }

    sensors = {
        "power_state": {
            "component": "binary_sensor",
            "name": "Power State",
            "state_topic": f"{prefix}/power_state",
            "payload_on": "on",
            "payload_off": "off",
            "device_class": "power",
        },
        "overall_health": {
            "component": "sensor",
            "name": "Overall Health",
            "state_topic": f"{prefix}/overall_health",
            "icon": "mdi:shield-alert-outline",
        },
        "ambient_temp_c": {
            "component": "sensor",
            "name": "Ambient Temp",
            "state_topic": f"{prefix}/ambient_temp_c",
            "device_class": "temperature",
            "state_class": "measurement",
            "unit_of_measurement": "°C",
        },
        "fan_1_rpm": {
            "component": "sensor",
            "name": "Fan 1 RPM",
            "state_topic": f"{prefix}/fan_1_rpm",
            "state_class": "measurement",
            "unit_of_measurement": "RPM",
        },
        "fan_2_rpm": {
            "component": "sensor",
            "name": "Fan 2 RPM",
            "state_topic": f"{prefix}/fan_2_rpm",
            "state_class": "measurement",
            "unit_of_measurement": "RPM",
        },
        "fan_3_rpm": {
            "component": "sensor",
            "name": "Fan 3 RPM",
            "state_topic": f"{prefix}/fan_3_rpm",
            "state_class": "measurement",
            "unit_of_measurement": "RPM",
        },
        "fan_4_rpm": {
            "component": "sensor",
            "name": "Fan 4 RPM",
            "state_topic": f"{prefix}/fan_4_rpm",
            "state_class": "measurement",
            "unit_of_measurement": "RPM",
        },
        "fan_5_rpm": {
            "component": "sensor",
            "name": "Fan 5 RPM",
            "state_topic": f"{prefix}/fan_5_rpm",
            "state_class": "measurement",
            "unit_of_measurement": "RPM",
        },
        "system_power_w": {
            "component": "sensor",
            "name": "System Power",
            "state_topic": f"{prefix}/system_power_w",
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
        },
        "psu_input_voltage_v": {
            "component": "sensor",
            "name": "PSU Input Voltage",
            "state_topic": f"{prefix}/psu_input_voltage_v",
            "state_class": "measurement",
            "unit_of_measurement": "V",
        },
        "psu_input_current_a": {
            "component": "sensor",
            "name": "PSU Input Current",
            "state_topic": f"{prefix}/psu_input_current_a",
            "state_class": "measurement",
            "unit_of_measurement": "A",
        },
        "fan_redundancy": {
            "component": "sensor",
            "name": "Fan Redundancy",
            "state_topic": f"{prefix}/fan_redundancy",
            "icon": "mdi:fan-alert",
        },
        "last_error": {
            "component": "sensor",
            "name": "Last Error",
            "state_topic": f"{prefix}/last_error",
            "icon": "mdi:alert-circle-outline",
        },
    }

    if server.get("os_host"):
        sensors["os_status"] = {
            "component": "binary_sensor",
            "name": "OS Status",
            "state_topic": f"{prefix}/os_status",
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "running",
            "icon": "mdi:server-network",
        }

    for object_id, cfg in sensors.items():
        component = cfg["component"]
        payload = {
            "name": cfg["name"],
            "unique_id": f"idrac_{server_id}_{object_id}",
            "device": device,
            "state_topic": cfg["state_topic"],
        }
        for k, v in cfg.items():
            if k not in ("component", "name", "state_topic"):
                payload[k] = v

        topic = f"homeassistant/{component}/{prefix.replace('/', '_')}/{object_id}/config"
        publish(client, topic, payload, retain=True)

    buttons = {
        "power_on":    {"name": "Power On",        "command": "power", "payload_press": "on"},
        "power_soft":  {"name": "Soft Shutdown",    "command": "power", "payload_press": "soft"},
        "power_off":   {"name": "Power Off (Hard)", "command": "power", "payload_press": "off"},
        "power_cycle": {"name": "Power Cycle",      "command": "power", "payload_press": "cycle"},
        "sel_clear":   {"name": "Clear SEL",        "command": "sel",   "payload_press": "clear", "icon": "mdi:delete-sweep"},
    }

    for object_id, cfg in buttons.items():
        topic = f"homeassistant/button/{prefix.replace('/', '_')}/{object_id}/config"
        payload = {
            "name": cfg["name"],
            "unique_id": f"idrac_{server_id}_{object_id}",
            "device": device,
            "command_topic": f"{prefix}/command/{cfg['command']}",
            "payload_press": cfg["payload_press"],
        }
        if "icon" in cfg:
            payload["icon"] = cfg["icon"]
        publish(client, topic, payload, retain=True)

    availability_topic = f"homeassistant/binary_sensor/{prefix.replace('/', '_')}/availability/config"
    availability_payload = {
        "name": "Available",
        "unique_id": f"idrac_{server_id}_available",
        "device": device,
        "state_topic": f"{prefix}/available",
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
    }
    publish(client, availability_topic, availability_payload, retain=True)


def handle_sel_command(server: dict, payload: str, timeout: int = 20) -> str:
    if payload.strip().lower() != "clear":
        raise ValueError(f"unsupported sel command: {payload}")
    run_ipmitool(server, ["sel", "clear"], timeout)
    return "SEL cleared"


def handle_power_command(server: dict, payload: str, timeout: int = 20) -> str:
    payload = payload.strip().lower()

    commands = {
        "on":    (["chassis", "power", "on"],    "power on requested"),
        "off":   (["chassis", "power", "off"],   "power off requested"),
        "cycle": (["chassis", "power", "cycle"], "power cycle requested"),
        "reset": (["chassis", "power", "reset"], "power reset requested"),
        "soft":  (["chassis", "power", "soft"],  "soft shutdown requested"),
    }

    if payload not in commands:
        raise ValueError(f"unsupported power command: {payload}")

    args, msg = commands[payload]
    log.info("Power command '%s' for %s -> ipmitool %s",
             payload, server.get("name"), " ".join(args))
    before = run_ipmitool(server, ["chassis", "power", "status"], timeout)
    log.info("Power state before '%s': %s", payload, before)
    out = run_ipmitool(server, args, timeout)
    log.info("ipmitool response for '%s': %s", payload, out)
    return msg


def poll_server(server: dict, timeout: int = 20) -> Dict[str, Any]:
    power_raw = run_ipmitool(server, ["chassis", "power", "status"], timeout)
    sdr_raw = run_ipmitool(server, ["sdr"], timeout)
    sel_raw_full = run_ipmitool(server, ["sel", "list", "last", "20"], timeout)

    power_state = parse_power_status(power_raw)
    sensors = extract_useful_sensors(sdr_raw)
    overall_health = summarize_health(sdr_raw, sel_raw_full, power_state)

    result: Dict[str, Any] = {
        "power_state": power_state,
        "overall_health": overall_health,
        "sensors": sensors,
        "power_raw": power_raw,
        "sdr_raw": sdr_raw,
        "sel_raw": sel_raw_full,
    }

    os_host = server.get("os_host", "").strip()
    if os_host:
        result["os_status"] = "online" if check_os_status(os_host) else "offline"

    return result


def publish_poll_results(client: mqtt.Client, prefix: str, results: Dict[str, Any]) -> None:
    publish(client, f"{prefix}/power_state", results["power_state"])
    publish(client, f"{prefix}/overall_health", results["overall_health"])
    publish(client, f"{prefix}/power_raw", results["power_raw"])
    publish(client, f"{prefix}/sdr_raw", results["sdr_raw"])
    publish(client, f"{prefix}/sel_raw", results["sel_raw"], retain=False)
    publish(client, f"{prefix}/available", "online")

    for key, value in results["sensors"].items():
        publish(client, f"{prefix}/{key}", value)

    if "os_status" in results:
        publish(client, f"{prefix}/os_status", results["os_status"])


def main() -> None:
    opts = load_options()

    level_name = str(opts.get("log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.getLogger().setLevel(level)
    log.setLevel(level)
    log.info("iDRAC MQTT poller starting (log_level=%s)", level_name)

    client = mqtt_connect(opts)
    global_prefix = opts["mqtt_topic_prefix"].rstrip("/")
    poll_interval = int(opts.get("poll_interval", 30))
    command_timeout = int(opts.get("command_timeout", opts.get("ipmi_timeout", 20)))
    log.info("Config: prefix=%s poll_interval=%ss command_timeout=%ss",
             global_prefix, poll_interval, command_timeout)

    if "servers" in opts:
        servers = opts["servers"]
    else:
        servers = [{
            "name": "server1",
            "idrac_host": opts["idrac_host"],
            "idrac_user": opts["idrac_user"],
            "idrac_password": opts["idrac_password"],
        }]

    def build_prefix(server: dict) -> str:
        name = server["name"]
        # Avoid double-appending if mqtt_topic_prefix already ends with the server name
        if global_prefix.endswith(f"/{name}") or global_prefix == name:
            return global_prefix
        return f"{global_prefix}/{name}"

    server_entries = [(s, build_prefix(s)) for s in servers]

    # topic -> (server, prefix, handler)
    command_handlers: Dict[str, tuple] = {}

    for server, prefix in server_entries:
        command_handlers[f"{prefix}/command/power"] = (server, prefix, handle_power_command)
        command_handlers[f"{prefix}/command/sel"]   = (server, prefix, handle_sel_command)
        log.info("Registered server '%s' host=%s prefix=%s",
                 server.get("name"), server.get("idrac_host"), prefix)
        publish_discovery(client, prefix, server)

    def on_message(client_obj, userdata, msg):
        raw_payload = msg.payload.decode("utf-8", errors="ignore")
        log.info("MQTT rx: topic=%s payload=%r retain=%s", msg.topic, raw_payload, msg.retain)
        entry = command_handlers.get(msg.topic)
        if entry is None:
            log.debug("No handler registered for topic %s", msg.topic)
            return
        server, prefix, handler = entry
        try:
            response = handler(server, raw_payload, command_timeout)
            log.info("Command result: %s", response)
            publish(client_obj, f"{prefix}/last_command", raw_payload)
            publish(client_obj, f"{prefix}/last_command_result", response)
            publish(client_obj, f"{prefix}/last_error", "")
        except Exception as e:
            log.exception("Command error on %s: %s", msg.topic, e)
            publish(client_obj, f"{prefix}/last_error", f"command error: {e}")

    client.on_message = on_message
    for command_topic in command_handlers:
        log.info("Subscribing to command topic: %s", command_topic)
        client.subscribe(command_topic)

    while True:
        for server, prefix in server_entries:
            log.debug("Polling %s (host=%s)", server.get("name"), server.get("idrac_host"))
            try:
                results = poll_server(server, command_timeout)
                log.debug("Poll ok for %s: power=%s health=%s",
                          server["name"], results["power_state"], results["overall_health"])
                publish_poll_results(client, prefix, results)
                publish(client, f"{prefix}/last_error", "")
            except Exception as e:
                log.error("Poll error for %s: %s", server["name"], e)
                publish(client, f"{prefix}/available", "offline")
                publish(client, f"{prefix}/last_error", str(e))

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
