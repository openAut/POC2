#!/bin/bash
# openAut POC2 — Edge node setup for shunt control on Siemens IOT2050
# Run as root: sudo bash setup.sh
#
# PREREQUISITES (same edge node as POC1):
#   1. Moxa UPort 1150 USB-RS485 adapter connected.
#   2. Moxa's mxu11x0 driver installed (see README "Moxa UPort 1150 — RS-485 2-wire").
#      The mainline mxuport driver cannot reliably switch the electrical interface.
#   3. User 'openaut' exists and is in 'dialout' group (from POC1 setup).
#   4. Siemens EM1.8U/R/D modules wired to the Moxa RS485 bus with unique slave IDs.
#
# POC2 uses a DEDICATED bus via the Moxa adapter (/dev/openaut-shunt), separate
# from POC1's /dev/ttyS2 — no RS485 contention with openaut-modbus.

set -e

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${BLUE}[openAut]${NC} $1"; }
ok()   { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

log "POC2 shunt-control edge setup (Moxa UPort 1150 bus)"

[ "$EUID" -eq 0 ] || err "Run as root: sudo bash setup.sh"

MODEL=$(cat /proc/device-tree/model 2>/dev/null || echo "unknown")
log "Device: $MODEL"

# --- Moxa adapter present? (USB 110a:1150) ---
if lsusb 2>/dev/null | grep -qi "110a:1150\|MOXA"; then
    ok "Moxa UPort 1150 detected on USB"
else
    warn "Moxa UPort 1150 not detected (lsusb). Check the USB cable/adapter."
fi

# --- Driver check: prefer Moxa's mxu11x0 over mainline mxuport ---
if lsmod | grep -q "^mxu11x0"; then
    ok "Moxa mxu11x0 driver loaded"
elif lsmod | grep -q "^mxuport"; then
    warn "Mainline 'mxuport' driver is loaded — it cannot reliably switch RS-485 2-wire."
    warn "Install Moxa's mxu11x0 driver (see README) and blacklist mxuport."
else
    warn "No Moxa serial driver detected yet. Install mxu11x0 (see README) before relying on RS-485 mode."
fi

# --- Ensure setserial present (needed to set RS-485 interface mode) ---
if ! command -v setserial &>/dev/null; then
    log "Installing setserial..."
    apt-get update -q
    apt-get install -y -q setserial
fi
ok "setserial available"

# --- Install udev rule for stable name, if provided alongside this script ---
RULE_SRC="$(dirname "$0")/99-openaut-moxa.rules"
if [ -f "$RULE_SRC" ]; then
    if grep -q "MOXA_SERIAL_HERE" "$RULE_SRC"; then
        warn "udev rule still has placeholder serial — fill in ATTRS{serial} before relying on /dev/openaut-shunt"
    fi
    cp "$RULE_SRC" /etc/udev/rules.d/99-openaut-moxa.rules
    udevadm control --reload-rules && udevadm trigger || true
    ok "udev rule installed (99-openaut-moxa.rules)"
else
    warn "99-openaut-moxa.rules not found next to setup.sh — install it manually for a stable /dev/openaut-shunt"
fi

# --- Stable symlink check + set RS-485 2-wire mode ---
if [ -e /dev/openaut-shunt ]; then
    ok "/dev/openaut-shunt present ($(readlink -f /dev/openaut-shunt))"
    if setserial /dev/openaut-shunt port 1 2>/dev/null; then
        ok "RS-485 2-wire mode set (setserial port 1)"
        log "Current mode: $(setserial -G /dev/openaut-shunt 2>/dev/null)"
    else
        warn "Could not set RS-485 mode via setserial — confirm mxu11x0 driver is loaded."
    fi
else
    warn "/dev/openaut-shunt not present yet — verify the udev serial, or temporarily use /dev/ttyUSB0"
    warn "Then set mode manually: sudo setserial /dev/ttyUSB0 port 1   # RS-485 2-wire"
fi

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
apt-get install -y -q python3 python3-pip python3-serial mosquitto-clients netcat-openbsd usbutils
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
echo "  1. Confirm RS-485 2-wire:  setserial -G /dev/openaut-shunt   # expect 'port 1'"
echo "  2. scp edge/shunt_control.py openaut@<IP>:/opt/openaut/shunt/"
echo "  3. scp config/<your>-shunt-config.json openaut@<IP>:/opt/openaut/shunt/config.json"
echo "  4. scp edge/openaut-shunt.service openaut@<IP>:/tmp/ && \\"
echo "     ssh openaut@<IP> 'sudo cp /tmp/openaut-shunt.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now openaut-shunt'"
echo "  5. ssh openaut@<IP> 'sudo journalctl -u openaut-shunt -f'"
echo ""
