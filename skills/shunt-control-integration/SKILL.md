---
name: shunt-control-integration
description: Deploy a heating shunt-group control loop to a Siemens IOT2050 over SSH — reads a JSON config (EM1.8 register map + 6-point heating curve + safety), validates it, installs the Python controller on a dedicated Moxa UPort 1150 RS485 bus, starts a systemd service, and verifies MQTT control telemetry.
metadata: {"openclaw":{"requires":{"bins":["ssh","scp"],"env":["MQTT_HOST"]},"os":["linux","darwin"]}}
---

# Shunt Control Integration (POC2)

Orchestrates deployment of a heating shunt-group control loop onto a Siemens
IOT2050 edge node, using Siemens EM1.8 (Desigo Essentials) Modbus I/O modules
on a dedicated **Moxa UPort 1150** USB-RS485 adapter. Run once per site/config.

## Trigger

Run when the user says things like:
- "driftsätt shuntreglering"
- "sätt upp shuntgruppen"
- "deploy shunt control"
- `/skill shunt-control-integration`

## I/O assumptions (Siemens EM1.8 modules on the Moxa RS485 bus)

| Signal | Direction | Module |
|--------|-----------|--------|
| Framledningstemp | resistiv in | EM1.8U |
| Returtemp | resistiv in | EM1.8U |
| Utetemp | resistiv in | EM1.8U |
| Ventilställdon | analog ut | EM1.8U |
| Pumpstart | reläut | EM1.8R |
| Pumplarm | digital in | EM1.8D |
| Pumpdrift | digital in | EM1.8D |

Each module has a unique Modbus slave ID. Register addresses come from the
Siemens datasheet (A6V13841491). Register numbering in the datasheet is
1-based; pymodbus is 0-based — subtract 1.

## Bus topology — dedicated Moxa adapter (no POC1 conflict)

POC2 runs the EM1.8 modules on a **separate** RS485 bus via a Moxa UPort 1150
USB adapter, NOT on the IOT2050 X30 port (`/dev/ttyS2`) that POC1 uses. The two
services (`openaut-modbus` and `openaut-shunt`) therefore run concurrently
without bus contention.

The config's `rs485.port` should be the stable udev symlink `/dev/openaut-shunt`
(see `edge/99-openaut-moxa.rules`), not a raw `/dev/ttyUSB*` name, so USB
enumeration order can't break the deployment.

## Step 0 — Get the config file path

Ask the user for the path to their shunt config JSON. If they don't have one,
offer to generate from `config/example-shunt-config.json` in this repo.

## Step 1 — Validate config

Confirm required fields exist: `site`, `edge_node.{ip,ssh_user}`,
`mqtt.host`, `rs485.port`, all seven `io.*` points, `heating_curve.points`
(>= 2 points), `control.{kp,ki}`, `pump.start_below_c`, `safety.{mode,watchdog_timeout_s}`.

Validate the heating curve: each point has numeric `outdoor` and `supply`;
warn if points are not monotonic in outdoor temperature.

Print a summary (site, edge node, MQTT, RS485 port, slave IDs per module,
curve points, pump start limit, fail-safe mode).

## Step 2 — Verify SSH

```bash
ssh -o ConnectTimeout=10 -o BatchMode=yes {ssh_user}@{edge_node_ip} "echo OK"
```

Do not proceed until SSH works (key-based; no password prompt).

## Step 3 — Verify the Moxa adapter and stable device name

```bash
# Is the Moxa UPort 1150 present? (USB IDs 110a:1150)
ssh {ssh_user}@{edge_node_ip} "lsusb | grep -i 110a || echo 'Moxa not found'"

# Is the stable symlink in place?
ssh {ssh_user}@{edge_node_ip} "ls -l {rs485.port} || echo 'symlink missing'"
```

If `{rs485.port}` (e.g. `/dev/openaut-shunt`) is missing, install the udev rule:

```bash
scp edge/99-openaut-moxa.rules {ssh_user}@{edge_node_ip}:/tmp/
# The rule needs the adapter's serial; help the user find it:
ssh {ssh_user}@{edge_node_ip} "udevadm info -a -n /dev/ttyUSB0 | grep -E '{{serial|idVendor|idProduct}}' | head"
# After the serial is filled into the rule:
ssh {ssh_user}@{edge_node_ip} "
  sudo cp /tmp/99-openaut-moxa.rules /etc/udev/rules.d/ &&
  sudo udevadm control --reload-rules && sudo udevadm trigger &&
  ls -l {rs485.port}
"
```

Also remind the user the Moxa port must be in **RS-485 2-wire mode** (set via
Moxa's Linux driver/utility for the UPort 1150) before the modules will answer.

## Step 4 — Run setup on the node

```bash
scp edge/setup.sh {ssh_user}@{edge_node_ip}:/tmp/poc2-setup.sh
ssh {ssh_user}@{edge_node_ip} "chmod +x /tmp/poc2-setup.sh && sudo /tmp/poc2-setup.sh"
```

Installs pymodbus/paho-mqtt (idempotent) and creates `/opt/openaut/shunt`.

## Step 5 — Modbus connectivity check (per module)

For each module slave ID in the config, verify it responds on the Moxa bus:

```bash
ssh {ssh_user}@{edge_node_ip} python3 - << 'EOF'
from pymodbus.client import ModbusSerialClient
c = ModbusSerialClient(port="{rs485.port}", baudrate={rs485.baudrate},
                       parity="{rs485.parity}", stopbits={rs485.stopbits},
                       bytesize={rs485.bytesize}, timeout={rs485.timeout})
assert c.connect(), "Cannot open {rs485.port}"
for slave in ({em1_8u_id}, {em1_8r_id}, {em1_8d_id}):
    r = c.read_holding_registers(address=0, count=1, slave=slave)
    print(f"slave {slave}: {'OK' if not r.isError() else 'NO RESPONSE'}")
c.close()
EOF
```

If a module does not respond: check wiring, slave ID (DIP/config), termination
resistor, that the Moxa port is in RS-485 mode, and that the address/function
code in the config match the datasheet. Do not deploy until all three respond.

## Step 6 — Deploy controller + config

```bash
scp edge/shunt_control.py {ssh_user}@{edge_node_ip}:/opt/openaut/shunt/shunt_control.py
scp {config_file_path} {ssh_user}@{edge_node_ip}:/opt/openaut/shunt/config.json
ssh {ssh_user}@{edge_node_ip} "ls -la /opt/openaut/shunt/"
```

## Step 7 — Install + start systemd service

```bash
scp edge/openaut-shunt.service {ssh_user}@{edge_node_ip}:/tmp/openaut-shunt.service
ssh {ssh_user}@{edge_node_ip} "
  sudo cp /tmp/openaut-shunt.service /etc/systemd/system/openaut-shunt.service &&
  sudo systemctl daemon-reload &&
  sudo systemctl enable --now openaut-shunt
"
ssh {ssh_user}@{edge_node_ip} "sudo systemctl status openaut-shunt --no-pager -l"
```

Expected: `Active: active (running)`.

## Step 8 — Verify MQTT control telemetry

```bash
mosquitto_sub -h {mqtt.host} -p {mqtt.port} -t "openaut/{site}/shunt/#" -v
```

Within `control_interval_seconds` you should see: `supply_temp`, `return_temp`,
`outdoor_temp`, `setpoint`, `valve_position`, `pump_start`, `pump_run`,
`pump_alarm`, `mode`. Confirm `setpoint` matches the curve for the current
outdoor temperature, and that `mode` is `auto` (not `failsafe_hold_last`).

## Step 9 — Report (Swedish)

```
✅ Shuntreglering driftsatt — {site}

Edge-nod: {edge_node_ip}
Buss:     Moxa UPort 1150 ({rs485.port})
Moduler:  EM1.8U (slave {u}), EM1.8R (slave {r}), EM1.8D (slave {d})
Kurva:    {N} punkter, börvärde nu {setpoint}°C vid ute {outdoor}°C
Ventil:   {valve}%   Pump: {pump_state}
Fail-safe: håll senaste läge (watchdog {watchdog}s)

Status:  sudo systemctl status openaut-shunt
Loggar:  sudo journalctl -u openaut-shunt -f
Rollback: sudo systemctl disable --now openaut-shunt
```

## Tuning notes

- The valve PI gains are `control.kp` / `control.ki`. Start conservative; if the
  supply temperature oscillates, lower `kp`; if it is sluggish, raise it slowly.
- `control.deadband_c` holds the valve when |setpoint − supply| is small to
  avoid actuator hunting.
- `pump.hysteresis_c` prevents pump short-cycling around the start limit.

## Safety reminder

Fail-safe is **hold-last**: on sensor loss, pump alarm or watchdog timeout the
controller freezes the valve and pump at their last commanded state and stops
issuing new commands until the fault clears. Field interlocks and the plant's
own frost protection always retain priority — this loop is supervisory, not a
safety device.
