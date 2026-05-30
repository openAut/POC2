#!/bin/bash
# openAut POC2 — Edge node setup for shunt control on Siemens IOT2050
# Run as root: sudo bash setup.sh
#
# PREREQUISITES (same edge node as POC1):
#   1. IOT2050 X30 already in RS485 mode (configured in POC1 via iot2050setup).
#   2. User 'openaut' exists and is in 'dialout' group (from POC1 setup).
#   3. Siemens EM1.8U/R/D modules wired to the RS485 bus with unique slave IDs.
#
# NOTE: If POC1's openaut-modbus.service already polls /dev/ttyS2, a second
#       process cannot share the same serial port. See README "RS485-samexistens".

set -e

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${BLUE}[openAut]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

log "POC2 shunt-control edge setup"

[ "$EUID" -eq 0 ] || err "Run as root: sudo bash setup.sh"

MODEL=$(cat /proc/device-tree/model 2>/dev/null || echo "unknown")
log "Device: $MODEL"

# --- Serial port present? ---
if [ ! -c /dev/ttyS2 ]; then
    err "/dev/ttyS2 not found. Configure RS485 mode (see POC1 iot2050-edge-setup)."
fi
ok "/dev/ttyS2 present"

# --- openaut user / dialout ---
if id openaut &>/dev/null; then
    ok "User 'openaut' exists"
else
    log "Creating user 'openaut'..."
    useradd -m -s /bin/bash openaut
    ok "User created"
fi
if groups openaut | grep -q dialout; then
    ok "openaut in dialout group"
else
    usermod -aG dialout openaut
    ok "Added openaut to dialout"
fi

# --- Dependencies (idempotent; harmless if POC1 already installed them) ---
log "Installing Python packages (pymodbus, paho-mqtt)..."
apt-get update -q
apt-get install -y -q python3 python3-pip python3-serial mosquitto-clients netcat-openbsd
pip3 install --break-system-packages --quiet pymodbus==3.7.4
pip3 install --break-system-packages --quiet paho-mqtt==2.1.0
ok "Dependencies installed"

# --- Deploy dir ---
log "Creating /opt/openaut/shunt..."
mkdir -p /opt/openaut/shunt
chown openaut:openaut /opt/openaut/shunt
ok "Deploy dir ready"

# --- Verify imports ---
python3 -c "from pymodbus.client import ModbusSerialClient; print('  pymodbus OK')" || err "pymodbus import failed"
python3 -c "import paho.mqtt.client; print('  paho-mqtt OK')" || err "paho-mqtt import failed"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}[openAut] POC2 setup complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Next steps:"
echo "  1. scp edge/shunt_control.py openaut@<IP>:/opt/openaut/shunt/"
echo "  2. scp config/<your>-shunt-config.json openaut@<IP>:/opt/openaut/shunt/config.json"
echo "  3. scp edge/openaut-shunt.service openaut@<IP>:/tmp/ && \\"
echo "     ssh openaut@<IP> 'sudo cp /tmp/openaut-shunt.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now openaut-shunt'"
echo "  4. ssh openaut@<IP> 'sudo journalctl -u openaut-shunt -f'"
echo ""
