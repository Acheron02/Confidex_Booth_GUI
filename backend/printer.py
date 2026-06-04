import os
import time
import random
import string
import glob
import textwrap

from serial import Serial
from serial.tools import list_ports


def _env_int(name, default, min_value=None, max_value=None):
    raw = os.getenv(name, "").strip()

    try:
        value = int(raw) if raw else int(default)
    except Exception:
        value = int(default)

    if min_value is not None:
        value = max(int(min_value), value)

    if max_value is not None:
        value = min(int(max_value), value)

    return value


DEFAULT_BAUD = _env_int("PRINTER_BAUD", 9600, 1200, 115200)

PRINTER_NAME = os.getenv("PRINTER_NAME", "CONFIDEX").strip() or "CONFIDEX"
EMAIL = os.getenv("PRINTER_EMAIL", "confidex@gmail.com").strip() or "confidex@gmail.com"

WEBSITE = (
    os.getenv("WEBSITE_BASE_URL", "").strip()
    or os.getenv("PRINTER_WEBSITE", "").strip()
    or "https://irretraceably-chirographical-shayne.ngrok-free.dev"
)

# Recommended in .env.local:
# THERMAL_PRINTER_PORT=/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
PREFERRED_PRINTER_PORT = os.getenv("THERMAL_PRINTER_PORT", "").strip() or None

PRINTER_LINE_WIDTH = _env_int("PRINTER_LINE_WIDTH", 32, 24, 48)

# ESC 7 n1 n2 n3
HEAT_DOTS = _env_int("PRINTER_HEAT_DOTS", 10, 1, 255)
HEAT_TIME = _env_int("PRINTER_HEAT_TIME", 0xC8, 1, 255)
HEAT_INTERVAL = _env_int("PRINTER_HEAT_INTERVAL", 0x02, 0, 255)

# DC2 # n density command for many mini thermal printers.
PRINT_DENSITY = _env_int("PRINTER_DENSITY", 15, 0, 15)
PRINT_BREAK_TIME = _env_int("PRINTER_BREAK_TIME", 7, 0, 7)

QR_MODULE_SIZE = _env_int("PRINTER_QR_MODULE_SIZE", 10, 4, 18)

# ESC/POS QR error correction:
# 48=L, 49=M, 50=Q, 51=H
QR_EC_LEVEL = _env_int("PRINTER_QR_EC_LEVEL", 51, 48, 51)
if QR_EC_LEVEL not in (48, 49, 50, 51):
    QR_EC_LEVEL = 51

TRAILING_BLANK_LINES = _env_int(
    "PRINTER_TRAILING_BLANK_LINES",
    80,
    0,
    200,
)

PLACEHOLDER_QR_MESSAGE = (
    os.getenv("PRINTER_PLACEHOLDER_QR_MESSAGE", "").strip()
    or "request a qr code on the website"
)


def generate_token(user_id=None, length=12):
    chars = string.ascii_uppercase + string.digits
    random_part = "".join(random.choices(chars, k=length))

    if user_id:
        return f"{str(user_id)[:6]}-{random_part}"

    return random_part


def _safe_text(value):
    if value is None:
        return ""

    return str(value)


def _write(ser, cmd: bytes, delay=0.08):
    ser.write(cmd)
    ser.flush()
    time.sleep(delay)


def _feed(ser, lines=1, delay=0.08):
    lines = int(lines or 0)

    if lines <= 0:
        return

    chunk_size = 8
    remaining = lines

    while remaining > 0:
        chunk = min(chunk_size, remaining)
        _write(ser, b"\n" * chunk, delay)
        remaining -= chunk


def _center(ser):
    _write(ser, b"\x1B\x61\x01", 0.06)


def _left(ser):
    _write(ser, b"\x1B\x61\x00", 0.06)


def _right(ser):
    _write(ser, b"\x1B\x61\x02", 0.06)


def _big(ser, on=True):
    _write(ser, b"\x1B\x21" + (b"\x30" if on else b"\x00"), 0.06)


def _bold(ser, on=True):
    _write(ser, b"\x1B\x45" + (b"\x01" if on else b"\x00"), 0.06)


def _normal(ser):
    _write(ser, b"\x1B\x21\x00", 0.06)
    _bold(ser, False)


def _set_line_spacing_default(ser):
    _write(ser, b"\x1B\x32", 0.06)


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
        "genuino",
        "ttyacm",
        "2341",
        "2a03",
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
        "cp210",
        "cp2102",
        "pl2303",
        "ftdi",
        "ch340",
        "ch341",
        "qinhen",
        "wch",
        "silicon labs",
        "silicon_labs",
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
        if os.path.exists(PREFERRED_PRINTER_PORT):
            return PREFERRED_PRINTER_PORT

        raise RuntimeError(
            f"THERMAL_PRINTER_PORT is set but does not exist: {PREFERRED_PRINTER_PORT}"
        )

    by_id_ports = sorted(glob.glob("/dev/serial/by-id/*"))

    if by_id_ports:
        for stable_port in by_id_ports:
            lowered = stable_port.lower()

            if "arduino" in lowered or "genuino" in lowered or "uno" in lowered:
                continue

            if (
                "cp210" in lowered
                or "silicon" in lowered
                or "ch340" in lowered
                or "usb_to_uart" in lowered
                or "uart" in lowered
                or "serial" in lowered
            ):
                return stable_port

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

        if (
            dev.startswith("/dev/ttyAMA")
            or dev.startswith("/dev/ttyS")
        ) and not _looks_like_arduino(p):
            return dev

    raise RuntimeError(
        "No thermal printer serial port found. "
        "Run debug_list_serial_devices() and set THERMAL_PRINTER_PORT in .env.local."
    )


def _open_printer_serial(com_port=None, baud=DEFAULT_BAUD):
    port = com_port or _find_printer_port()

    print(f"[PRINTER] Opening printer port {port} at {baud}", flush=True)

    ser = Serial(
        port=port,
        baudrate=baud,
        timeout=2,
        write_timeout=2,
    )

    time.sleep(1.0)

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass

    return ser


def _init_printer(ser):
    print("[PRINTER] Initializing printer with darker settings", flush=True)

    # Reset printer.
    _write(ser, b"\x1B\x40", 0.20)

    # EM5820 / 5822-2007 stronger heating config.
    _write(
        ser,
        b"\x1B\x37" + bytes([HEAT_DOTS, HEAT_TIME, HEAT_INTERVAL]),
        0.20,
    )

    # Optional density command supported by many mini thermal clones:
    # DC2 # n where n = (break_time << 5) | density
    density_byte = ((PRINT_BREAK_TIME & 0x07) << 5) | (PRINT_DENSITY & 0x0F)
    _write(ser, b"\x12\x23" + bytes([density_byte]), 0.20)

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
            break_on_hyphens=False,
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
        _write(
            ser,
            (line + "\n").encode("ascii", errors="ignore"),
            delay,
        )


def _print_wrapped_line(ser, text, align="left", delay=0.03):
    _print_lines(
        ser,
        _wrap_text(text),
        align=align,
        delay=delay,
    )


def _print_separator(ser, char="-", count=PRINTER_LINE_WIDTH):
    _left(ser)
    _write(
        ser,
        (char * count + "\n").encode("ascii", errors="ignore"),
        0.03,
    )


def _print_boxed_message(ser, message=PLACEHOLDER_QR_MESSAGE):
    box_width = min(PRINTER_LINE_WIDTH, 32)
    inner_width = box_width - 4

    wrapped = textwrap.wrap(
        _safe_text(message).strip() or "request a qr code on the website",
        width=inner_width,
        break_long_words=False,
        break_on_hyphens=False,
    )

    if not wrapped:
        wrapped = ["request a qr code on the website"]

    _center(ser)
    _feed(ser, 1, 0.08)

    _bold(ser, True)

    top_border = "+" + ("-" * (box_width - 2)) + "+"
    empty_line = "|" + (" " * (box_width - 2)) + "|"

    _write(ser, (top_border + "\n").encode("ascii", errors="ignore"), 0.08)

    for _ in range(2):
        _write(ser, (empty_line + "\n").encode("ascii", errors="ignore"), 0.08)

    for line in wrapped:
        line = line[:inner_width]
        left_pad = max(0, (inner_width - len(line)) // 2)
        right_pad = max(0, inner_width - len(line) - left_pad)
        boxed_line = "| " + (" " * left_pad) + line + (" " * right_pad) + " |"
        _write(ser, (boxed_line + "\n").encode("ascii", errors="ignore"), 0.08)

    for _ in range(2):
        _write(ser, (empty_line + "\n").encode("ascii", errors="ignore"), 0.08)

    _write(ser, (top_border + "\n").encode("ascii", errors="ignore"), 0.08)

    _bold(ser, False)
    _feed(ser, 3, 0.10)


def _print_qr(
    ser,
    data: str,
    module_size=QR_MODULE_SIZE,
    ec_level=QR_EC_LEVEL,
):
    qr_data = _safe_text(data).strip().encode(
        "ascii",
        errors="ignore",
    )

    if not qr_data:
        raise ValueError("QR data empty")

    module_size = max(4, min(18, int(module_size)))

    ec_level = int(ec_level)
    if ec_level not in (48, 49, 50, 51):
        ec_level = 51

    print(
        f"[PRINTER] Printing QR bytes={len(qr_data)} size={module_size}",
        flush=True,
    )

    _center(ser)

    # QR Model 2
    _write(
        ser,
        b"\x1D\x28\x6B\x04\x00\x31\x41\x32\x00",
        0.2,
    )

    # Module size
    _write(
        ser,
        b"\x1D\x28\x6B\x03\x00\x31\x43" + bytes([module_size]),
        0.2,
    )

    # Error correction
    _write(
        ser,
        b"\x1D\x28\x6B\x03\x00\x31\x45" + bytes([ec_level]),
        0.2,
    )

    # Store QR data
    total_len = len(qr_data) + 3
    pL = total_len & 0xFF
    pH = (total_len >> 8) & 0xFF

    _write(
        ser,
        b"\x1D\x28\x6B"
        + bytes([pL, pH])
        + b"\x31\x50\x30"
        + qr_data,
        0.35,
    )

    # Print QR
    _write(
        ser,
        b"\x1D\x28\x6B\x03\x00\x31\x51\x30",
        1.5,
    )

    _feed(ser, 4, 0.12)


def _finalize_print(ser):
    _normal(ser)
    _left(ser)

    print(
        f"[PRINTER] Feeding {TRAILING_BLANK_LINES} blank lines",
        flush=True,
    )

    _set_line_spacing_default(ser)
    _feed(ser, TRAILING_BLANK_LINES, 0.12)
    _set_line_spacing_default(ser)

    ser.flush()
    time.sleep(2.0)


def print_discount_qr(token: str = None, com_port=None, baud=DEFAULT_BAUD):
    """
    Prints the discount coupon.

    If token exists:
        Prints the real QR code.

    If token is missing:
        Still prints the coupon, but replaces the QR area with a box saying:
        "request a qr code on the website"
    """

    ser = None
    token_text = _safe_text(token).strip()
    has_qr_token = bool(token_text)

    try:
        ser = _open_printer_serial(com_port=com_port, baud=baud)
        _init_printer(ser)

        print("[PRINTER] Printing discount coupon header", flush=True)

        _center(ser)
        _big(ser, True)
        _bold(ser, True)
        _print_wrapped_line(
            ser,
            PRINTER_NAME,
            align="center",
            delay=0.08,
        )

        _normal(ser)
        _print_wrapped_line(
            ser,
            EMAIL,
            align="center",
            delay=0.08,
        )

        _feed(ser, 1, 0.08)

        _bold(ser, True)
        _print_wrapped_line(
            ser,
            "DISCOUNT COUPON",
            align="center",
            delay=0.08,
        )

        _normal(ser)

        if has_qr_token:
            _print_wrapped_line(
                ser,
                "Scan this QR on your next use",
                align="center",
                delay=0.08,
            )
        else:
            _print_wrapped_line(
                ser,
                "QR code not available",
                align="center",
                delay=0.08,
            )

        _feed(ser, 1, 0.08)

        if has_qr_token:
            print("[PRINTER] Printing discount coupon QR", flush=True)
            _print_qr(
                ser,
                token_text,
                module_size=QR_MODULE_SIZE,
                ec_level=QR_EC_LEVEL,
            )
        else:
            print("[PRINTER] Printing placeholder QR message box", flush=True)
            _print_boxed_message(
                ser,
                PLACEHOLDER_QR_MESSAGE,
            )

        print("[PRINTER] Printing discount coupon footer", flush=True)

        if has_qr_token:
            _print_wrapped_line(
                ser,
                "This code is one-time use only",
                align="left",
                delay=0.08,
            )
            _print_wrapped_line(
                ser,
                "and will expire after 3 months.",
                align="left",
                delay=0.08,
            )
            _print_wrapped_line(
                ser,
                "Keep this paper for your next purchase.",
                align="left",
                delay=0.08,
            )
        else:
            _print_wrapped_line(
                ser,
                "No local discount QR was generated",
                align="left",
                delay=0.08,
            )
            _print_wrapped_line(
                ser,
                "for this printed coupon.",
                align="left",
                delay=0.08,
            )
            _print_wrapped_line(
                ser,
                "Open the website to request",
                align="left",
                delay=0.08,
            )
            _print_wrapped_line(
                ser,
                "a discount QR code.",
                align="left",
                delay=0.08,
            )

        _feed(ser, 1, 0.08)

        _print_wrapped_line(
            ser,
            f"Visit: {WEBSITE}",
            align="left",
            delay=0.08,
        )

        _finalize_print(ser)

        if has_qr_token:
            print("[PRINTER] Discount coupon with QR printed successfully", flush=True)
        else:
            print("[PRINTER] Discount coupon placeholder printed successfully", flush=True)

        return True

    except Exception as e:
        print(f"[PRINTER] Printer error in print_discount_qr: {e}", flush=True)
        return False

    finally:
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass


def debug_list_serial_devices():
    print("[SERIAL] Available serial devices:", flush=True)

    for dev in list_serial_devices():
        print(
            f"  device={dev['device']} | "
            f"description={dev['description']} | "
            f"manufacturer={dev['manufacturer']} | "
            f"product={dev['product']} | "
            f"hwid={dev['hwid']}",
            flush=True,
        )


def debug_print_test_coupon():
    token = generate_token("DEBUG")
    return print_discount_qr(token)


def debug_print_placeholder_coupon():
    return print_discount_qr(None)