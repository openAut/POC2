#!/usr/bin/env python3
"""
openAut Shunt Control — POC2

Edge control loop that runs ON the Siemens IOT2050, against local Modbus I/O
(Siemens EM1.8 Desigo Essentials modules) on the RS485 bus. No round-trip to
the AI server.

What it does each cycle:
  1. Read supply/return/outdoor temperatures + pump status from EM1.8 modules.
  2. Compute the supply-temperature setpoint from a 6-point heating curve
     (outdoor temp -> supply setpoint, linear interpolation).
  3. Run a PI controller to drive the valve actuator toward the setpoint.
  4. Start/stop the circulation pump on outdoor temperature (with hysteresis).
  5. Publish every value, the control state and alarms to MQTT.

Safety model (defence-in-depth; field interlocks always retain priority):
  - Output bounds: valve % is clamped to [valve_min_pct, valve_max_pct] before
    any write; the raw actuator is never commanded outside its declared range.
  - Watchdog: if a cycle does not complete within watchdog_timeout_s, or cycles
    stop, the loop enters fail-safe.
  - Fail-safe = HOLD LAST (configurable): on sensor loss, pump alarm or watchdog
    timeout the last valve position and pump state are held and no new control
    is written until the fault clears.

Usage:
    python3 shunt_control.py /opt/openaut/shunt/config.json

MQTT topics:
    openaut/{site}/shunt/{signal}
Payload:
    {"value": X, "unit": "...", "ts": "...", "site": "...", "group": "shunt"}
"""

import json
import sys
import time
import logging
import signal
import struct
from datetime import datetime, timezone

try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    sys.exit("[ERROR] pymodbus not installed. Run: pip3 install pymodbus")

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("[ERROR] paho-mqtt not installed. Run: pip3 install paho-mqtt")

# paho-mqtt 2.x exposes CallbackAPIVersion; 1.x does not.
_HAS_CALLBACK_API_VERSION = hasattr(mqtt, "CallbackAPIVersion")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("openaut.shunt")


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _handle_signal(sig, frame):
    global _running
    log.info("Shutdown signal received, stopping...")
    _running = False


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return _strip_comments(cfg)


def _strip_comments(obj):
    """Recursively drop keys starting with '_' (JSON comment convention)."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Modbus decode / encode
# ---------------------------------------------------------------------------
def decode_value(registers: list[int], data_type: str) -> float:
    if data_type == "int16":
        raw = registers[0]
        return raw if raw < 0x8000 else raw - 0x10000
    if data_type == "uint16":
        return registers[0]
    if data_type in ("int32", "uint32", "float32"):
        if len(registers) < 2:
            raise ValueError(f"Need 2 registers for {data_type}, got {len(registers)}")
        combined = (registers[0] << 16) | registers[1]
        if data_type == "float32":
            return struct.unpack(">f", struct.pack(">I", combined))[0]
        if data_type == "int32":
            return combined if combined < 0x80000000 else combined - 0x100000000
        return combined  # uint32
    raise ValueError(f"Unknown data_type: {data_type}")


def register_count(data_type: str) -> int:
    return 2 if data_type in ("int32", "uint32", "float32") else 1


def read_point(client: ModbusSerialClient, point: dict) -> float | None:
    """Read a single I/O point. Returns scaled value or None on failure."""
    slave = point["slave_id"]
    fc = point["function_code"]
    addr = point["address"]
    dtype = point.get("data_type", "int16")
    count = register_count(dtype)
    scale = point.get("scale", 1)
    offset = point.get("offset", 0)
    try:
        if fc == 3:
            res = client.read_holding_registers(address=addr, count=count, slave=slave)
        elif fc == 4:
            res = client.read_input_registers(address=addr, count=count, slave=slave)
        else:
            log.warning("Unsupported function_code %s", fc)
            return None
        if res.isError():
            log.warning("Modbus read error (slave %s addr %s): %s", slave, addr, res)
            return None
        return round(decode_value(res.registers, dtype) * scale + offset, 4)
    except ModbusException as e:
        log.warning("ModbusException (slave %s addr %s): %s", slave, addr, e)
        return None
    except Exception as e:  # noqa: BLE001
        log.error("Unexpected read error (slave %s addr %s): %s", slave, addr, e)
        return None


def write_register(client: ModbusSerialClient, point: dict, value: int) -> bool:
    """Write a single holding register (used for valve AO and pump relay)."""
    slave = point["slave_id"]
    addr = point["address"]
    try:
        res = client.write_register(address=addr, value=int(value), slave=slave)
        if res.isError():
            log.warning("Modbus write error (slave %s addr %s): %s", slave, addr, res)
            return False
        return True
    except ModbusException as e:
        log.warning("ModbusException on write (slave %s addr %s): %s", slave, addr, e)
        return False
    except Exception as e:  # noqa: BLE001
        log.error("Unexpected write error (slave %s addr %s): %s", slave, addr, e)
        return False


# ---------------------------------------------------------------------------
# Heating curve — 6-point outdoor-compensated, linear interpolation
# ---------------------------------------------------------------------------
def curve_setpoint(outdoor: float, points: list[dict]) -> float:
    """Interpolate supply setpoint from outdoor temp. Clamps outside endpoints."""
    pts = sorted(points, key=lambda p: p["outdoor"])
    if outdoor <= pts[0]["outdoor"]:
        return pts[0]["supply"]
    if outdoor >= pts[-1]["outdoor"]:
        return pts[-1]["supply"]
    for a, b in zip(pts, pts[1:]):
        if a["outdoor"] <= outdoor <= b["outdoor"]:
            span = b["outdoor"] - a["outdoor"]
            if span == 0:
                return a["supply"]
            frac = (outdoor - a["outdoor"]) / span
            return a["supply"] + frac * (b["supply"] - a["supply"])
    return pts[-1]["supply"]  # unreachable, defensive


# ---------------------------------------------------------------------------
# PI controller for valve position
# ---------------------------------------------------------------------------
class PIController:
    def __init__(self, kp: float, ki: float, out_min: float, out_max: float):
        self.kp = kp
        self.ki = ki
        self.out_min = out_min
        self.out_max = out_max
        self._integral = 0.0

    def step(self, error: float, dt: float) -> float:
        # Tentative integral update
        self._integral += error * dt
        output = self.kp * error + self.ki * self._integral
        # Clamp + anti-windup: if saturated, hold integral
        if output > self.out_max:
            output = self.out_max
            self._integral -= error * dt
        elif output < self.out_min:
            output = self.out_min
            self._integral -= error * dt
        return output


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
def _on_connect(client, userdata, flags, reason_code, properties=None):
    failed = getattr(reason_code, "is_failure", None)
    if failed is True or (isinstance(reason_code, int) and reason_code != 0):
        log.error("MQTT connection refused (rc=%s)", reason_code)
    else:
        log.info("MQTT connected (rc=%s)", reason_code)


def _on_disconnect(client, userdata, disconnect_flags=None, reason_code=None, properties=None):
    log.warning("MQTT disconnected (rc=%s)", reason_code)


def build_mqtt_client(cfg: dict) -> mqtt.Client:
    mc = cfg["mqtt"]
    client_id = mc.get("client_id", "openaut-shunt")
    if _HAS_CALLBACK_API_VERSION:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
    else:
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

    user = mc.get("username", "").strip()
    pwd = mc.get("password", "").strip()
    if user:
        client.username_pw_set(user, pwd or None)
    if mc.get("tls", False):
        client.tls_set()
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    return client


def mqtt_connect(client: mqtt.Client, cfg: dict) -> bool:
    mc = cfg["mqtt"]
    try:
        client.connect(mc["host"], mc.get("port", 1883), keepalive=60)
        client.loop_start()
        time.sleep(0.5)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Cannot connect to MQTT %s:%s — %s", mc["host"], mc.get("port", 1883), e)
        return False


def publish(client: mqtt.Client, site: str, signal: str, value, unit: str = "") -> None:
    topic = f"openaut/{site}/shunt/{signal}"
    payload = json.dumps({
        "value": value,
        "unit": unit,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": site,
        "group": "shunt",
    })
    res = client.publish(topic, payload=payload, qos=1)
    if res.rc != mqtt.MQTT_ERR_SUCCESS:
        log.warning("MQTT publish failed for %s (rc=%s)", topic, res.rc)


# ---------------------------------------------------------------------------
# Plausibility
# ---------------------------------------------------------------------------
def plausible(value: float, point: dict) -> bool:
    mn, mx = point.get("min_plausible"), point.get("max_plausible")
    if mn is not None and value < mn:
        return False
    if mx is not None and value > mx:
        return False
    return True


# ---------------------------------------------------------------------------
# Controller state
# ---------------------------------------------------------------------------
class ShuntState:
    def __init__(self):
        self.valve_pct = 0.0       # last commanded valve position
        self.pump_on = False       # last commanded pump state
        self.failsafe = False
        self.failsafe_reason = ""
        self.consecutive_failures = 0


def enter_failsafe(state: ShuntState, reason: str) -> None:
    if not state.failsafe:
        log.error("FAIL-SAFE (hold-last): %s", reason)
    state.failsafe = True
    state.failsafe_reason = reason


def clear_failsafe(state: ShuntState) -> None:
    if state.failsafe:
        log.warning("Fail-safe cleared — resuming control")
    state.failsafe = False
    state.failsafe_reason = ""
    state.consecutive_failures = 0


# ---------------------------------------------------------------------------
# One control cycle
# ---------------------------------------------------------------------------
def control_cycle(modbus, mqtt_client, cfg, io, pi, state, dt):
    site = cfg["site"]
    safety = cfg["safety"]

    # --- Read inputs ---
    supply = read_point(modbus, io["supply_temp"])
    ret = read_point(modbus, io["return_temp"])
    outdoor = read_point(modbus, io["outdoor_temp"])
    pump_alarm_raw = read_point(modbus, io["pump_alarm"])
    pump_run_raw = read_point(modbus, io["pump_run"])

    # Validate critical sensors (supply + outdoor needed for control)
    sensor_ok = True
    for name, val, pt in (
        ("supply_temp", supply, io["supply_temp"]),
        ("return_temp", ret, io["return_temp"]),
        ("outdoor_temp", outdoor, io["outdoor_temp"]),
    ):
        if val is None or not plausible(val, pt):
            log.warning("Sensor %s invalid (value=%s)", name, val)
            sensor_ok = False

    pump_alarm = bool(pump_alarm_raw) if pump_alarm_raw is not None else None
    pump_run = bool(pump_run_raw) if pump_run_raw is not None else None

    # --- Publish raw readings (whatever we did read) ---
    if supply is not None:
        publish(mqtt_client, site, "supply_temp", supply, "°C")
    if ret is not None:
        publish(mqtt_client, site, "return_temp", ret, "°C")
    if outdoor is not None:
        publish(mqtt_client, site, "outdoor_temp", outdoor, "°C")
    if pump_alarm is not None:
        publish(mqtt_client, site, "pump_alarm", int(pump_alarm), "")
    if pump_run is not None:
        publish(mqtt_client, site, "pump_run", int(pump_run), "")

    # --- Fault handling -> hold-last fail-safe ---
    if not sensor_ok:
        state.consecutive_failures += 1
        if state.consecutive_failures >= safety.get("max_consecutive_read_failures", 5):
            enter_failsafe(state, "Sensor read failures exceeded threshold")
    elif pump_alarm:
        enter_failsafe(state, "Pump alarm active")
    else:
        # Healthy cycle clears a previous fail-safe (hold_last is recoverable)
        clear_failsafe(state)

    if state.failsafe:
        # HOLD LAST: do not recompute or write new outputs. Re-assert last
        # known commands so the modules keep the held state, then report.
        write_register(modbus, io["valve_position"], round(state.valve_pct))
        write_register(modbus, io["pump_start"], 1 if state.pump_on else 0)
        publish(mqtt_client, site, "valve_position", round(state.valve_pct, 1), "%")
        publish(mqtt_client, site, "pump_start", int(state.pump_on), "")
        publish(mqtt_client, site, "mode", "failsafe_hold_last", "")
        publish(mqtt_client, site, "failsafe_reason", state.failsafe_reason, "")
        log.info("FAIL-SAFE hold: valve=%.1f%% pump=%s (%s)",
                 state.valve_pct, state.pump_on, state.failsafe_reason)
        return

    # --- Normal control ---
    setpoint = curve_setpoint(outdoor, cfg["heating_curve"]["points"])
    error = setpoint - supply

    deadband = cfg["control"].get("deadband_c", 0.0)
    if abs(error) <= deadband:
        valve_pct = state.valve_pct  # within deadband: hold
    else:
        valve_pct = pi.step(error, dt)

    vp = io["valve_position"]
    valve_pct = max(vp.get("output_min", 0), min(vp.get("output_max", 100), valve_pct))

    # Pump logic: on below limit, off above limit+hysteresis
    pcfg = cfg["pump"]
    start_below = pcfg["start_below_c"]
    hyst = pcfg.get("hysteresis_c", 1.0)
    if outdoor < start_below:
        pump_cmd = True
    elif outdoor > start_below + hyst:
        pump_cmd = False
    else:
        pump_cmd = state.pump_on  # in hysteresis band: hold

    # --- Write outputs ---
    write_register(modbus, vp, round(valve_pct))
    write_register(modbus, io["pump_start"], 1 if pump_cmd else 0)

    state.valve_pct = valve_pct
    state.pump_on = pump_cmd

    # --- Publish control state ---
    publish(mqtt_client, site, "setpoint", round(setpoint, 1), "°C")
    publish(mqtt_client, site, "valve_position", round(valve_pct, 1), "%")
    publish(mqtt_client, site, "pump_start", int(pump_cmd), "")
    publish(mqtt_client, site, "mode", "auto", "")

    log.info("out=%.1f°C set=%.1f°C sup=%.1f°C err=%+.1f valve=%.1f%% pump=%s",
             outdoor, setpoint, supply, error, valve_pct, pump_cmd)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <config.json>")
        sys.exit(1)

    cfg = load_config(sys.argv[1])
    site = cfg["site"]
    rs485 = cfg["rs485"]
    io = cfg["io"]
    interval = cfg.get("control_interval_seconds", 10)
    watchdog = cfg["safety"].get("watchdog_timeout_s", 30)

    log.info("openAut shunt control — site=%s interval=%ss watchdog=%ss",
             site, interval, watchdog)
    log.info("Heating curve points: %s",
             [(p["outdoor"], p["supply"]) for p in cfg["heating_curve"]["points"]])

    # --- Modbus ---
    modbus = ModbusSerialClient(
        port=rs485["port"],
        baudrate=rs485["baudrate"],
        parity=rs485.get("parity", "N"),
        stopbits=rs485.get("stopbits", 1),
        bytesize=rs485.get("bytesize", 8),
        timeout=rs485.get("timeout", 1.0),
    )
    if not modbus.connect():
        log.error("Cannot open serial port %s — check RS485 mode/wiring", rs485["port"])
        sys.exit(1)
    log.info("Modbus serial connected: %s @ %d baud", rs485["port"], rs485["baudrate"])

    # --- MQTT ---
    mqtt_client = build_mqtt_client(cfg)
    retry = 5
    while _running:
        if mqtt_connect(mqtt_client, cfg):
            break
        log.warning("Retrying MQTT in %ds...", retry)
        time.sleep(retry)
        retry = min(retry * 2, 60)
    if not _running:
        modbus.close()
        sys.exit(0)

    # --- Controller ---
    c = cfg["control"]
    pi = PIController(c["kp"], c["ki"],
                     c.get("valve_min_pct", 0), c.get("valve_max_pct", 100))
    state = ShuntState()

    log.info("Starting control loop")
    last = time.monotonic()

    while _running:
        t0 = time.monotonic()
        dt = max(0.001, t0 - last)
        last = t0

        try:
            control_cycle(modbus, mqtt_client, cfg, io, pi, state, dt)
        except Exception as e:  # noqa: BLE001 — any fault -> fail-safe hold
            enter_failsafe(state, f"Unhandled exception in control cycle: {e!r}")
            # Re-assert held outputs defensively
            try:
                write_register(modbus, io["valve_position"], round(state.valve_pct))
                write_register(modbus, io["pump_start"], 1 if state.pump_on else 0)
            except Exception:  # noqa: BLE001
                log.critical("Could not re-assert outputs during fail-safe")

        # Watchdog on cycle duration
        elapsed = time.monotonic() - t0
        if elapsed > watchdog:
            enter_failsafe(state, f"Watchdog: cycle took {elapsed:.1f}s (limit {watchdog}s)")

        # Sleep remainder, responsive to SIGTERM
        sleep_for = max(0.0, interval - (time.monotonic() - t0))
        end = time.monotonic() + sleep_for
        while _running and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()))

    log.info("Shutting down (outputs held at last commanded state)...")
    modbus.close()
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    log.info("Stopped.")


if __name__ == "__main__":
    main()
