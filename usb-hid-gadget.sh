#!/bin/bash
# Sets up a USB HID (boot-protocol keyboard) gadget via configfs/libcomposite
# and binds it to whatever UDC (USB Device Controller) is available.
set -euo pipefail

GADGET_NAME=g1
CONFIGFS=/sys/kernel/config/usb_gadget
GADGET_DIR="$CONFIGFS/$GADGET_NAME"

modprobe libcomposite

# If the gadget already exists (e.g. service restarted), tear it down first
# so we don't fail on "File exists".
if [ -d "$GADGET_DIR" ]; then
    if [ -f "$GADGET_DIR/UDC" ] && [ -s "$GADGET_DIR/UDC" ]; then
        echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    fi
    rm -rf "$GADGET_DIR/configs/c.1/hid.usb0" 2>/dev/null || true
    rm -rf "$GADGET_DIR/configs/c.1/hid.usb1" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR/configs/c.1" 2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.usb1" 2>/dev/null || true
    rmdir "$GADGET_DIR/functions/hid.usb0" 2>/dev/null || true
    rmdir "$GADGET_DIR/strings/0x409" 2>/dev/null || true
    rmdir "$GADGET_DIR" 2>/dev/null || true
fi

mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x1d6b > idVendor    # Linux Foundation
echo 0x0104 > idProduct   # Multifunction Composite Gadget
echo 0x0400 > bcdDevice   # bumped when the mouse was added: Windows caches
                          # descriptors per VID/PID/revision and will not re-read
                          # a changed composite device unless this changes
echo 0x0200 > bcdUSB      # USB2

mkdir -p strings/0x409
echo "0001"                    > strings/0x409/serialnumber
echo "max"                     > strings/0x409/manufacturer
echo "Pi Macro Keyboard"       > strings/0x409/product

mkdir -p configs/c.1/strings/0x409
echo "HID keyboard config" > configs/c.1/strings/0x409/configuration
echo 250                   > configs/c.1/MaxPower

mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol      # 1 = keyboard
echo 0 > functions/hid.usb0/subclass      # 0 = no boot subclass advertised beyond protocol
echo 8 > functions/hid.usb0/report_length

# Standard USB HID boot-keyboard report descriptor: 8-byte reports
# (1 modifier byte, 1 reserved byte, 6 keycode bytes).
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
    > functions/hid.usb0/report_desc

ln -sf "$GADGET_DIR/functions/hid.usb0" "$GADGET_DIR/configs/c.1/hid.usb0"

# --- mouse (standard relative), reported as 4 bytes: ------------------------
# [buttons, dx, dy, wheel]  - each delta is a signed byte, -127..127.
# Windows rejected an absolute-pointer descriptor here, so this is the plain
# relative mouse every host supports; absolute moves are built from deltas.
mkdir -p functions/hid.usb1
echo 2 > functions/hid.usb1/protocol      # 2 = mouse
echo 1 > functions/hid.usb1/subclass      # 1 = boot interface
echo 4 > functions/hid.usb1/report_length
echo 1 > functions/hid.usb1/no_out_endpoint

printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' \
    > functions/hid.usb1/report_desc

ln -sf "$GADGET_DIR/functions/hid.usb1" "$GADGET_DIR/configs/c.1/hid.usb1"


# Bind to the first available UDC (USB device controller)
UDC=$(ls /sys/class/udc | head -n1)
if [ -z "$UDC" ]; then
    echo "usb-hid-gadget: no UDC found under /sys/class/udc" >&2
    exit 1
fi
echo "$UDC" > UDC

# Wait for the device node to appear, then relax perms via udev (see
# /etc/udev/rules.d/99-hidg.rules); this is just a sanity wait.
for _ in $(seq 1 20); do
    [ -e /dev/hidg0 ] && [ -e /dev/hidg1 ] && break
    sleep 0.1
done

for node in /dev/hidg0 /dev/hidg1; do
    if [ ! -e "$node" ]; then
        echo "usb-hid-gadget: $node did not appear after binding UDC" >&2
        exit 1
    fi
done

echo "usb-hid-gadget: bound to $UDC - /dev/hidg0 keyboard, /dev/hidg1 mouse"
