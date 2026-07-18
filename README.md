# iDRAC MQTT Poller — Home Assistant Add-on

Polls one or more Dell iDRAC servers via IPMI over LAN (`ipmitool`) and publishes hardware sensor data to MQTT with full Home Assistant auto-discovery.

## Features

- **Multi-server** support — poll any number of iDRAC hosts from one add-on instance
- **Auto-discovery** — sensors and buttons appear automatically in HA (no manual YAML config)
- **Sensors**: power state, ambient temperature, fan RPMs (1–5), system power (W), PSU voltage/current, fan redundancy, overall health, last SEL error
- **Buttons**: Power On, Soft Shutdown, Power Off (hard), Power Cycle, Clear SEL
- **OS reachability** check (optional HTTP ping to confirm the OS is actually up)
- Configurable poll interval, command timeout, and log level

## Requirements

- Dell server with iDRAC (tested on iDRAC6 firmware 2.92, should work on later versions)
- IPMI over LAN enabled in iDRAC settings
- An MQTT broker reachable from your HA instance (e.g. Mosquitto add-on)

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) in the top-right and choose **Repositories**
3. Add: `https://github.com/richcj10/ha-idrac-plugin`
4. Find **iDRAC MQTT Poller** in the store and click **Install**

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `servers` | list | — | List of iDRAC servers to poll (see below) |
| `mqtt_host` | string | `127.0.0.1` | MQTT broker host |
| `mqtt_port` | port | `1883` | MQTT broker port |
| `mqtt_user` | string | _(empty)_ | MQTT username (leave blank for anonymous) |
| `mqtt_password` | password | _(empty)_ | MQTT password |
| `mqtt_topic_prefix` | string | `idrac` | Root topic prefix; each server publishes under `<prefix>/<server_name>/` |
| `poll_interval` | int | `30` | Seconds between polls (minimum 5) |
| `command_timeout` | int | `20` | ipmitool timeout in seconds (minimum 3) |
| `log_level` | enum | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

### Server entry fields

| Field | Type | Description |
|---|---|---|
| `name` | string | Friendly name — used in topic path and HA entity names |
| `idrac_host` | string | iDRAC IP address or hostname |
| `idrac_user` | string | iDRAC username (commonly `root`) |
| `idrac_password` | password | iDRAC password |
| `os_host` | string | _(optional)_ IP/hostname to HTTP-ping for OS reachability check |

### Example configuration

```yaml
servers:
  - name: "Tower"
    idrac_host: "192.168.1.50"
    idrac_user: "root"
    idrac_password: "your-idrac-password"
    os_host: "192.168.1.10"
mqtt_host: "127.0.0.1"
mqtt_port: 1883
mqtt_user: ""
mqtt_password: ""
mqtt_topic_prefix: "idrac"
poll_interval: 30
command_timeout: 20
log_level: "INFO"
```

## MQTT Topics

All topics are rooted at `<mqtt_topic_prefix>/<server_name>/`.

| Topic | Description |
|---|---|
| `.../power_state` | `on` or `off` |
| `.../overall_health` | `ok`, `warning`, or `critical` |
| `.../ambient_temp_c` | Inlet temperature in °C |
| `.../fan_1_rpm` … `fan_5_rpm` | Fan speeds in RPM |
| `.../system_power_w` | System power draw in watts |
| `.../psu_input_voltage_v` | PSU input voltage |
| `.../psu_input_current_a` | PSU input current |
| `.../fan_redundancy` | Fan redundancy status string |
| `.../os_status` | `online` / `offline` (only if `os_host` is set) |
| `.../available` | `online` / `offline` (add-on connectivity) |
| `.../last_error` | Last poll or command error (empty string when healthy) |
| `.../sdr_raw` | Raw SDR output (for debugging) |
| `.../sel_raw` | Last 20 SEL entries (no retain) |
| `.../command/power` | Publish `on`/`off`/`cycle`/`reset`/`soft` to control power |
| `.../command/sel` | Publish `clear` to wipe the System Event Log |

## Notes

- **iDRAC6 soft shutdown**: a `chassis power status` call is issued immediately before `chassis power soft` to prime the lanplus session — this is required for the ACPI signal to reach the OS on older iDRAC firmware.
- Fan discovery is hardcoded to fans 1–5. Additional fans will publish data but won't have HA entities.
- The device model reported to HA is `iDRAC` (not read dynamically from the server).
