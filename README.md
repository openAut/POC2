# openAut POC2 — Shuntgruppsreglering

> Reglerar en värme-shuntgrupp från samma Siemens IOT2050 som POC1, via Siemens
> EM1.8 (Desigo Essentials) Modbus-I/O. En 6-punkts utomhuskompenserad värmekurva
> styr framledningstemperaturen; värden, status och larm publiceras till MQTT och
> lagras i TimescaleDB.

Bygger vidare på **POC1** och återanvänder dess mini-PC-stack (EMQX, TimescaleDB,
Telegraf, Grafana). POC2 lägger till en edge-process på IOT2050 som **läser och
styr** — till skillnad från POC1 som bara läser.

---

## Hårdvara

| Enhet | Roll | Standard-IP |
|-------|------|-------------|
| Mini-PC | OpenClaw · TimescaleDB · EMQX · Telegraf · Grafana (delas med POC1) | 192.168.10.10 |
| Asus GX10 | Lokal LLM (Nemotron) | 192.168.10.20 |
| Siemens IOT2050 | Edge-nod — Modbus RTU + shuntreglering | 192.168.10.50 |
| Moxa UPort 1150 | USB-RS485-adapter — egen buss för EM1.8-modulerna | /dev/openaut-shunt |

### Siemens EM1.8-moduler (RS485 via Moxa, egna slave-ID)

| Modul | Funktion | Används till |
|-------|----------|--------------|
| **EM1.8U** | 8 universella I/O | Framledning-, retur-, utetemp (resistiva in) + ventilställdon (analog ut) |
| **EM1.8R** | 8 reläutgångar | Pumpstart |
| **EM1.8D** | 8 digitala ingångar | Pumplarm, pumpdrift |

---

## RS485-bussar — POC1 och POC2 är separerade

POC2 kör EM1.8-modulerna på en **egen** buss via en **Moxa UPort 1150**
USB-RS485-adapter. POC1 fortsätter på IOT2050:s inbyggda X30-port (`/dev/ttyS2`).
Eftersom det är två fysiskt skilda portar finns ingen busskonflikt — `openaut-modbus`
(POC1) och `openaut-shunt` (POC2) kör samtidigt utan att störa varandra.

**Stabilt enhetsnamn (viktigt):** USB-serieportar kan byta nummer mellan
omstarter (`ttyUSB0` ↔ `ttyUSB1`). Därför pinnas Moxa-adaptern till en fast
symlänk `/dev/openaut-shunt` via en udev-regel (`edge/99-openaut-moxa.rules`),
och configen pekar på den symlänken — inte på `ttyUSB0` direkt.

---

## Moxa UPort 1150 — RS-485 2-wire på Linux (gör detta först)

UPort 1150 stödjer RS-232, RS-422, RS-485 2-wire och 4-wire i hårdvaran, men
det *elektriska* läget måste väljas i mjukvara. Linux har två drivrutiner och de
beter sig olika:

- **Mainline `mxuport`** (följer med kärnan, laddas automatiskt) ger dig
  `/dev/ttyUSB*` direkt — men kan **inte** på ett tillförlitligt sätt växla det
  elektriska gränssnittet (2W/4W/422). Standard-ioctl:n (`TIOCSRS485`) räcker
  inte för att välja interface på den här enheten.
- **Moxas egen `mxu11x0`** använder `setserial` för att välja läge och är den
  **stödda vägen** för programmatisk 2-wire-växling.

> Använd därför Moxas `mxu11x0`-drivrutin för POC2.

### 1. Installera Moxas mxu11x0-drivrutin

```bash
# Byggberoenden (en gång)
sudo apt-get update
sudo apt-get install -y build-essential linux-headers-$(uname -r) git setserial

# Hämta drivrutinen
git clone https://github.com/Moxa-Linux/mxu11x0.git
cd mxu11x0

# Bygg och installera
make
sudo make install      # installerar mxu11x0.ko + laddar modulen
```

> **Nyare kärna?** Om `make` fallerar på kärna ≥ 6.x, använd community-forken
> `https://github.com/j-hc/mxu11x0-linux-v6.5` som har patchats för moderna
> kärnor. På IOT2050 (Debian/Yocto-baserad) — kontrollera `uname -r` och välj
> källa därefter.

Verifiera att rätt modul laddats (inte mainline `mxuport`):

```bash
lsmod | grep -E 'mxu11x0|mxuport'
dmesg | grep -i mxu | tail
```

Om mainline `mxuport` redan ockuperar enheten, blockera den så Moxas modul
används i stället:

```bash
echo 'blacklist mxuport' | sudo tee /etc/modprobe.d/blacklist-mxuport.conf
sudo modprobe -r mxuport 2>/dev/null || true
sudo modprobe mxu11x0
```

### 2. Sätt porten till RS-485 2-wire (`port 1`)

`setserial` väljer det elektriska läget. Värden för UPort 1150:
`0 = RS-232`, **`1 = RS-485 2W`**, `2 = RS-422`, `3 = RS-485 4W`.

```bash
sudo setserial /dev/ttyUSB0 port 1     # RS-485 2-wire
setserial -G /dev/ttyUSB0              # verifiera (ska visa 'port 1')
```

### 3. Gör läget bestående över omstart

Lägg läges-kommandot i en liten systemd-unit som binds till enheten, så att
2-wire sätts varje gång adaptern dyker upp:

```bash
sudo tee /etc/systemd/system/openaut-moxa-rs485.service >/dev/null <<'EOF'
[Unit]
Description=Set Moxa UPort 1150 to RS-485 2-wire
After=dev-openaut\x2dshunt.device
BindsTo=dev-openaut\x2dshunt.device

[Service]
Type=oneshot
ExecStart=/usr/bin/setserial /dev/openaut-shunt port 1
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now openaut-moxa-rs485.service
```

### 4. Installera udev-regeln för stabilt namn

```bash
udevadm info -a -n /dev/ttyUSB0 | grep -E 'serial|idVendor|idProduct'
# fyll i ATTRS{serial} i edge/99-openaut-moxa.rules, sedan:
sudo cp edge/99-openaut-moxa.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/openaut-shunt   # ska peka på Moxans ttyUSB*
```

---

## Reglerlogik

**Värmekurva (6 punkter):** utetemp → framledningsbörvärde, linjär interpolation,
klampning utanför ändpunkterna. Standardkurvan i exemplet:

| Ute °C | -20 | -10 | 0 | 5 | 12 | 17 |
|--------|-----|-----|---|---|----|----|
| Framledning °C | 60 | 52 | 44 | 38 | 30 | 22 |

**Ventil:** PI-regulator (`control.kp`, `control.ki`) driver ventilläget (0–100%)
så att framledningstemp följer kurvans börvärde. `deadband_c` håller ventilen still
vid små avvikelser.

**Pump:** startar när utetemp < `pump.start_below_c` (default 17°C), stoppar över
gränsen + `hysteresis_c` (sommarstopp utan korttcykling).

**Fail-safe = håll senaste läge:** vid sensorbortfall, pumplarm eller
watchdog-timeout fryses ventilläge och pumpstatus, och ingen ny styrning skrivs
förrän felet rättats. Ventilläget klampas alltid till `output_min/max` före
skrivning. Fältets interlocks och frysskydd har alltid prioritet — loopen är
supervisory, inte en säkerhetsfunktion.

---

## Driftsättning (via OpenClaw)

1. Installera Moxas mxu11x0-drivrutin, sätt porten till RS-485 2-wire (`port 1`)
   och installera udev-regeln (se avsnittet ovan).

2. Kopiera och fyll i config:
   ```bash
   cp config/example-shunt-config.json config/shunt-config.json
   # Fyll i: slave-ID, registeradresser (från Siemens datablad A6V13841491),
   # ventilskalning, kurva och pumpgräns. Lämna rs485.port = /dev/openaut-shunt.
   nano config/shunt-config.json
   ```
   > EM1.8 registernumrering i databladet är **1-baserad**; pymodbus är 0-baserad
   > — subtrahera 1.

3. Säg till agenten:
   ```
   Driftsätt shuntregleringen.
   Konfig: config/shunt-config.json
   ```
   OpenClaw kör `shunt-control-integration`:
   verifierar SSH → kör `setup.sh` → bekräftar /dev/openaut-shunt → skannar
   EM1.8-modulerna → kopierar `shunt_control.py` + config → startar systemd-service
   → verifierar MQTT-telemetri.

4. Skapa databastabellen (en gång):
   ```bash
   docker compose exec timescaledb psql -U openaut -d openaut < db/shunt_init.sql
   ```

5. Lägg till Telegraf-konsumenten (`telegraf/shunt-mqtt.conf`) i POC1:s
   Telegraf-config så att `openaut/<site>/shunt/#` ingestas.

---

## Dataflöde

```
EM1.8U/R/D (Modbus RS485)
        ↓  Moxa UPort 1150 → /dev/openaut-shunt
  IOT2050 — shunt_control.py  (läser givare, kör kurva+PI, styr ventil+pump)
        ↓  MQTT  openaut/poc2/shunt/{signal}
  EMQX broker (Mini-PC)
        ↓  Telegraf
  TimescaleDB → openaut.shunt_readings
        ↑
  Grafana :3000
```

MQTT-signaler: `supply_temp`, `return_temp`, `outdoor_temp`, `setpoint`,
`valve_position`, `pump_start`, `pump_run`, `pump_alarm`, `mode`,
`failsafe_reason`.

---

## Repo-struktur

```
skills/shunt-control-integration/SKILL.md   OpenClaw-skill (SSH-deploy)
config/example-shunt-config.json            Mall: registerkarta + kurva + safety
config/shunt-config.schema.json             JSON-schema för validering
edge/shunt_control.py                       Reglerloop (körs på IOT2050)
edge/openaut-shunt.service                  systemd-service
edge/setup.sh                               Installerar beroenden + deploy-dir
edge/99-openaut-moxa.rules                  udev: stabilt namn för Moxa UPort 1150
db/shunt_init.sql                           TimescaleDB-tabell
telegraf/shunt-mqtt.conf                    Telegraf MQTT → TimescaleDB
AGENTS.md                                   Instruktioner för OpenClaw-agenten
```

---

## Verifiering

```bash
# Rätt drivrutin laddad (mxu11x0, inte mainline mxuport)?
ssh openaut@192.168.10.50 "lsmod | grep -E 'mxu11x0|mxuport'"

# Moxa-adaptern syns och har stabilt namn?
ssh openaut@192.168.10.50 "ls -l /dev/openaut-shunt"

# Är porten i RS-485 2-wire (port 1)?
ssh openaut@192.168.10.50 "setserial -G /dev/openaut-shunt"

# Service
ssh openaut@192.168.10.50 "sudo systemctl status openaut-shunt"

# Live-telemetri (börvärde ska matcha kurvan vid aktuell utetemp)
mosquitto_sub -h 192.168.10.10 -t "openaut/poc2/shunt/#" -v

# Senaste värden i databasen
docker compose exec timescaledb psql -U openaut -d openaut \
  -c "SELECT signal_name, value, unit, time FROM openaut.shunt_readings ORDER BY time DESC LIMIT 15;"
```

---

MIT License · [openAut](https://github.com/openAut) · [openaut.io](https://openaut.io)
