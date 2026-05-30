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

### Siemens EM1.8-moduler (RS485, egna slave-ID)

| Modul | Funktion | Används till |
|-------|----------|--------------|
| **EM1.8U** | 8 universella I/O | Framledning-, retur-, utetemp (resistiva in) + ventilställdon (analog ut) |
| **EM1.8R** | 8 reläutgångar | Pumpstart |
| **EM1.8D** | 8 digitala ingångar | Pumplarm, pumpdrift |

---

## ⚠️ RS485-samexistens med POC1 (läs detta först)

IOT2050:s X30-port (`/dev/ttyS2`) kan bara öppnas av **en** process. POC1:s
`openaut-modbus.service` äger redan porten för att polla aggregaten. Två tjänster
på samma fysiska port fungerar inte.

Välj en väg innan driftsättning:

1. **Egen adapter (rekommenderas för POC2):** koppla EM1.8-modulerna till en
   USB-RS485-adapter (`/dev/ttyUSB0`) och sätt `rs485.port` därefter. Då kör AHU
   och shunt på var sin port utan konflikt.
2. **Slå ihop:** konsolidera AHU-polling och shuntstyrning till en enda
   bussägande process. Renast på lång sikt, men en större förändring.

`shunt-control-integration`-skillet kontrollerar om `openaut-modbus` är aktiv och
stoppar med en fråga innan den startar `openaut-shunt` på samma port.

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

1. Kopiera och fyll i config:
   ```bash
   cp config/example-shunt-config.json config/shunt-config.json
   # Fyll i: slave-ID, registeradresser (från Siemens datablad A6V13841491),
   # ventilskalning, kurva och pumpgräns.
   nano config/shunt-config.json
   ```
   > EM1.8 registernumrering i databladet är **1-baserad**; pymodbus är 0-baserad
   > — subtrahera 1.

2. Säg till agenten:
   ```
   Driftsätt shuntregleringen.
   Konfig: config/shunt-config.json
   ```
   OpenClaw kör `shunt-control-integration`:
   kontrollerar bussamexistens → verifierar SSH → kör `setup.sh` → skannar
   EM1.8-modulerna → kopierar `shunt_control.py` + config → startar systemd-service
   → verifierar MQTT-telemetri.

3. Skapa databastabellen (en gång):
   ```bash
   docker compose exec timescaledb psql -U openaut -d openaut < db/shunt_init.sql
   ```

4. Lägg till Telegraf-konsumenten (`telegraf/shunt-mqtt.conf`) i POC1:s
   Telegraf-config så att `openaut/<site>/shunt/#` ingestas.

---

## Dataflöde

```
EM1.8U/R/D (Modbus RS485)
        ↓  /dev/ttyS2 (eller /dev/ttyUSB0)
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
db/shunt_init.sql                           TimescaleDB-tabell
telegraf/shunt-mqtt.conf                    Telegraf MQTT → TimescaleDB
AGENTS.md                                   Instruktioner för OpenClaw-agenten
```

---

## Verifiering

```bash
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
