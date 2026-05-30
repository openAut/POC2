# openAut POC2 — Agent Instructions

Du arbetar med **openAut POC2**: reglering av en värme-shuntgrupp från samma
Siemens IOT2050 som POC1 använder för ventilationsaggregaten. I/O sker via
Siemens EM1.8 (Desigo Essentials) Modbus-moduler på RS485.

## Systembeskrivning

| Enhet | Roll | IP (standard) |
|-------|------|---------------|
| Mini-PC | OpenClaw, TimescaleDB, EMQX, Telegraf, Grafana (delas med POC1) | 192.168.10.10 |
| Asus GX10 | Lokal LLM (Nemotron) | 192.168.10.20 |
| Siemens IOT2050 | Edge-nod — Modbus RTU + shuntreglering | 192.168.10.50 |

## I/O — Siemens EM1.8-moduler

| Signal | Riktning | Modul |
|--------|----------|-------|
| Framledningstemp | resistiv in | EM1.8U |
| Returtemp | resistiv in | EM1.8U |
| Utetemp | resistiv in | EM1.8U |
| Ventilställdon | analog ut | EM1.8U |
| Pumpstart | reläut | EM1.8R |
| Pumplarm | digital in | EM1.8D |
| Pumpdrift | digital in | EM1.8D |

## Tillgängliga skills

### `shunt-control-integration`
Driftsätter shuntregleringen på IOT2050 via SSH. Kör vid "driftsätt
shuntreglering", "sätt upp shuntgruppen", "deploy shunt control".

**Kräver:** ifylld config (baserad på `config/example-shunt-config.json`).

## Viktiga begränsningar

- IOT2050 X30 = `/dev/ttyS2`. En seriell port kan **inte** delas av två
  processer. Kontrollera om POC1:s `openaut-modbus` redan kör på porten innan
  `openaut-shunt` startas — se README "RS485-samexistens".
- EM1.8 registernumrering i databladet är 1-baserad; pymodbus är 0-baserad.
- Fail-safe = håll senaste läge (ventil + pump fryses vid fel).
- Reglerloopen är överordnad/supervisory — fältets interlocks och frysskydd har
  alltid prioritet.

## Reglering

- 6-punkts utomhuskompenserad värmekurva: utetemp → framledningsbörvärde,
  linjär interpolation.
- PI-regulator driver ventilläget mot börvärdet (`control.kp`, `control.ki`).
- Pump startar när utetemp < `pump.start_below_c`, stoppar över gräns +
  `pump.hysteresis_c`.

## Felsökning

```bash
# Kör shuntregleringen?
ssh openaut@192.168.10.50 "sudo systemctl status openaut-shunt"

# Loggar
ssh openaut@192.168.10.50 "sudo journalctl -u openaut-shunt -n 30 --no-pager"

# MQTT-telemetri
mosquitto_sub -h 192.168.10.10 -t "openaut/poc2/shunt/#" -v

# Krockar med POC1 på bussen?
ssh openaut@192.168.10.50 "systemctl is-active openaut-modbus"
```
