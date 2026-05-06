import time
import random
import string
import glob
import textwrap

from serial import Serial
from serial.tools import list_ports

DEFAULT_BAUD = 9600
PRINTER_NAME = "CONFIDEX"
EMAIL = "confidex@gmail.com"
WEBSITE = "https://irretraceably-chirographical-shayne.ngrok-free.dev"

PREFERRED_PRINTER_PORT = None

# =========================
# EM5820 / 5822-2007 tuning
# =========================
PRINTER_LINE_WIDTH = 32

# ESC 7 n1 n2 n3
# n1 = max heating dots (0-255, practical 7..11 for small mechanisms)
# n2 = heating time
# n3 = heating interval
#
# For weak / gray prints, n2 is the most important.
# These values are intentionally stronger than default.
HEAT_DOTS = 10
HEAT_TIME = 0xC8   # 200
HEAT_INTERVAL = 0x02

# Some clones respond to DC2 # n for density. Not all do.
# Safe to try; ignored by unsupported units.
PRINT_DENSITY = 15   # 0..15
PRINT_BREAK_TIME = 7 # 0..7

# QR tuning
QR_MODULE_SIZE = 7
# ESC/POS QR error correction:
# 48=L, 49=M, 50=Q, 51=H
QR_EC_LEVEL = 51  # H = highest recovery


def generate_token(user_id=None, length=12):
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(random.choices(chars, k=length))
    if user_id:
        return f"{str(user_id)[:6]}-{random_part}"
    return random_part


def _safe_text(value):
    if value is None:
        return ''
    return str(value)


def _peso(value):
    try:
        return f"PHP {float(value):.2f}"
    except Exception:
        return f"PHP {_safe_text(value)}"


def _write(ser, cmd: bytes, delay=0.08):
    ser.write(cmd)
    ser.flush()
    time.sleep(delay)


def _feed(ser, lines=1, delay=0.08):
    if lines <= 0:
        return
    _write(ser, b"\n" * lines, delay)


def _center(ser):
    _write(ser, b'\x1B\x61\x01', 0.06)


def _left(ser):
    _write(ser, b'\x1B\x61\x00', 0.06)


def _right(ser):
    _write(ser, b'\x1B\x61\x02', 0.06)


def _big(ser, on=True):
    _write(ser, b'\x1B\x21' + (b'\x30' if on else b'\x00'), 0.06)


def _bold(ser, on=True):
    _write(ser, b'\x1B\x45' + (b'\x01' if on else b'\x00'), 0.06)


def _normal(ser):
    _write(ser, b'\x1B\x21\x00', 0.06)
    _bold(ser, False)


def _set_line_spacing_default(ser):
    _write(ser, b'\x1B\x32', 0.06)


def _set_line_spacing(ser, n=30):
    _write(ser, b'\x1B\x33' + bytes([max(0, min(255, int(n)))]), 0.06)


def _looks_like_arduino(port_info):
    text = " ".join([
        _safe_text(port_info.device),
        _safe_text(port_info.description),
        _safe_text(port_info.manufacturer),
        _safe_text(port_info.product),
        _safe_text(port_info.hwid),
    ]).lower()

    arduino_keywords = [
        "arduino",
        "uno",
        "mega",
        "wch",
        "ch340",
        "cp210",
        "acm",
    ]

    return any(word in text for word in arduino_keywords)


def _looks_like_printer(port_info):
    text = " ".join([
        _safe_text(port_info.device),
        _safe_text(port_info.description),
        _safe_text(port_info.manufacturer),
        _safe_text(port_info.product),
        _safe_text(port_info.hwid),
    ]).lower()

    printer_keywords = [
        "printer",
        "thermal",
        "pos",
        "receipt",
        "ttl",
        "usb serial",
        "usb-serial",
        "serial",
        "uart",
        "ch340",
        "cp210",
        "pl2303",
        "ftdi",
        "qinhen",
        "wch",
    ]

    return any(word in text for word in printer_keywords)


def list_serial_devices():
    devices = []
    for p in list_ports.comports():
        devices.append({
            "device": p.device,
            "description": p.description,
            "manufacturer": p.manufacturer,
            "product": p.product,
            "hwid": p.hwid,
            "vid": p.vid,
            "pid": p.pid,
        })
    return devices


def _find_printer_port():
    if PREFERRED_PRINTER_PORT:
        return PREFERRED_PRINTER_PORT

    by_id_ports = sorted(glob.glob("/dev/serial/by-id/*"))
    if by_id_ports:
        for stable_port in by_id_ports:
            lowered = stable_port.lower()
            if "arduino" not in lowered and "uno" not in lowered:
                return stable_port

    ports = list(list_ports.comports())

    for p in ports:
        if _looks_like_printer(p) and not _looks_like_arduino(p):
            return p.device

    for p in ports:
        dev = _safe_text(p.device)
        if dev.startswith("/dev/ttyUSB") and not _looks_like_arduino(p):
            return dev

    for p in ports:
        dev = _safe_text(p.device)
        if (dev.startswith("/dev/ttyAMA") or dev.startswith("/dev/ttyS")) and not _looks_like_arduino(p):
            return dev

    raise RuntimeError(
        "No thermal printer serial port found. "
        "Run list_serial_devices() and set PREFERRED_PRINTER_PORT to the correct device."
    )


def _open_printer_serial(com_port=None, baud=DEFAULT_BAUD):
    port = com_port or _find_printer_port()
    print(f"[PRINTER] Opening printer port {port} at {baud}", flush=True)

    ser = Serial(
        port=port,
        baudrate=baud,
        timeout=2,
        write_timeout=2
    )

    time.sleep(1.0)
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser


def _init_printer(ser):
    print("[PRINTER] Initializing printer with darker settings", flush=True)

    # Reset
    _write(ser, b'\x1B\x40', 0.20)

    # EM5820 stronger heating config
    _write(
        ser,
        b'\x1B\x37' + bytes([HEAT_DOTS, HEAT_TIME, HEAT_INTERVAL]),
        0.20
    )

    # Optional density command supported by many mini thermal clones:
    # DC2 # n  where n = (break_time << 5) | density
    density_byte = ((PRINT_BREAK_TIME & 0x07) << 5) | (PRINT_DENSITY & 0x0F)
    _write(ser, b'\x12\x23' + bytes([density_byte]), 0.20)

    _left(ser)
    _normal(ser)
    _set_line_spacing_default(ser)
    _feed(ser, 1, 0.10)
    time.sleep(0.30)


def _wrap_text(text, width=PRINTER_LINE_WIDTH):
    raw = _safe_text(text)
    if not raw:
        return [""]

    lines = []
    for part in raw.splitlines() or [""]:
        if not part.strip():
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            part,
            width=width,
            break_long_words=True,
            break_on_hyphens=False
        )
        lines.extend(wrapped if wrapped else [""])
    return lines


def _print_lines(ser, lines, align="left", delay=0.03):
    if align == "center":
        _center(ser)
    elif align == "right":
        _right(ser)
    else:
        _left(ser)

    for line in lines:
        _write(ser, (line + "\n").encode("ascii", errors="ignore"), delay)


def _print_wrapped_line(ser, text, align="left", delay=0.03):
    _print_lines(ser, _wrap_text(text), align=align, delay=delay)


def _print_separator(ser, char="-", count=PRINTER_LINE_WIDTH):
    _left(ser)
    _write(ser, (char * count + "\n").encode("ascii", errors="ignore"), 0.03)


def _print_qr(ser, data: str, module_size=QR_MODULE_SIZE, ec_level=QR_EC_LEVEL):
    qr_data = _safe_text(data).encode("ascii", errors="ignore")
    if not qr_data:
        raise ValueError("QR data is empty")

    module_size = max(4, min(16, int(module_size)))
    ec_level = int(ec_level)
    if ec_level not in (48, 49, 50, 51):
        ec_level = 51

    print(
        f"[PRINTER] Printing QR | bytes={len(qr_data)} | module_size={module_size} | ec={ec_level}",
        flush=True
    )

    _center(ser)

    # Model 2
    _write(ser, b'\x1D\x28\x6B\x04\x00\x31\x41\x32\x00', 0.20)

    # Module size
    _write(ser, b'\x1D\x28\x6B\x03\x00\x31\x43' + bytes([module_size]), 0.20)

    # Error correction
    _write(ser, b'\x1D\x28\x6B\x03\x00\x31\x45' + bytes([ec_level]), 0.20)

    # Store data
    total_len = len(qr_data) + 3
    pL = total_len & 0xFF
    pH = (total_len >> 8) & 0xFF
    _write(
        ser,
        b'\x1D\x28\x6B' + bytes([pL, pH]) + b'\x31\x50\x30' + qr_data,
        0.35
    )

    # Print
    _write(ser, b'\x1D\x28\x6B\x03\x00\x31\x51\x30', 1.50)
    _feed(ser, 1, 0.15)


def _finalize_print(ser):
    _normal(ser)
    _left(ser)
    _feed(ser, 4, 0.20)
    ser.flush()
    time.sleep(2.0)


def print_discount_qr(token: str, com_port=None, baud=DEFAULT_BAUD):
    """
    Prints only the discount QR coupon.
    No purchase details are printed.
    """
    ser = None
    try:
        ser = _open_printer_serial(com_port=com_port, baud=baud)
        _init_printer(ser)

        print('[PRINTER] Printing QR-only coupon header', flush=True)

        _center(ser)
        _big(ser, True)
        _bold(ser, True)
        _print_wrapped_line(ser, PRINTER_NAME, align="center", delay=0.08)

        _normal(ser)
        _print_wrapped_line(ser, EMAIL, align="center", delay=0.08)
        _feed(ser, 1, 0.08)

        _bold(ser, True)
        _print_wrapped_line(ser, "DISCOUNT COUPON", align="center", delay=0.08)
        _normal(ser)
        _print_wrapped_line(ser, "Scan this QR on your next use", align="center", delay=0.08)
        _feed(ser, 1, 0.08)

        print('[PRINTER] Printing QR-only coupon QR', flush=True)
        _print_qr(ser, token, module_size=QR_MODULE_SIZE, ec_level=QR_EC_LEVEL)

        print('[PRINTER] Printing QR-only coupon footer', flush=True)
        _print_wrapped_line(ser, "This code is one-time use only", align="left", delay=0.08)
        _print_wrapped_line(ser, "and will expire after 3 months.", align="left", delay=0.08)
        _print_wrapped_line(ser, "Keep this paper for your next purchase.", align="left", delay=0.08)
        _feed(ser, 1, 0.08)
        _print_wrapped_line(ser, f"Visit: {WEBSITE}", align="left", delay=0.08)

        _finalize_print(ser)

        print('[PRINTER] QR-only coupon printed successfully', flush=True)
        return True

    except Exception as e:
        print(f'[PRINTER] Printer error in print_discount_qr: {e}', flush=True)
        return False

    finally:
        if ser and ser.is_open:
            ser.close()

def debug_list_serial_devices():
    print("[SERIAL] Available serial devices:", flush=True)
    for dev in list_serial_devices():
        print(
            f"  device={dev['device']} | "
            f"description={dev['description']} | "
            f"manufacturer={dev['manufacturer']} | "
            f"product={dev['product']} | "
            f"hwid={dev['hwid']}",
            flush=True
        )