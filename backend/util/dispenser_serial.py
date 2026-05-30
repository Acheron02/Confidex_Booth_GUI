import glob
import os
import threading
import time

try:
    import serial
except Exception as exc:
    serial = None
    _SERIAL_IMPORT_ERROR = exc
else:
    _SERIAL_IMPORT_ERROR = None


BAUD_RATE = int(os.getenv("ARDUINO_BAUD", "9600"))
SERIAL_TIMEOUT = 30
ARDUINO_BOOT_WAIT_SECONDS = 3.0

HOME_TIMEOUT_SECONDS = 130
DISPENSE_TIMEOUT_SECONDS = 75
RETURN_HOME_TIMEOUT_SECONDS = 90
DISPOSE_TIMEOUT_SECONDS = 90
CHANGE_TIMEOUT_SECONDS = 90

_SERIAL_LOCK = threading.RLock()
_SERIAL_CONN = None
_SERIAL_PORT = None

_KITS_HOMED_EVENT = threading.Event()
_HOMING_IN_PROGRESS = False
_HOMING_LOCK = threading.RLock()

_DISPOSAL_CANCEL_EVENT = threading.Event()


def request_disposal_cancel():
    """Ask an in-progress DISPOSE_KIT command to stop/defer.

    The serial reader checks this event while it is waiting for DISPOSED:.
    It writes STOP through the same open serial connection before releasing
    the shared lock, so the normal booth flow can continue sooner.
    """
    _DISPOSAL_CANCEL_EVENT.set()


def clear_disposal_cancel_request():
    _DISPOSAL_CANCEL_EVENT.clear()


def is_disposal_cancel_requested():
    return _DISPOSAL_CANCEL_EVENT.is_set()


FINAL_PREFIXES = (
    "DISPENSED:",
    "CHANGE_DISPENSED:",
    "DISPOSED:",
    "DISPOSE_STARTED:",
    "DISPOSE_RESUMED:",
    "DISPOSE_DEFERRED:",
    "KIT_HOME_DONE:",
    "KIT_RETURNED_HOME:",
    "KIT_STATUS:",
    "TRASH_STATUS:",
    "BILL_STATUS:",
)

FAILURE_PREFIXES = (
    "ERROR:",
    "ERR:",
    "DISPENSE_FAILED:",
    "DISPOSE_FAILED:",
    "KIT_HOME_FAILED:",
    "CHANGE_FAILED:",
)

FINAL_LINES = {
    "PONG",
    "READY",
    "OK",
    "BUSY",
    "BILL_STATUS:ON",
    "BILL_STATUS:OFF",
    "SERVOS_RESET",
    "KIT_SLOTS_RESET",
    "STOP_ALL",
}


# =====================================================
# BASIC HELPERS
# =====================================================

def _clean(value):
    return str(value or "").strip()


def _upper(value):
    return _clean(value).upper()


def _normalize_markers(markers):
    if not markers:
        return []

    return [_upper(item) for item in markers if _clean(item)]


def _is_failure_line(line):
    upper = _upper(line)

    if upper == "BUSY":
        return True

    return any(upper.startswith(prefix) for prefix in FAILURE_PREFIXES)


def _is_expected_line(line, wait_for_prefixes=None, wait_for_lines=None):
    upper = _upper(line)
    prefixes = _normalize_markers(wait_for_prefixes)
    lines = set(_normalize_markers(wait_for_lines))

    if lines and upper in lines:
        return True

    for prefix in prefixes:
        if upper.startswith(prefix):
            return True

    return False


def _is_generic_final_line(line):
    upper = _upper(line)

    if upper in FINAL_LINES:
        return True

    if _is_failure_line(upper):
        return True

    return any(upper.startswith(prefix) for prefix in FINAL_PREFIXES)


# =====================================================
# SERIAL PORT SELECTION
# =====================================================

def _candidate_ports():
    """
    Important:
    Do NOT auto-search /dev/serial/by-id/* here.

    Your thermal printer appeared as a CP2102 /dev/serial/by-id device,
    and Arduino commands were accidentally sent to the printer.

    Best:
      ARDUINO_PORT=/dev/ttyUSB0

    or:
      ARDUINO_PORT=/dev/ttyACM0
    """
    explicit = os.getenv("ARDUINO_PORT", "").strip()

    if explicit:
        return [explicit]

    ports = []

    preferred = [
        "/dev/ttyUSB0",
        "/dev/ttyACM10",
        "/dev/ttyACM1",
        "/dev/ttyACM0",
    ]

    for port in preferred:
        if os.path.exists(port):
            ports.append(port)

    ports.extend(sorted(glob.glob("/dev/ttyUSB*")))
    ports.extend(sorted(glob.glob("/dev/ttyACM*")))

    unique = []
    seen = set()

    for port in ports:
        if port and port not in seen:
            unique.append(port)
            seen.add(port)

    return unique


def _find_arduino_port():
    candidates = _candidate_ports()

    if not candidates:
        raise RuntimeError(
            "No Arduino serial device found. Set ARDUINO_PORT=/dev/ttyUSB0 "
            "or ARDUINO_PORT=/dev/ttyACM0 in .env.local."
        )

    return candidates[0]


def _close_serial_locked():
    global _SERIAL_CONN, _SERIAL_PORT

    try:
        if _SERIAL_CONN and _SERIAL_CONN.is_open:
            _SERIAL_CONN.close()
    except Exception:
        pass

    _SERIAL_CONN = None
    _SERIAL_PORT = None


def close_serial_connection():
    with _SERIAL_LOCK:
        _close_serial_locked()


def _ensure_serial_locked(force_reopen=False):
    global _SERIAL_CONN, _SERIAL_PORT

    if serial is None:
        raise RuntimeError(
            "pyserial is not installed or failed to import. "
            f"Install it with: pip install pyserial. Error={_SERIAL_IMPORT_ERROR}"
        )

    port = _find_arduino_port()

    if force_reopen:
        _close_serial_locked()

    if _SERIAL_CONN and _SERIAL_CONN.is_open and _SERIAL_PORT == port:
        return _SERIAL_CONN

    _close_serial_locked()

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD_RATE
    ser.timeout = 0.15
    ser.write_timeout = 2
    ser.rtscts = False
    ser.dsrdtr = False
    ser.open()

    time.sleep(ARDUINO_BOOT_WAIT_SECONDS)

    # Only clear immediately after opening the port.
    # Do NOT clear before every command, because that can chop delayed replies.
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception:
        pass

    _SERIAL_CONN = ser
    _SERIAL_PORT = port

    print(f"[SERIAL] Connected to Arduino on {port}", flush=True)
    return _SERIAL_CONN


# =====================================================
# SERIAL READ / WRITE
# =====================================================

def _drain_stale_serial_lines(ser, drain_seconds=0.30):
    """
    Drain boot leftovers such as READY before sending a command.

    This prevents:
      BILL_OFF -> reads stale READY -> fails
      next command -> reads chopped BILL_STATUS:OFF
    """
    drained = []
    deadline = time.time() + float(drain_seconds)

    while time.time() < deadline:
        raw = ser.readline()

        if not raw:
            continue

        line = raw.decode("utf-8", errors="ignore").strip()

        if line:
            drained.append(line)

    for line in drained:
        print(f"[SERIAL] Drained stale reply: {line}", flush=True)

    return drained


def _read_replies(
    ser,
    timeout,
    wait_for_prefixes=None,
    wait_for_lines=None,
    cancel_event=None,
    cancel_command=None,
):
    """
    Read Arduino replies.

    If caller waits for a specific line/prefix, stale READY and unrelated lines
    must not finish the read.
    """
    start = time.time()
    replies = []

    wait_for_prefixes = _normalize_markers(wait_for_prefixes)
    wait_for_lines = set(_normalize_markers(wait_for_lines))
    has_specific_target = bool(wait_for_prefixes or wait_for_lines)

    cancel_sent = False

    while time.time() - start < timeout:
        if cancel_event is not None and cancel_event.is_set() and not cancel_sent:
            cancel_sent = True
            cancel_line = "CANCEL_REQUESTED:DISPOSAL_DEFERRED"
            replies.append(cancel_line)
            print(f"[SERIAL] {cancel_line}", flush=True)

            if cancel_command:
                try:
                    if not str(cancel_command).endswith("\n"):
                        cancel_command = str(cancel_command) + "\n"
                    ser.write(str(cancel_command).encode("utf-8"))
                    ser.flush()
                    print(f"[SERIAL] Sent cancel: {str(cancel_command).strip()}", flush=True)
                except Exception as e:
                    print(f"[SERIAL] Cancel command failed: {e}", flush=True)

            cancel_deadline = time.time() + 2.5
            while time.time() < cancel_deadline:
                raw_cancel = ser.readline()
                if not raw_cancel:
                    continue
                cancel_reply = raw_cancel.decode("utf-8", errors="ignore").strip()
                if not cancel_reply:
                    continue
                replies.append(cancel_reply)
                print(f"[SERIAL] Reply after cancel: {cancel_reply}", flush=True)
                upper_cancel = _upper(cancel_reply)
                if upper_cancel == "STOP_ALL" or _is_failure_line(upper_cancel):
                    break

            break

        raw = ser.readline()

        if not raw:
            continue

        line = raw.decode("utf-8", errors="ignore").strip()

        if not line:
            continue

        replies.append(line)
        print(f"[SERIAL] Reply: {line}", flush=True)

        upper = _upper(line)

        if has_specific_target:
            # OK is only acknowledgement for long commands.
            if upper == "OK":
                continue

            # READY can be a stale boot line. Ignore unless caller specifically wants READY.
            if upper == "READY" and "READY" not in wait_for_lines:
                continue

            if _is_expected_line(
                upper,
                wait_for_prefixes=wait_for_prefixes,
                wait_for_lines=wait_for_lines,
            ):
                break

            if _is_failure_line(upper):
                break

            # Ignore unrelated lines while waiting for target.
            continue

        if _is_generic_final_line(upper):
            break

    return replies


def _send_command_and_collect(
    command,
    timeout=SERIAL_TIMEOUT,
    wait_for_prefixes=None,
    wait_for_lines=None,
    cancel_event=None,
    cancel_command=None,
):
    if not command.endswith("\n"):
        command += "\n"

    with _SERIAL_LOCK:
        last_error = None

        for attempt in range(2):
            try:
                ser = _ensure_serial_locked(force_reopen=(attempt == 1))

                # Do not reset_input_buffer here.
                # It can cut BILL_STATUS:OFF into L_STATUS:OFF.
                _drain_stale_serial_lines(ser, drain_seconds=0.25)

                ser.write(command.encode("utf-8"))
                ser.flush()

                print(f"[SERIAL] Sent: {command.strip()}", flush=True)

                replies = _read_replies(
                    ser,
                    timeout=timeout,
                    wait_for_prefixes=wait_for_prefixes,
                    wait_for_lines=wait_for_lines,
                    cancel_event=cancel_event,
                    cancel_command=cancel_command,
                )

                return replies

            except Exception as e:
                last_error = e
                print(
                    f"[SERIAL] command attempt {attempt + 1} failed: {e}",
                    flush=True,
                )
                _close_serial_locked()
                time.sleep(0.5)

        print(
            f"[SERIAL] final failure sending {command.strip()}: {last_error}",
            flush=True,
        )

        return []


def _error_result(message, replies=None, **extra):
    payload = {
        "success": False,
        "message": str(message or "Operation failed."),
        "replies": replies or [],
    }
    payload.update(extra)
    return payload


def _success_result(message, replies=None, **extra):
    payload = {
        "success": True,
        "message": str(message or "OK"),
        "replies": replies or [],
    }
    payload.update(extra)
    return payload


# =====================================================
# ERROR TRANSLATION
# =====================================================

def _translate_arduino_error(message):
    raw = _clean(message)
    upper = raw.upper()

    if upper == "BUSY":
        return "Arduino is busy. Please wait for the current hardware movement to finish."

    if "COOLDOWN" in upper:
        return "The dispenser is cooling down. Please try again after a few seconds."

    if "KIT_EMPTY" in upper:
        if "KIT1" in upper:
            return "The HIV kit lane appears to be empty. Please refill KIT1 and reset/home the lane."
        if "KIT2" in upper:
            return "The Dengue kit lane appears to be empty. Please refill KIT2 and reset/home the lane."
        return "The selected kit lane appears to be empty. Please refill and reset/home the lane."

    if "UNKNOWN_POSITION" in upper or "NOT_HOMED" in upper:
        return "The actuator position is not trusted. Home the kit actuators before dispensing."

    if "INVALID_TRAVEL" in upper:
        return "The actuator is already past the next target slot. Home the lane and reset kit slots."

    if "NOT_WIRED" in upper:
        return "This kit lane is not wired in the current machine."

    if "DISPENSE_STOPPED" in upper:
        return "Kit dispensing was stopped before completion."

    if "COIN_DISPENSE_FAILED" in upper:
        return "Coin dispensing did not complete. Please check the coin servo mechanism and coin path."

    if "INVALID_CHANGE_PLAN" in upper:
        return "The change dispense plan sent to Arduino was invalid."

    if "DISPOSE" in upper:
        return "Trash disposal did not complete. Check the ULN2003 stepper mechanism."

    return raw or "Arduino reported an error."


def _extract_dispensed_kit(line):
    parts = _clean(line).split(":")

    if len(parts) >= 2:
        return parts[1].strip().upper()

    return ""


# =====================================================
# PRODUCT MAPPING
# =====================================================

def map_product_to_command(product_id="", product_name=""):
    from config_manager import config

    pid = _clean(product_id)
    pname = _clean(product_name).lower()

    product = config.get_product_by_id(pid) if pid else None

    if product:
        slot = _clean(product.get("dispense_slot", "")).upper()

        if slot == "KIT1":
            return "DISPENSE:KIT1\n", "KIT1"

        if slot == "KIT2":
            return "DISPENSE:KIT2\n", "KIT2"

        if slot == "KIT3":
            return "DISPENSE:KIT3\n", "KIT3"

    pid_lower = pid.lower()

    if pid_lower in ("1", "kit1", "oral", "oralkit", "hiv123", "hiv"):
        return "DISPENSE:KIT1\n", "KIT1"

    if pid_lower in ("2", "kit2", "blood", "bloodkit", "dengue123", "dengue"):
        return "DISPENSE:KIT2\n", "KIT2"

    if pid_lower in ("3", "kit3", "urine", "urinekit"):
        return "DISPENSE:KIT3\n", "KIT3"

    if "oral" in pname or "hiv" in pname:
        return "DISPENSE:KIT1\n", "KIT1"

    if "blood" in pname or "dengue" in pname:
        return "DISPENSE:KIT2\n", "KIT2"

    if "urine" in pname:
        return "DISPENSE:KIT3\n", "KIT3"

    return None, None


# =====================================================
# BASIC COMMANDS
# =====================================================

def ping_arduino():
    replies = _send_command_and_collect(
        "PING\n",
        timeout=5,
        wait_for_lines={"PONG"},
    )

    for line in replies:
        if _upper(line) == "PONG":
            return _success_result("Arduino is reachable.", replies)

    replies = _send_command_and_collect(
        "READY\n",
        timeout=5,
        wait_for_lines={"READY"},
    )

    for line in replies:
        if _upper(line) == "READY":
            return _success_result("Arduino is reachable.", replies)

    return _error_result("No PONG/READY reply received.", replies)


def send_raw_command(command, timeout=5):
    replies = _send_command_and_collect(command, timeout=timeout)

    if not replies:
        return _error_result("No reply from Arduino.", [])

    for line in replies:
        upper = _upper(line)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _success_result(replies[-1], replies)


def send_bill_on_command():
    replies = _send_command_and_collect(
        "BILL_ON\n",
        timeout=8,
        wait_for_lines={"BILL_STATUS:ON"},
    )

    for line in replies:
        upper = _upper(line)

        if upper == "BILL_STATUS:ON":
            return _success_result(line, replies)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _error_result("No BILL_STATUS:ON confirmation received.", replies)


def send_bill_off_command():
    replies = _send_command_and_collect(
        "BILL_OFF\n",
        timeout=8,
        wait_for_lines={"BILL_STATUS:OFF"},
    )

    for line in replies:
        upper = _upper(line)

        if upper == "BILL_STATUS:OFF":
            return _success_result(line, replies)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _error_result("No BILL_STATUS:OFF confirmation received.", replies)


def get_bill_status(timeout=5):
    replies = _send_command_and_collect(
        "GET_BILL_STATUS\n",
        timeout=timeout,
        wait_for_prefixes=("BILL_STATUS:",),
    )

    for line in replies:
        upper = _upper(line)

        if upper.startswith("BILL_STATUS:"):
            return _success_result(line, replies, status_line=line)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _error_result("No BILL_STATUS response received.", replies)


def reset_servos_to_rest(timeout=8):
    replies = _send_command_and_collect(
        "RESET_SERVOS\n",
        timeout=timeout,
        wait_for_lines={"SERVOS_RESET"},
    )

    for line in replies:
        upper = _upper(line)

        if upper == "SERVOS_RESET":
            return _success_result(line, replies)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _error_result("No SERVOS_RESET confirmation received.", replies)


# =====================================================
# HOMING / STATUS
# =====================================================

def home_kit_actuators(timeout=HOME_TIMEOUT_SECONDS):
    global _HOMING_IN_PROGRESS

    with _HOMING_LOCK:
        _HOMING_IN_PROGRESS = True

    try:
        _KITS_HOMED_EVENT.clear()

        replies = _send_command_and_collect(
            "HOME_KITS\n",
            timeout=timeout,
            wait_for_prefixes=("KIT_HOME_DONE:",),
        )

        got_ok = False
        final_line = None
        error_line = None
        busy = False

        for line in replies:
            upper = _upper(line)

            if upper == "OK":
                got_ok = True
            elif upper == "BUSY":
                busy = True
            elif upper.startswith("KIT_HOME_DONE:"):
                final_line = line
            elif _is_failure_line(upper):
                error_line = line

        if busy:
            return _error_result(_translate_arduino_error("BUSY"), replies)

        if error_line:
            return _error_result(_translate_arduino_error(error_line), replies)

        if final_line:
            _KITS_HOMED_EVENT.set()
            return _success_result(final_line, replies)

        if got_ok:
            return _error_result(
                "Arduino accepted HOME_KITS, but no KIT_HOME_DONE confirmation was received.",
                replies,
            )

        return _error_result("No valid HOME_KITS response received from Arduino.", replies)

    finally:
        with _HOMING_LOCK:
            _HOMING_IN_PROGRESS = False


def start_background_home_kits():
    global _HOMING_IN_PROGRESS

    with _HOMING_LOCK:
        if _HOMING_IN_PROGRESS:
            return {
                "success": True,
                "message": "HOME_KITS already running",
                "background": True,
            }

        if _KITS_HOMED_EVENT.is_set():
            return {
                "success": True,
                "message": "Kits already homed",
                "background": True,
            }

        _HOMING_IN_PROGRESS = True

    def _worker():
        global _HOMING_IN_PROGRESS

        try:
            print("[SERIAL] Background HOME_KITS started", flush=True)
            result = home_kit_actuators(timeout=HOME_TIMEOUT_SECONDS)
            print(f"[SERIAL] Background HOME_KITS result: {result}", flush=True)
        finally:
            with _HOMING_LOCK:
                _HOMING_IN_PROGRESS = False

    threading.Thread(
        target=_worker,
        name="BackgroundHomeKits",
        daemon=True,
    ).start()

    return {
        "success": True,
        "message": "HOME_KITS started in background",
        "background": True,
    }


def get_kit_status(timeout=5):
    replies = _send_command_and_collect(
        "GET_KIT_STATUS\n",
        timeout=timeout,
        wait_for_prefixes=("KIT_STATUS:",),
    )

    for line in replies:
        upper = _upper(line)

        if upper.startswith("KIT_STATUS:"):
            return _success_result(line, replies, status_line=line)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _error_result("No KIT_STATUS response received from Arduino.", replies)


def are_kits_homed():
    if _KITS_HOMED_EVENT.is_set():
        return True

    status = get_kit_status(timeout=5)

    if status.get("success"):
        line = str(status.get("status_line") or status.get("message") or "")

        if "KIT1=" in line and "KIT2=" in line:
            parts = line.split(";")

            if len(parts) >= 2 and all("HOMED=1" in part for part in parts[:2]):
                _KITS_HOMED_EVENT.set()
                return True

    return False


def ensure_kits_homed(timeout=HOME_TIMEOUT_SECONDS):
    with _HOMING_LOCK:
        homing_running = _HOMING_IN_PROGRESS

    if homing_running:
        ok = _KITS_HOMED_EVENT.wait(timeout=float(timeout))

        if ok:
            return _success_result("Kits homed.", [])

        return _error_result("Timed out waiting for background HOME_KITS before dispense.", [])

    if are_kits_homed():
        return _success_result("Kits already homed.", [])

    start_background_home_kits()

    ok = _KITS_HOMED_EVENT.wait(timeout=float(timeout))

    if ok:
        return _success_result("Kits homed.", [])

    return _error_result("Timed out waiting for HOME_KITS before dispense.", [])


def reset_kit_slots(timeout=5):
    replies = _send_command_and_collect(
        "RESET_KIT_SLOTS\n",
        timeout=timeout,
        wait_for_lines={"KIT_SLOTS_RESET"},
    )

    for line in replies:
        upper = _upper(line)

        if upper == "KIT_SLOTS_RESET":
            return _success_result(line, replies)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _error_result("No KIT_SLOTS_RESET confirmation received.", replies)


# =====================================================
# RETURN HOME AFTER STOCK ZERO
# =====================================================

def return_kit_home(kit_code, timeout=RETURN_HOME_TIMEOUT_SECONDS):
    kit_code = _clean(kit_code).upper()

    if kit_code not in {"KIT1", "KIT2"}:
        return _error_result(f"Invalid return-home kit code: {kit_code}", [])

    command = f"RETURN_{kit_code}_HOME\n"
    expected = f"KIT_RETURNED_HOME:{kit_code}"

    replies = _send_command_and_collect(
        command,
        timeout=timeout,
        wait_for_prefixes=(expected,),
    )

    got_ok = False
    final_line = None
    error_line = None
    busy = False

    for line in replies:
        upper = _upper(line)

        if upper == "OK":
            got_ok = True
        elif upper == "BUSY":
            busy = True
        elif upper.startswith(expected):
            final_line = line
        elif _is_failure_line(upper):
            error_line = line

    if busy:
        return _error_result(_translate_arduino_error("BUSY"), replies)

    if error_line:
        return _error_result(_translate_arduino_error(error_line), replies)

    if final_line:
        return _success_result(final_line, replies, returned_home=kit_code)

    if got_ok:
        return _error_result(
            f"Arduino accepted RETURN_{kit_code}_HOME, but no {expected} confirmation was received.",
            replies,
        )

    return _error_result(f"No valid RETURN_{kit_code}_HOME response received.", replies)


# =====================================================
# CHANGE DISPENSING
# =====================================================

def _normalize_breakdown(breakdown):
    normalized = {}

    if not isinstance(breakdown, dict):
        return normalized

    for key, value in breakdown.items():
        try:
            denom = int(key)
            qty = int(value)
        except Exception:
            continue

        if denom not in (20, 5, 1):
            continue

        if qty > 0:
            normalized[denom] = qty

    return normalized


def _build_change_command_payload(breakdown):
    normalized = _normalize_breakdown(breakdown)
    parts = []

    for denom in (20, 5, 1):
        qty = normalized.get(denom, 0)

        if qty > 0:
            parts.append(f"{denom}x{qty}")

    return ",".join(parts), normalized


def _parse_change_dispensed_line(change_line):
    result = {}

    try:
        payload = change_line.split(":", 1)[1].strip()
    except Exception:
        return result

    if not payload or payload == "0":
        return result

    for part in [p.strip() for p in payload.split(",") if p.strip()]:
        lowered = part.lower()

        if "x" not in lowered:
            continue

        left, right = lowered.split("x", 1)

        try:
            denom = int(left.strip())
            qty = int(right.strip())
        except Exception:
            continue

        if denom in (20, 5, 1) and qty > 0:
            result[denom] = qty

    return result


def _estimate_change_timeout(normalized):
    count20 = int(normalized.get(20, 0))
    count5 = int(normalized.get(5, 0))
    count1 = int(normalized.get(1, 0))

    estimated = 10.0 + (count20 * 8.0) + (count5 * 6.0) + (count1 * 6.0)

    groups = sum(1 for denom in (20, 5, 1) if normalized.get(denom, 0) > 0)
    estimated += max(0, groups - 1) * 2.0
    estimated += 15.0

    return max(30, int(round(estimated)))


def send_change_command(breakdown):
    command_payload, normalized = _build_change_command_payload(breakdown)

    if not normalized:
        return _error_result(
            "Invalid or empty change breakdown.",
            [],
            requested_breakdown={},
            confirmed_breakdown={},
        )

    timeout = _estimate_change_timeout(normalized)

    replies = _send_command_and_collect(
        f"DISPENSE_CHANGE:{command_payload}\n",
        timeout=timeout,
        wait_for_prefixes=("CHANGE_DISPENSED:",),
        wait_for_lines={"BUSY"},
    )

    got_ok = False
    change_line = None
    error_line = None
    busy = False

    for line in replies:
        upper = _upper(line)

        if upper == "OK":
            got_ok = True
        elif upper == "BUSY":
            busy = True
        elif upper.startswith("CHANGE_DISPENSED:"):
            change_line = line
        elif _is_failure_line(upper):
            error_line = line

    if busy:
        return _error_result(
            _translate_arduino_error("BUSY"),
            replies,
            requested_breakdown=normalized,
            confirmed_breakdown={},
        )

    if error_line:
        return _error_result(
            _translate_arduino_error(error_line),
            replies,
            requested_breakdown=normalized,
            confirmed_breakdown={},
        )

    if change_line:
        confirmed = _parse_change_dispensed_line(change_line)

        if confirmed != normalized:
            return _error_result(
                f"Arduino confirmed a different breakdown. Requested={normalized}, Confirmed={confirmed}",
                replies,
                requested_breakdown=normalized,
                confirmed_breakdown=confirmed,
            )

        return _success_result(
            change_line,
            replies,
            requested_breakdown=normalized,
            confirmed_breakdown=confirmed,
        )

    if got_ok:
        return _error_result(
            "Command accepted, but no final CHANGE_DISPENSED confirmation was received.",
            replies,
            requested_breakdown=normalized,
            confirmed_breakdown={},
        )

    return _error_result(
        "No valid change response received from Arduino.",
        replies,
        requested_breakdown=normalized,
        confirmed_breakdown={},
    )


# =====================================================
# KIT DISPENSING
# =====================================================

def _estimate_dispense_timeout(expected_kit, return_home_after=False):
    if return_home_after:
        return 150

    return DISPENSE_TIMEOUT_SECONDS


def send_dispense_command(
    product_id="",
    product_name="",
    return_home_after=False,
):
    homed = ensure_kits_homed()

    if not homed.get("success"):
        return homed

    command, expected_kit = map_product_to_command(
        product_id=product_id,
        product_name=product_name,
    )

    if not command or not expected_kit:
        return _error_result(
            f"Unknown product mapping. product_id={product_id}, product_name={product_name}",
            [],
        )

    timeout = _estimate_dispense_timeout(
        expected_kit,
        return_home_after=return_home_after,
    )

    replies = _send_command_and_collect(
        command,
        timeout=timeout,
        wait_for_prefixes=("DISPENSED:",),
        wait_for_lines={"BUSY"},
    )

    got_ok = False
    dispensed_line = None
    error_line = None
    busy = False

    for line in replies:
        upper = _upper(line)

        if upper == "OK":
            got_ok = True
        elif upper == "BUSY":
            busy = True
        elif upper.startswith("DISPENSED:"):
            dispensed_line = line
        elif _is_failure_line(upper):
            error_line = line

    if busy:
        return _error_result(
            "Arduino is busy dispensing another operation.",
            replies,
            expected_kit=expected_kit,
        )

    if error_line:
        return _error_result(
            _translate_arduino_error(error_line),
            replies,
            expected_kit=expected_kit,
            raw_error=error_line,
        )

    if not dispensed_line:
        if got_ok:
            return _error_result(
                f"Command accepted for {expected_kit}, but no final DISPENSED confirmation was received.",
                replies,
                expected_kit=expected_kit,
            )

        return _error_result(
            "No valid dispense response received from Arduino.",
            replies,
            expected_kit=expected_kit,
        )

    actual_kit = _extract_dispensed_kit(dispensed_line)

    if actual_kit != expected_kit:
        return _error_result(
            f"Arduino dispensed {actual_kit}, but expected {expected_kit}.",
            replies,
            expected_kit=expected_kit,
            actual_kit=actual_kit,
        )

    payload = _success_result(
        dispensed_line,
        replies,
        expected_kit=expected_kit,
        actual_kit=actual_kit,
        return_home_after=bool(return_home_after),
        returned_home=False,
        return_home_result=None,
    )

    if return_home_after and expected_kit in {"KIT1", "KIT2"}:
        home_result = return_kit_home(expected_kit)

        payload["return_home_result"] = home_result
        payload["returned_home"] = bool(home_result.get("success"))

        if not home_result.get("success"):
            payload["success"] = False
            payload["message"] = (
                f"{dispensed_line}, but return-home failed: "
                f"{home_result.get('message')}"
            )

    return payload


# =====================================================
# RVM / TRASH
# =====================================================

def _parse_trash_status_line(line):
    """Parse TRASH_STATUS:key=value,key=value into a dictionary."""
    raw = str(line or "").strip()
    if not raw.upper().startswith("TRASH_STATUS:"):
        return {}

    body = raw.split(":", 1)[1]
    parsed = {}

    for token in body.split(","):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, value = token.split("=", 1)
        parsed[key.strip().lower()] = value.strip()

    active = str(parsed.get("active", "")).upper() == "YES"
    deferred = str(parsed.get("deferred", "")).upper() == "YES"
    phase = str(parsed.get("phase", "")).upper()

    try:
        remaining_steps = int(parsed.get("remaining_steps", "0") or 0)
    except Exception:
        remaining_steps = 0

    completed = (not active) and (not deferred) and phase in {"", "IDLE"} and remaining_steps <= 0

    return {
        "raw": raw,
        "fields": parsed,
        "active": active,
        "deferred": deferred,
        "phase": phase,
        "remaining_steps": remaining_steps,
        "completed": completed,
    }


def get_dispose_status(timeout=3):
    """Read current trash/RVM disposal status from the revised Arduino sketch.

    The revised Arduino may print DISPOSED:TRASH asynchronously after an earlier
    DISPOSE_KIT command. We therefore treat either DISPOSED:TRASH or a
    TRASH_STATUS line with ACTIVE=NO, DEFERRED=NO, PHASE=IDLE,
    REMAINING_STEPS=0 as completed.
    """
    replies = _send_command_and_collect(
        "DISPOSE_STATUS\n",
        timeout=timeout,
        wait_for_prefixes=("TRASH_STATUS:",),
        wait_for_lines={"BUSY"},
    )

    status_payload = None
    disposed_line = None
    deferred_line = None
    error_line = None
    busy = False

    for line in replies:
        upper = _upper(line)

        if upper == "BUSY" or upper.startswith("BUSY:"):
            busy = True
        elif upper.startswith("TRASH_STATUS:"):
            status_payload = _parse_trash_status_line(line)
        elif upper.startswith("DISPOSED:"):
            disposed_line = line
        elif upper.startswith("DISPOSE_DEFERRED:"):
            deferred_line = line
        elif _is_failure_line(upper):
            error_line = line

    if error_line:
        return _error_result(_translate_arduino_error(error_line), replies)

    if status_payload:
        return _success_result(
            status_payload.get("raw") or "TRASH_STATUS",
            replies,
            **status_payload,
            async_disposed=bool(disposed_line),
            deferred_line=deferred_line,
        )

    if disposed_line:
        return _success_result(
            disposed_line,
            replies,
            active=False,
            deferred=False,
            phase="IDLE",
            remaining_steps=0,
            completed=True,
            async_disposed=True,
        )

    if deferred_line:
        return _success_result(
            deferred_line,
            replies,
            active=False,
            deferred=True,
            phase="DEFERRED",
            remaining_steps=None,
            completed=False,
            deferred_line=deferred_line,
        )

    if busy:
        return _error_result(_translate_arduino_error("BUSY"), replies)

    return _error_result("No valid trash disposal status received from Arduino.", replies)


def send_dispose_kit_command(timeout=None):
    """Start or resume low-priority RVM disposal without waiting for completion.

    This matches the revised Arduino firmware:
      DISPOSE_KIT -> OK + DISPOSE_STARTED:TRASH / DISPOSE_RESUMED:TRASH
      later, asynchronously -> DISPOSED:TRASH

    The old Raspi code waited for DISPOSED:TRASH here and held _SERIAL_LOCK for
    the entire motor movement. That defeated the non-blocking Arduino update.
    This function now releases the serial lock immediately after the start/resume
    acknowledgement. Use get_dispose_status() to poll for completion.
    """
    if timeout is None:
        timeout = 4

    if is_disposal_cancel_requested():
        return _error_result(
            "Trash disposal deferred because the booth currently needs Arduino serial.",
            ["CANCEL_REQUESTED:DISPOSAL_DEFERRED"],
            deferred=True,
            started=False,
            completed=False,
        )

    replies = _send_command_and_collect(
        "DISPOSE_KIT\n",
        timeout=timeout,
        wait_for_prefixes=(
            "DISPOSE_STARTED:",
            "DISPOSE_RESUMED:",
            "DISPOSED:",
            "DISPOSE_DEFERRED:",
            "BUSY:",
        ),
        wait_for_lines={"BUSY"},
    )

    got_ok = False
    started_line = None
    resumed_line = None
    disposed_line = None
    deferred_line = None
    error_line = None
    busy_line = None

    for line in replies:
        upper = _upper(line)

        if upper == "OK":
            got_ok = True
        elif upper == "BUSY" or upper.startswith("BUSY:"):
            busy_line = line
        elif upper.startswith("DISPOSE_STARTED:"):
            started_line = line
        elif upper.startswith("DISPOSE_RESUMED:"):
            resumed_line = line
        elif upper.startswith("DISPOSED:"):
            disposed_line = line
        elif upper.startswith("DISPOSE_DEFERRED:"):
            deferred_line = line
        elif _is_failure_line(upper):
            error_line = line

    if deferred_line:
        return _error_result(
            "Trash disposal was deferred by Arduino.",
            replies,
            deferred=True,
            started=False,
            completed=False,
        )

    if busy_line:
        # BUSY:DISPOSING means disposal is already active; treat as started so
        # the queue worker can poll DISPOSE_STATUS instead of failing the job.
        if _upper(busy_line).startswith("BUSY:DISPOSING"):
            return _success_result(
                busy_line,
                replies,
                started=True,
                resumed=True,
                completed=False,
                already_active=True,
            )
        return _error_result(_translate_arduino_error("BUSY"), replies)

    if error_line:
        return _error_result(_translate_arduino_error(error_line), replies)

    if disposed_line:
        return _success_result(
            disposed_line,
            replies,
            started=True,
            completed=True,
            active=False,
        )

    if started_line or resumed_line:
        line = started_line or resumed_line
        return _success_result(
            line,
            replies,
            started=True,
            resumed=bool(resumed_line),
            completed=False,
            active=True,
        )

    if got_ok:
        # Some sketches may only send OK before starting. Treat it as accepted,
        # then the queue worker will verify using DISPOSE_STATUS.
        return _success_result(
            "DISPOSE_KIT accepted by Arduino.",
            replies,
            started=True,
            completed=False,
            active=True,
        )

    return _error_result("No valid trash disposal start/resume response received from Arduino.", replies)

def stop_all():
    replies = _send_command_and_collect(
        "STOP\n",
        timeout=5,
        wait_for_lines={"STOP_ALL"},
    )

    for line in replies:
        if _upper(line) == "STOP_ALL":
            return _success_result(line, replies)

    return _error_result("No STOP_ALL confirmation received.", replies)


# =====================================================
# BACKWARD-COMPATIBLE ALIASES
# =====================================================

bill_on = send_bill_on_command
bill_off = send_bill_off_command

reset_servos = reset_servos_to_rest
send_reset_servos_command = reset_servos_to_rest

home_kits = home_kit_actuators
send_home_kits_command = home_kit_actuators
send_get_kit_status_command = get_kit_status

send_return_kit_home_command = return_kit_home
send_reset_kit_slots_command = reset_kit_slots

# After physical restocking, home that lane and reset its slot pointer.
send_restock_command = lambda kit_code="ALL": (
    home_kit_actuators() if _upper(kit_code) == "ALL"
    else _send_home_single_kit_fallback(kit_code)
)


def _send_home_single_kit_fallback(kit_code):
    kit_code = _upper(kit_code)

    if kit_code not in {"KIT1", "KIT2"}:
        return _error_result(f"Invalid kit code: {kit_code}", [])

    command = f"HOME_{kit_code}\n"
    expected = f"KIT_HOME_DONE:{kit_code}"

    replies = _send_command_and_collect(
        command,
        timeout=HOME_TIMEOUT_SECONDS,
        wait_for_prefixes=(expected,),
    )

    for line in replies:
        upper = _upper(line)

        if upper.startswith(expected):
            _KITS_HOMED_EVENT.set()
            return _success_result(line, replies)

        if upper == "BUSY" or _is_failure_line(upper):
            return _error_result(_translate_arduino_error(line), replies)

    return _error_result(f"No {expected} confirmation received.", replies)