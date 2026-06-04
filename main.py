import os
import sys
import subprocess
import time
import threading
import traceback
from frontend import tk_compat as ctk

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from pages.welcome_page import WelcomePage
from pages.qr_login_page import QRLoginPage
from pages.purchase_page import PurchasePage
from pages.payment_method_page import PaymentMethodPage
from pages.cash_payment_page import CashPaymentPage
from pages.receipt_page import ReceiptPage
from pages.how_to_use_page import HowToUsePage
from pages.kit_insertion_page import KitInsertionPage
from frontend.components.loading import LoadingPage
from pages.online_payment_page import OnlinePaymentPage
from pages.dispensing_page import DispensingPage
from pages.cash_bill_check_page import CashBillCheckPage
from pages.change_dispensing_page import ChangeDispensingPage
from frontend.components.error_dialog import ErrorDialog
try:
    from backend.util.kit_queue_worker import start_kit_queue_worker, stop_kit_queue_worker
except Exception:
    from backend.util.kit_queue_worker import start_kit_queue_worker
    stop_kit_queue_worker = None
from backend.util.dispenser_serial import (
    send_bill_off_command,
    reset_servos_to_rest,
    home_kit_actuators,
    get_kit_status,
    reset_kit_slots,
    request_disposal_cancel,
    clear_disposal_cancel_request,
    is_return_home_in_progress,
)
from backend.device_sync import start_background_sync, mark_inventory_dirty, push_inventory_if_dirty

try:
    from backend.offline_cash_sync import start_offline_cash_sync
except Exception:
    start_offline_cash_sync = None

try:
    from backend.offline_flow_sync import start_offline_flow_sync
except Exception:
    start_offline_flow_sync = None
from backend.system_events import drain_visible_events, report_error, report_warning
from backend.payment_recovery import (
    get_pending_payments_for_user,
    update_payment_status,
    mark_payment_completed,
    build_transaction_payload_from_record,
)
from backend.flow_state import save_active_flow, clear_active_flow, get_active_flow
from backend.booth_activity import set_booth_activity, page_requires_arduino_serial
from backend.util import api_client
from config_manager import config
from backend.booth_session_guard import (
    extract_user_id,
    get_resume_flow_for_user,
    build_resume_kwargs,
    SAFE_AUTO_RESUME_STAGES,
)


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))


def start_fastapi():
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.server:app",
                "--host",
                "0.0.0.0",
                "--port",
                "5000",
            ],
            cwd=ROOT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[FASTAPI] Started using {sys.executable} -m uvicorn", flush=True)
    except Exception as e:
        print(f"[FASTAPI] Failed to start: {e}", flush=True)
        report_error("fastapi", "Local API Failed", f"Failed to start local booth API: {e}", visible=True)


def ensure_servos_reset():
    try:
        result = reset_servos_to_rest(timeout=8)
        print(f"[STARTUP] RESET_SERVOS result: {result}", flush=True)

        if result.get("success"):
            return True

        message = str(result.get("message", "")).upper()
        replies = [str(item).upper() for item in result.get("replies", [])]

        if "UNKNOWN_COMMAND" in message or any("UNKNOWN_COMMAND" in r for r in replies):
            print(
                "[STARTUP] RESET_SERVOS is not supported by the current Arduino sketch; continuing.",
                flush=True,
            )
            return True

        return False
    except Exception as e:
        print(f"[STARTUP] Failed to reset servos to rest: {e}", flush=True)
        report_warning("startup", "Servo Reset Failed", f"Unable to reset coin servos: {e}", visible=True)
        return False


def ensure_bill_acceptor_off():
    try:
        result = send_bill_off_command()
        print(f"[STARTUP] BILL_OFF result: {result}", flush=True)

        if result.get("success"):
            return True

        print(
            "[STARTUP] BILL_OFF did not confirm. Continuing because bill acceptor is also controlled by GPIO page logic.",
            flush=True,
        )
        return False
    except Exception as e:
        print(f"[STARTUP] Failed to force bill acceptor OFF: {e}", flush=True)
        report_warning("startup", "Bill Acceptor Reset Failed", f"Unable to force bill acceptor off: {e}", visible=True)
        return False



def _product_lane_capacity(product: dict, default: int = 4) -> int:
    """Return the physical full-lane quantity for a kit product.

    The current Arduino firmware dispenses slots 2, 3, 4, and 5, so the
    default capacity is 4. If the website/config later adds a capacity field,
    this helper will honor it without another code change.
    """
    if not isinstance(product, dict):
        return int(default)

    for key in (
        "restock_stock",
        "restock_quantity",
        "max_stock",
        "stock_capacity",
        "lane_capacity",
        "capacity",
        "full_stock",
    ):
        if key not in product:
            continue
        try:
            value = int(float(product.get(key)))
            if value > 0:
                return value
        except Exception:
            continue

    return int(default)


def sync_restocked_inventory_after_kit_slot_reset(reason: str = "kit_slot_reset") -> bool:
    """Mark local and website kit inventory as full after Arduino slot reset.

    RESET_KIT_SLOTS only resets the Arduino's next-slot pointer. It does not
    modify inventory.json or the website inventory by itself. This function is
    the missing bridge: once the booth intentionally treats both kit lanes as
    refilled, the Pi updates local product stock and pushes that snapshot to
    the website.

    This is intentionally limited to products mapped to KIT1/KIT2 so it does
    not touch future non-actuator products.
    """
    try:
        products = config.get_products() or []
    except Exception as e:
        print(f"[RESTOCK] Could not read products for restock sync: {e}", flush=True)
        return False

    changed = []

    for product in products:
        if not isinstance(product, dict):
            continue

        product_id = str(product.get("product_id") or product.get("productID") or product.get("id") or "").strip()
        dispense_slot = str(product.get("dispense_slot") or product.get("slot") or "").strip().upper()

        if not product_id or dispense_slot not in {"KIT1", "KIT2"}:
            continue

        capacity = _product_lane_capacity(product, default=4)

        try:
            before = int(config.get_product_stock(product_id))
        except Exception:
            before = 0

        try:
            ok = config.set_product_stock(product_id, capacity)
        except Exception as e:
            print(f"[RESTOCK] Failed to set stock for {product_id}: {e}", flush=True)
            ok = False

        if ok:
            changed.append(
                {
                    "product_id": product_id,
                    "slot": dispense_slot,
                    "before": before,
                    "after": capacity,
                }
            )

    if not changed:
        print("[RESTOCK] No KIT1/KIT2 products were updated after kit slot reset.", flush=True)
        return False

    print(f"[RESTOCK] Kit inventory restored after {reason}: {changed}", flush=True)

    try:
        mark_inventory_dirty()
    except Exception as e:
        print(f"[RESTOCK] Failed to mark inventory dirty: {e}", flush=True)

    try:
        pushed = push_inventory_if_dirty(force=True)
        print(f"[RESTOCK] Inventory push after restock result: {pushed}", flush=True)
    except Exception as e:
        print(f"[RESTOCK] Inventory push after restock failed: {e}", flush=True)

    return True

def ensure_kit_actuators_home():
    """
    Home both kit actuators and reset Arduino kit-slot pointers.

    Why RESET_KIT_SLOTS is included:
    - The Raspberry Pi inventory may say stock was refilled.
    - The Arduino may still remember that KIT1/KIT2 reached the empty/end slot
      from a previous run.
    - HOME_KITS confirms physical home position.
    - RESET_KIT_SLOTS makes the Arduino assume the lanes were restocked and
      start again from the first dispense slot.

    Run this only during startup/restock recovery when the machine has been
    physically checked or intentionally treated as freshly stocked.
    """
    try:
        result = home_kit_actuators(timeout=120)
        print(f"[STARTUP] HOME_KITS result: {result}", flush=True)

        if result.get("success"):
            try:
                slot_reset = reset_kit_slots(timeout=8)
                print(f"[STARTUP] RESET_KIT_SLOTS result: {slot_reset}", flush=True)

                if slot_reset.get("success"):
                    sync_restocked_inventory_after_kit_slot_reset(reason="startup_home_and_reset")
                else:
                    report_warning(
                        "startup",
                        "Kit Slot Reset Warning",
                        "Kit actuators were homed, but Arduino slot pointers were not reset. "
                        "If the lanes were refilled, dispensing may still report KIT_EMPTY.",
                        details=slot_reset,
                        visible=True,
                    )
            except Exception as e:
                print(f"[STARTUP] RESET_KIT_SLOTS failed: {e}", flush=True)
                report_warning(
                    "startup",
                    "Kit Slot Reset Failed",
                    f"Unable to reset Arduino kit slot pointers: {e}",
                    visible=True,
                )

        status = get_kit_status(timeout=5)
        print(f"[STARTUP] KIT_STATUS result: {status}", flush=True)

        return bool(result.get("success"))
    except Exception as e:
        print(f"[STARTUP] Failed to home kit actuators: {e}", flush=True)
        report_error("startup", "Kit Homing Failed", f"Unable to home kit actuators: {e}", visible=True)
        return False

def start_startup_hardware_init(app=None):
    """
    Run serial startup tasks without blocking the GUI.

    Order:
      1. BILL_OFF
      2. RESET_SERVOS
      3. HOME_KITS
      4. KIT_STATUS

    This prevents the app from freezing before WelcomePage appears. While these
    startup Arduino commands are running, background trash disposal is marked
    unsafe so it cannot compete with homing/reset commands.
    """

    def worker():
        print("[STARTUP] Hardware init thread started", flush=True)

        try:
            if app is not None:
                try:
                    app.startup_hardware_ready = False
                except Exception:
                    pass

            set_booth_activity(
                page_name="StartupHardwareInit",
                transaction_id="",
                busy=True,
                reason="startup_arduino_init",
            )
            request_disposal_cancel()
        except Exception as e:
            print(f"[STARTUP] Failed to publish startup activity: {e}", flush=True)

        try:
            ensure_bill_acceptor_off()
        except Exception as e:
            print(f"[STARTUP] BILL_OFF startup step failed: {e}", flush=True)

        try:
            ensure_servos_reset()
        except Exception as e:
            print(f"[STARTUP] RESET_SERVOS startup step failed: {e}", flush=True)

        try:
            ensure_kit_actuators_home()
        except Exception as e:
            print(f"[STARTUP] HOME_KITS startup step failed: {e}", flush=True)

        try:
            page = getattr(app, "current_page_name", None) or "WelcomePage"
            tx = getattr(app, "current_transaction_id", None) or ""
            arduino_required = page_requires_arduino_serial(page)
            set_booth_activity(
                page_name=page,
                transaction_id=tx,
                busy=arduino_required,
                reason=("arduino_serial_required" if arduino_required else "arduino_serial_free"),
            )
            if not arduino_required:
                clear_disposal_cancel_request()
        except Exception as e:
            print(f"[STARTUP] Failed to restore booth activity: {e}", flush=True)

        try:
            if app is not None:
                app.startup_hardware_ready = True
                try:
                    app.after(0, lambda: setattr(app, "startup_hardware_ready", True))
                except Exception:
                    pass
        except Exception:
            pass

        print("[STARTUP] Hardware init thread finished", flush=True)

    threading.Thread(
        target=worker,
        name="StartupHardwareInit",
        daemon=True,
    ).start()


def start_device_sync_safely():
    try:
        start_background_sync()
    except Exception as e:
        print(f"[DEVICE WS] Failed to start background sync: {e}", flush=True)
        traceback.print_exc()
        report_warning("device_sync", "Website Sync Failed", f"Could not start booth website sync: {e}", visible=True)

    try:
        if start_offline_cash_sync is not None:
            start_offline_cash_sync(interval_seconds=30, initial_delay_seconds=3)
    except Exception as e:
        print(f"[OFFLINE TX] Failed to start offline cash sync: {e}", flush=True)

    try:
        if start_offline_flow_sync is not None:
            start_offline_flow_sync(interval_seconds=45, initial_delay_seconds=9)
    except Exception as e:
        print(f"[OFFLINE FLOW] Failed to start receipt/image sync: {e}", flush=True)
def start_kit_queue_worker_safely():
    try:
        start_kit_queue_worker()
    except Exception as e:
        print(f"[KIT QUEUE] Failed to start worker: {e}", flush=True)
        traceback.print_exc()
        report_error("kit_queue", "Kit Queue Worker Failed", f"Could not start camera/kit queue worker: {e}", visible=True)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("CONFIDEX")
        self.configure(fg_color="#F5F2DE")
        self.frames = {}

        self.current_user = None
        self.selected_product = None
        self.current_transaction_id = None
        self.current_page_name = None
        self.active_flow_kwargs = {}

        self.current_error_dialog = None
        self.current_error_source = None
        self.startup_hardware_ready = False
        self._is_resetting = False
        self._event_dialog_showing = False
        self._resume_check_in_progress = False
        self._resume_candidate_user_id = None

        self._dismissed_event_keys = {}
        self._current_visible_event_key = None
        self._current_visible_event = None

        self._shutdown_requested = False

        # Escape exits the app again, but it now uses clean shutdown.
        # Do NOT bind Escape directly to self.destroy(), because Tk may close
        # while OpenCV/camera/background workers are still alive.
        self.bind("<Escape>", lambda e: self.shutdown_app(reason="escape_key"))

        try:
            self.protocol("WM_DELETE_WINDOW", lambda: self.shutdown_app(reason="window_close"))
        except Exception:
            pass

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        self.geometry(f"{screen_w}x{screen_h}+0+0")
        self.minsize(screen_w, screen_h)

        self.update_idletasks()
        self.after(100, self.enable_fullscreen)

        for PageClass in (
            WelcomePage,
            QRLoginPage,
            PurchasePage,
            PaymentMethodPage,
            CashBillCheckPage,
            CashPaymentPage,
            ChangeDispensingPage,
            OnlinePaymentPage,
            ReceiptPage,
            DispensingPage,
            HowToUsePage,
            KitInsertionPage,
            LoadingPage,
        ):
            page = PageClass(self, self)
            self.frames[PageClass.__name__] = page
            page.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.show_frame("WelcomePage")

        threading.excepthook = self._thread_exception_handler
        self.after(500, self._poll_system_events)

        # Start background systems only after the GUI exists.
        self.after(300, lambda: start_startup_hardware_init(self))
        self.after(700, start_device_sync_safely)

        # Delaying this helps avoid native camera/OpenCV startup crashes before Tk is stable.
        self.after(1200, start_kit_queue_worker_safely)

    def enable_fullscreen(self):
        try:
            self.attributes("-fullscreen", True)
        except Exception as e:
            print("Fullscreen failed:", e, flush=True)
            try:
                self.state("zoomed")
            except Exception as e2:
                print("Zoomed mode failed:", e2, flush=True)
        self.lift()
        self.focus_force()

    def show_frame(self, page_name, **kwargs):
        try:
            if (
                page_name != "LoadingPage"
                and page_requires_arduino_serial(page_name)
                and is_return_home_in_progress()
            ):
                return self.show_frame(
                    "LoadingPage",
                    message="Finishing empty kit lane reset",
                    next_page=page_name,
                    next_kwargs=kwargs,
                    delay=1200,
                )
        except Exception as e:
            print(f"[APP] Background return-home gate check failed: {e}", flush=True)

        self.current_page_name = page_name
        self.active_flow_kwargs = dict(kwargs or {})

        # Publish booth activity for the background RVM worker. Disposal is
        # low-priority, but it is not limited to WelcomePage. It may continue
        # on pages/tasks that do not require Arduino USB-serial communication.
        # Pages that use bill relay, coin servos, kit actuators, homing, or
        # change dispensing mark the booth as Arduino-busy and the queue worker
        # will defer/stop trash disposal until Arduino-safe again.
        try:
            current_tx = (
                kwargs.get("transaction_id")
                or self.current_transaction_id
                or ""
            )
            arduino_required = page_requires_arduino_serial(page_name)
            set_booth_activity(
                page_name=page_name,
                transaction_id=current_tx,
                busy=arduino_required,
                reason=("arduino_serial_required" if arduino_required else "arduino_serial_free"),
            )

            if arduino_required:
                request_disposal_cancel()
            else:
                clear_disposal_cancel_request()
        except Exception as e:
            print(f"[APP] Failed to update booth activity state: {e}", flush=True)

        frame = self.frames.get(page_name)
        if not frame:
            print(f"[APP] Frame '{page_name}' does not exist", flush=True)
            return

        user_data = kwargs.get("user_data")
        selected_product = kwargs.get("selected_product") or kwargs.get("product")
        transaction_id = kwargs.get("transaction_id")

        if user_data:
            self.current_user = user_data
        if selected_product:
            self.selected_product = selected_product
        if transaction_id:
            self.current_transaction_id = transaction_id

        # Persist post-payment page state so recoverable errors can return to
        # the same page with the same transaction data instead of logging out.
        if page_name in {"ChangeDispensingPage", "ReceiptPage", "DispensingPage", "HowToUsePage", "KitInsertionPage"}:
            try:
                save_active_flow(
                    stage=page_name,
                    user_data=self.current_user or user_data or {},
                    selected_product=self.selected_product or selected_product or {},
                    transaction_id=self.current_transaction_id or transaction_id or "",
                    extra=kwargs,
                )
            except Exception as e:
                print(f"[APP] Failed to save active flow state: {e}", flush=True)

        if hasattr(frame, "update_data"):
            try:
                frame.update_data(**kwargs)
            except TypeError:
                if kwargs:
                    print(f"update_data mismatch for {page_name}: {kwargs}", flush=True)
            except Exception as e:
                print(f"[APP] update_data failed for {page_name}: {e}", flush=True)
                traceback.print_exc()
                recoverable = page_name in {"HowToUsePage", "KitInsertionPage"}
                self.show_error(
                    f"Failed to load {page_name}.\n{e}",
                    title="Page Error",
                    action_text="Retry / Continue" if recoverable else "Reset System",
                    on_action=self.retry_current_step if recoverable else self.full_reset,
                )
                return

        frame.tkraise()

        if user_data and page_name not in {
            "OnlinePaymentPage",
            "ReceiptPage",
            "DispensingPage",
            "HowToUsePage",
            "KitInsertionPage",
            "LoadingPage",
        }:
            self.after(600, lambda data=user_data: self._check_resume_after_login(data))

    def show_loading_then(self, message, next_page, delay=900, **kwargs):
        self.show_frame(
            "LoadingPage",
            message=message,
            next_page=next_page,
            next_kwargs=kwargs,
            delay=delay,
        )

    def _restore_page_click_input(self):
        try:
            grabbed = self.grab_current()
        except Exception:
            grabbed = None

        if grabbed is not None:
            try:
                grabbed.grab_release()
            except Exception:
                pass

        try:
            self.grab_release()
        except Exception:
            pass

        try:
            self.focus_force()
        except Exception:
            pass

        try:
            frame = self.frames.get(self.current_page_name or "")
            if frame is not None:
                frame.focus_set()
        except Exception:
            pass

    def close_error(self):
        dialog = self.current_error_dialog
        self.current_error_dialog = None
        self.current_error_source = None

        if dialog is not None:
            try:
                release = getattr(dialog, "_safe_release", None)
                if callable(release):
                    release()
            except Exception:
                pass
            try:
                dialog.destroy()
            except Exception:
                pass

        self._current_visible_event = None
        self._current_visible_event_key = None
        self._restore_page_click_input()
        try:
            self.after(50, self._restore_page_click_input)
        except Exception:
            pass
    def show_error(
        self,
        message,
        title="Something went wrong",
        action_text="Reset System",
        on_action=None,
        on_close=None,
        error_source=None,
    ):
        print(f"[APP ERROR] {title}: {message}", flush=True)

        clean_action_text = "" if action_text is None else str(action_text).strip()

        # Never show a no-button kiosk overlay by default. Older code used
        # action_text=None for display-only warnings, which made the booth feel stuck.
        if not clean_action_text:
            clean_action_text = "Dismiss"
            on_action = self._close_error_only
        elif clean_action_text.lower() == "dismiss" and on_action is None:
            on_action = self._close_error_only
        elif on_action is None:
            on_action = self.full_reset

        if on_close is None:
            on_close = self._close_error_only

        try:
            if self.current_error_dialog is not None:
                try:
                    self.current_error_dialog.on_action = on_action
                    self.current_error_dialog.on_close = on_close
                    self.current_error_source = str(error_source or "").strip() or self.current_error_source
                    self.current_error_dialog.set_message(
                        message,
                        title=title,
                        action_text=clean_action_text,
                    )
                    self.current_error_dialog.lift()
                    return
                except Exception:
                    try:
                        self.current_error_dialog.destroy()
                    except Exception:
                        pass
                    self.current_error_dialog = None

            self.current_error_source = str(error_source or "").strip() or None
            self.current_error_dialog = ErrorDialog(
                self,
                message=message,
                title=title,
                action_text=clean_action_text,
                on_action=on_action,
                on_close=on_close,
            )
        except Exception as e:
            print(f"[APP] Error dialog failed: {e}", flush=True)
            traceback.print_exc()
    def _close_error_only(self):
        self.close_error()
        self._restore_page_click_input()
    def _call_page_method_if_exists(self, method_name):
        for name, frame in self.frames.items():
            method = getattr(frame, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception as e:
                    print(f"[APP] {name}.{method_name} failed: {e}", flush=True)

    def _stop_runtime_activity(self):
        cleanup_methods = [
            "stop_camera",
            "stop_video",
            "_stop_polling",
            "_cancel_redirect",
            "stop_animation",
            "stop_status_animation",
            "hide_loading",
            "disable_bill_acceptor",
        ]

        for method_name in cleanup_methods:
            self._call_page_method_if_exists(method_name)

        try:
            result = send_bill_off_command()
            print(f"[SYSTEM] BILL_OFF: {result}", flush=True)
        except Exception as e:
            print(f"[SYSTEM] BILL_OFF failed: {e}", flush=True)

    def _stop_background_workers_for_exit(self):
        """Stop native/background workers before destroying Tk.

        This is intentionally used only for app exit, not normal Full Reset.
        Full Reset should keep the kit queue worker alive so pending result
        captures/uploads are not interrupted.
        """
        try:
            if stop_kit_queue_worker is not None:
                stop_kit_queue_worker(join=True, timeout=3.0)
                print("[SYSTEM] Kit queue worker stopped for app shutdown", flush=True)
        except TypeError:
            try:
                stop_kit_queue_worker()
                print("[SYSTEM] Kit queue worker stop requested for app shutdown", flush=True)
            except Exception as e:
                print(f"[SYSTEM] Kit queue worker stop failed: {e}", flush=True)
        except Exception as e:
            print(f"[SYSTEM] Kit queue worker stop failed: {e}", flush=True)

    def shutdown_app(self, reason="user_exit"):
        """Cleanly exit the kiosk app.

        The previous Escape binding called self.destroy() directly. If the Tk/X
        window disappears while OpenCV/V4L2 is still active, Raspberry Pi can
        abort with messages like:
          X connection to :0 broken
          cv::Exception: Can't fetch data from terminated TLS container
        """
        if getattr(self, "_shutdown_requested", False):
            return

        self._shutdown_requested = True
        print(f"[SYSTEM] SHUTDOWN requested reason={reason}", flush=True)

        try:
            self.close_error()
        except Exception:
            pass

        try:
            self._stop_runtime_activity()
        except Exception as e:
            print(f"[SYSTEM] Runtime cleanup during shutdown failed: {e}", flush=True)

        self._stop_background_workers_for_exit()

        try:
            self.destroy()
        except Exception as e:
            print(f"[SYSTEM] Tk destroy during shutdown failed: {e}", flush=True)

    def _reset_all_page_state(self):
        for name, frame in self.frames.items():
            if hasattr(frame, "reset_fields") and callable(frame.reset_fields):
                try:
                    frame.reset_fields()
                except Exception as e:
                    print(f"[APP] {name}.reset_fields failed: {e}", flush=True)

            for attr, value in (
                ("user_data", {}),
                ("selected_product", None),
                ("product", None),
                ("discount", 0),
                ("transaction_id", None),
                ("transaction_in_progress", False),
                ("loading_visible", False),
                ("processing", False),
                ("payment_session_id", None),
                ("payment_checkout_url", None),
                ("payment_reference", None),
                ("payment_amount", 0),
                ("payment_status", None),
                ("request_in_progress", False),
                ("status_request_in_progress", False),
                ("redirecting_to_cash", False),
                ("finalizing_purchase", False),
                ("total_cash_inserted", 0),
                ("website_transaction_id", None),
            ):
                if hasattr(frame, attr):
                    try:
                        setattr(frame, attr, value)
                    except Exception:
                        pass

    def _has_durable_active_flow(self):
        try:
            flow = get_active_flow()
        except Exception:
            flow = None

        if not flow:
            return False

        stage = str(flow.get("stage") or "").strip()
        transaction_id = str(flow.get("transaction_id") or "").strip()

        return bool(
            stage in {
                "ChangeDispensingPage",
                "ReceiptPage",
                "DispensingPage",
                "HowToUsePage",
                "KitInsertionPage",
            }
            and transaction_id
        )

    def _should_preserve_active_flow_on_reset(self):
        if self.current_page_name not in {
            "ChangeDispensingPage",
            "ReceiptPage",
            "DispensingPage",
            "HowToUsePage",
            "KitInsertionPage",
        }:
            return False

        if self.current_transaction_id:
            return True

        return self._has_durable_active_flow()

    def full_reset(self):
        """
        Kiosk-flow reset. It logs out and returns to WelcomePage.

        If reset happens during a paid/post-payment flow, preserve that user's
        active flow so the same user can scan again and resume.

        If reset happens before a paid flow, clear only the current user's current
        flow if one is known. Never clear another user's saved flow.
        """
        if self._is_resetting:
            return

        self._is_resetting = True
        print("[SYSTEM] FULL RESET", flush=True)

        # Save these before page/frame state is wiped.
        reset_user = self.current_user
        reset_transaction_id = self.current_transaction_id or ""
        preserve_active_flow = self._should_preserve_active_flow_on_reset()

        try:
            self.close_error()
            self._stop_runtime_activity()
            self._reset_all_page_state()

            self.current_user = None
            self.selected_product = None
            self.current_transaction_id = None
            self.active_flow_kwargs = {}

            if preserve_active_flow:
                print("[SYSTEM] Preserving active paid flow for same-user resume after reset.", flush=True)
            else:
                try:
                    clear_active_flow(
                        user_data=reset_user,
                        transaction_id=reset_transaction_id,
                    )
                except TypeError:
                    # Backward compatibility if the old flow_state.py is still installed.
                    # After replacing flow_state.py with the per-user version, this fallback
                    # should no longer be used.
                    print(
                        "[SYSTEM] clear_active_flow does not support scoped clearing yet. "
                        "Replace backend/flow_state.py with the per-user version.",
                        flush=True,
                    )
                except Exception as e:
                    print(f"[SYSTEM] Failed to clear active flow: {e}", flush=True)

            self.show_frame("WelcomePage")

        except Exception as e:
            print(f"[SYSTEM] Full reset failed: {e}", flush=True)
            traceback.print_exc()
        finally:
            self._is_resetting = False

        self._event_dialog_showing = False
        self._resume_check_in_progress = False
        self._resume_candidate_user_id = None
        self._dismissed_event_keys = {}
        self._current_visible_event_key = None
        self._current_visible_event = None

    def recover_to_page(self, page_name, message=None, **kwargs):
        """
        Recover forward without logging the user out.
        Use this after successful payment/dispense when flow must continue.
        """
        print(f"[SYSTEM] RECOVER TO PAGE -> {page_name}", flush=True)

        try:
            self.close_error()
            self._stop_runtime_activity()

            if "user_data" not in kwargs and self.current_user:
                kwargs["user_data"] = self.current_user
            if "selected_product" not in kwargs and self.selected_product:
                kwargs["selected_product"] = self.selected_product
            if "transaction_id" not in kwargs and self.current_transaction_id:
                kwargs["transaction_id"] = self.current_transaction_id

            if message:
                self.show_loading_then(message, page_name, delay=700, **kwargs)
            else:
                self.show_frame(page_name, **kwargs)

        except Exception as e:
            print(f"[SYSTEM] Recover to page failed: {e}", flush=True)
            traceback.print_exc()
            self.full_reset()

    def continue_after_success(self):
        """
        Default forward recovery after payment success.
        If tutorial fails, continue to kit insertion.
        """
        self.recover_to_page(
            "KitInsertionPage",
            message="Continuing to kit insertion",
        )

    def _get_current_frame(self):
        if not self.current_page_name:
            return None
        return self.frames.get(self.current_page_name)

    def _is_current_page_recoverable(self):
        return self.current_page_name in {"HowToUsePage", "KitInsertionPage"}

    def retry_current_step(self):
        """Retry the failed operation without logging the user out."""
        frame = self._get_current_frame()
        self.close_error()

        if frame is not None:
            method = getattr(frame, "recover_from_error", None)
            if callable(method):
                try:
                    method()
                    return
                except Exception as e:
                    print(f"[APP] recover_from_error failed for {self.current_page_name}: {e}", flush=True)
                    traceback.print_exc()

        # Fallback: reload the same page with preserved kwargs.
        kwargs = dict(self.active_flow_kwargs or {})
        if "user_data" not in kwargs and self.current_user:
            kwargs["user_data"] = self.current_user
        if "selected_product" not in kwargs and self.selected_product:
            kwargs["selected_product"] = self.selected_product
        if "transaction_id" not in kwargs and self.current_transaction_id:
            kwargs["transaction_id"] = self.current_transaction_id

        if self.current_page_name:
            self.show_frame(self.current_page_name, **kwargs)

    def show_recoverable_error(self, message, title="Recoverable Error", event=None):
        """Show a recoverable error and let the current page auto-retry if possible."""
        frame = self._get_current_frame()

        if frame is not None:
            scheduler = getattr(frame, "schedule_auto_recovery", None)
            if callable(scheduler):
                try:
                    scheduler(event or {})
                except Exception as e:
                    print(f"[APP] schedule_auto_recovery failed: {e}", flush=True)

        self.show_error(
            message,
            title=title,
            action_text="Retry / Continue",
            on_action=self.retry_current_step,
            on_close=self._close_error_only,
        )

    def show_background_warning(self, message, title="Background Warning"):
        """Show a warning without forcing logout/reset. Background workers keep retrying."""
        self.show_error(
            message,
            title=title,
            action_text="Dismiss",
            on_action=self._close_error_only,
            on_close=self._close_error_only,
        )

    def _event_key(self, event):
        source = str(event.get("source") or "system").strip().lower()
        title = str(event.get("title") or "").strip().lower()
        severity = str(event.get("severity") or "").strip().lower()
        return f"{severity}:{source}:{title}"

    def _is_event_suppressed(self, key):
        if not key:
            return False
        now = time.time()
        expires_at = float(self._dismissed_event_keys.get(key) or 0)
        if expires_at <= 0:
            return False
        if expires_at < now:
            try:
                self._dismissed_event_keys.pop(key, None)
            except Exception:
                pass
            return False
        return True

    def _suppress_event_temporarily(self, key, seconds=90):
        if not key:
            return
        try:
            self._dismissed_event_keys[key] = time.time() + max(15, int(seconds or 90))
        except Exception:
            pass

    def _dismiss_current_event_dialog(self):
        key = self._current_visible_event_key
        event = self._current_visible_event or {}
        if key and self._is_internet_related_event(event):
            self._suppress_event_temporarily(key, seconds=90)
        self.close_error()

    def _reactivate_qr_login_page(self, delay_ms=100):
        """Keep QRLoginPage usable after a login is refused by the flow guard."""
        try:
            if self.current_page_name != "QRLoginPage":
                return

            qr_page = self.frames.get("QRLoginPage")
            if qr_page is not None and hasattr(qr_page, "reset_fields"):
                self.after(
                    max(0, int(delay_ms)),
                    lambda: qr_page.reset_fields(start_active=True),
                )
        except Exception as e:
            print(f"[FLOW GUARD] Failed to reactivate QR page: {e}", flush=True)

    def _is_internet_related_event(self, event):
        severity = str(event.get("severity") or "").lower()
        if severity in {"critical"}:
            return False

        source = str(event.get("source") or "").lower()
        title = str(event.get("title") or "").lower()
        message = str(event.get("message") or "").lower()
        details = str(event.get("details") or "").lower()
        haystack = " ".join([source, title, message, details])

        hardware_block_keywords = (
            "jam", "kit jam", "dispense failed", "dispensing failed",
            "change dispense failed", "trash disposal failed", "disposal failed",
            "arduino failed", "serial failed", "camera failed", "homing failed",
            "bill acceptor", "servo reset failed",
        )
        if any(word in haystack for word in hardware_block_keywords):
            return False

        internet_keywords = (
            "internet", "network", "connection", "remote disconnected", "timeout",
            "timed out", "api", "website", "websocket", "ws", "device sync",
            "device_sync", "offline", "sync failed", "upload pending", "upload failed",
            "receipt upload", "image upload", "result upload", "offline_flow_sync",
            "offline_cash_sync", "payment recovery", "payment_recovery",
        )
        return any(word in haystack for word in internet_keywords)

    def _poll_system_events(self):
        try:
            # Backward-compatible safety for partially patched/stale running files.
            # This prevents the GUI event loop from crashing if an older App
            # instance did not initialize these fields.
            if not hasattr(self, "current_error_source"):
                self.current_error_source = None
            if not hasattr(self, "current_error_dialog"):
                self.current_error_dialog = None
            if not hasattr(self, "_dismissed_event_keys"):
                self._dismissed_event_keys = {}
            if not hasattr(self, "_current_visible_event_key"):
                self._current_visible_event_key = None
            if not hasattr(self, "_current_visible_event"):
                self._current_visible_event = None

            events = drain_visible_events(max_items=10)

            for event in events:
                severity = str(event.get("severity", "info")).lower()
                source = str(event.get("source") or "system").strip()

                if severity in {"clear", "resolved", "recovered"}:
                    active_source = str(self.current_error_source or "").strip()
                    if self.current_error_dialog is not None and (
                        source in {"", "all"} or active_source in {"", source}
                    ):
                        print(f"[APP] Clearing visible event dialog for source={source}", flush=True)
                        self.close_error()

                    try:
                        src_key = source.lower()
                        for key in list(self._dismissed_event_keys.keys()):
                            if f":{src_key}:" in key or source in {"", "all"}:
                                self._dismissed_event_keys.pop(key, None)
                    except Exception:
                        pass
                    continue

                if severity not in {"error", "critical", "warning"}:
                    continue

                key = self._event_key(event)
                if self._is_event_suppressed(key):
                    continue

                title = event.get("title") or "Booth Warning"
                message = event.get("message") or "An issue occurred."
                display_message = f"{message}\n\nSource: {source}"

                self._current_visible_event_key = key
                self._current_visible_event = dict(event)

                if self._is_internet_related_event(event):
                    self.show_error(
                        display_message + "\n\nThe booth will continue in offline mode. Local data will be retried automatically when the website connection returns.",
                        title=title,
                        action_text="Dismiss",
                        on_action=self._dismiss_current_event_dialog,
                        on_close=self._dismiss_current_event_dialog,
                        error_source=source,
                    )
                    break

                if severity == "critical":
                    self.show_error(
                        display_message,
                        title=title,
                        action_text="Reset System",
                        on_action=self.full_reset,
                        on_close=self._close_error_only,
                        error_source=source,
                    )
                    break

                self.show_error(
                    display_message,
                    title=title,
                    action_text="Dismiss",
                    on_action=self._close_error_only,
                    on_close=self._close_error_only,
                    error_source=source,
                )
                break

        except Exception as e:
            print(f"[APP] Failed to poll system events: {e}", flush=True)

        self.after(500, self._poll_system_events)
    def _extract_user_id(self, user_data):
        if not isinstance(user_data, dict):
            return ""
        return str(user_data.get("_id") or user_data.get("userID") or user_data.get("id") or "").strip()

    def _active_flow_owner_id(self, flow=None):
        try:
            flow = flow if isinstance(flow, dict) else get_active_flow()
        except Exception:
            flow = None

        if not isinstance(flow, dict):
            return ""

        return extract_user_id(flow.get("user_data") or {})

    def _active_flow_belongs_to_other_user(self, user_data):
        try:
            flow = get_active_flow()
        except Exception:
            flow = None

        if not isinstance(flow, dict):
            return False

        stage = str(flow.get("stage") or "").strip()
        transaction_id = str(flow.get("transaction_id") or "").strip()

        if stage not in {
            "ChangeDispensingPage",
            "ReceiptPage",
            "DispensingPage",
            "HowToUsePage",
            "KitInsertionPage",
        } or not transaction_id:
            return False

        incoming_id = extract_user_id(user_data or {})
        owner_id = self._active_flow_owner_id(flow)

        return bool(incoming_id and owner_id and incoming_id != owner_id)

    def _discard_other_user_active_flow_for_new_login(self, user_data, reason="new_qr_login"):
        """Allow a different user to log in without deleting the previous user's flow.

        The old behavior fixed kiosk blocking by clearing the single active_flow row.
        That allowed the new user, but erased the previous user's unfinished paid
        transaction. With per-user active flows, another user's flow should be
        ignored for this login, not deleted.
        """
        if not self._active_flow_belongs_to_other_user(user_data):
            return False

        try:
            flow = get_active_flow() or {}
        except Exception:
            flow = {}

        old_owner = self._active_flow_owner_id(flow)
        new_owner = extract_user_id(user_data or {})
        old_stage = str((flow or {}).get("stage") or "").strip()
        old_tx = str((flow or {}).get("transaction_id") or "").strip()

        print(
            "[FLOW GUARD] Preserving other user's unfinished flow while allowing new login. "
            f"old_user={old_owner} new_user={new_owner} stage={old_stage} tx={old_tx} reason={reason}",
            flush=True,
        )

        # IMPORTANT:
        # Do not call clear_active_flow() here.
        # The scanned user will be checked by get_resume_flow_for_user(user_data).
        # If no flow belongs to the scanned user, they proceed to PurchasePage.
        return False

    def has_unfinished_paid_flow(self, user_data=None):
        try:
            if user_data:
                # Only the same user's flow should be considered unfinished
                # for that login. A stale flow from another user must not lock
                # the kiosk for the next person.
                return bool(get_resume_flow_for_user(user_data))

            flow = get_active_flow()
            if not flow:
                return False
            stage = str(flow.get("stage") or "").strip()
            tx = str(flow.get("transaction_id") or "").strip()
            return bool(stage in {
                "ChangeDispensingPage",
                "ReceiptPage",
                "DispensingPage",
                "HowToUsePage",
                "KitInsertionPage",
            } and tx)
        except Exception:
            return False

    def handle_qr_login_success(self, user_data):
        """Single gate for successful QR login.

        QRLoginPage calls this instead of directly opening PurchasePage.
        This prevents stale/repeated QR events from creating a second purchase
        while a paid booth flow is still unfinished.
        """
        user_data = user_data or {}

        if self.current_page_name != "QRLoginPage":
            print(
                f"[FLOW GUARD] Ignored QR login success outside QRLoginPage; current={self.current_page_name}",
                flush=True,
            )
            return

        if not bool(getattr(self, "startup_hardware_ready", False)):
            print("[FLOW GUARD] QR login refused because startup hardware init is still running.", flush=True)
            self.show_error(
                "The booth is still initializing the bill acceptor, servos, and kit actuators. "
                "Please wait until startup is finished, then scan again.",
                title="Booth Still Initializing",
                action_text="Dismiss",
                on_action=self._close_error_only,
                on_close=self._close_error_only,
                error_source="startup",
            )
            self._reactivate_qr_login_page(delay_ms=400)
            return

        # Do not block a new customer because an earlier customer's foreground
        # paid-flow resume record exists. If the same user scans, the flow is
        # resumed below. If a different user scans, the old user's flow is ignored
        # for this login but preserved so that user can resume later.
        self._discard_other_user_active_flow_for_new_login(
            user_data,
            reason="different_user_qr_login",
        )

        flow = get_resume_flow_for_user(user_data)
        if flow:
            stage = str(flow.get("stage") or "").strip()
            kwargs = build_resume_kwargs(user_data, flow)
            tx = str(kwargs.get("transaction_id") or "").strip()
            product = kwargs.get("selected_product") or kwargs.get("product") or {}
            product_name = "selected kit"
            if isinstance(product, dict):
                product_name = str(product.get("name") or product.get("type") or "selected kit")

            self.current_user = kwargs.get("user_data") or user_data
            self.selected_product = product if isinstance(product, dict) else None
            self.current_transaction_id = tx or self.current_transaction_id

            if stage in SAFE_AUTO_RESUME_STAGES:
                self.close_error()
                self.show_loading_then(
                    "Unfinished transaction found. Continuing your previous flow",
                    stage,
                    delay=900,
                    **kwargs,
                )
                return

            self.show_error(
                (
                    "A paid transaction was found, but it stopped during a hardware step.\n\n"
                    f"Step: {stage}\n"
                    f"Transaction: {tx}\n"
                    f"Product: {product_name}\n\n"
                    "Verify that repeating this hardware step is safe before continuing."
                ),
                title="Unfinished Paid Transaction",
                action_text="Continue Hardware Step",
                on_action=lambda s=stage, kw=kwargs: self.recover_to_page(
                    s,
                    message="Continuing unfinished hardware step",
                    **kw,
                ),
                on_close=self._close_error_only,
                error_source="active_flow",
            )
            return

        self.current_user = user_data
        self.show_frame("PurchasePage", user_data=user_data)

    def _check_resume_after_login(self, user_data):
        if get_resume_flow_for_user(user_data):
            return self.handle_qr_login_success(user_data)
        return self._check_online_payment_resume_after_login(user_data)

    def _check_online_payment_resume_after_login(self, user_data):
        user_id = self._extract_user_id(user_data)
        if not user_id:
            return

        if self._resume_check_in_progress:
            return

        # Do not interrupt active post-payment flows.
        active_page = None
        try:
            for name, frame in self.frames.items():
                if frame.winfo_ismapped():
                    active_page = name
                    break
        except Exception:
            pass

        self._resume_check_in_progress = True
        self._resume_candidate_user_id = user_id

        def worker():
            try:
                pending = get_pending_payments_for_user(user_id)

                for record in pending:
                    session_id = str(record.get("session_id") or "")
                    if not session_id:
                        continue

                    try:
                        res = api_client.get_paymongo_checkout_status(session_id)
                        data = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}

                        if not res.ok:
                            raise RuntimeError(data.get("error") or f"Status check failed: {res.status_code}")

                        status = str(data.get("status", record.get("status", "pending"))).lower()
                        paid = bool(data.get("paid", False)) or status in {"paid", "completed", "succeeded"}

                        if not paid:
                            continue

                        update_payment_status(session_id, status="paid", flow_stage="payment_confirmed")

                        transaction_id = str(record.get("website_transaction_id") or "")

                        if not transaction_id:
                            payload = build_transaction_payload_from_record(user_data, record)
                            transaction_res = api_client.post_transaction(payload)
                            transaction_data = transaction_res.json() if transaction_res.headers.get("content-type", "").startswith("application/json") else {}

                            if not transaction_res.ok:
                                raise RuntimeError(transaction_data.get("error") or "Paid payment found, but transaction save failed.")

                            transaction_obj = transaction_data.get("transaction") or {}
                            transaction_id = (
                                transaction_obj.get("_id")
                                or transaction_data.get("_id")
                                or transaction_data.get("transaction_id")
                                or transaction_data.get("id")
                                or ""
                            )

                            update_payment_status(
                                session_id,
                                status="paid",
                                website_transaction_id=transaction_id,
                                flow_stage="transaction_saved",
                            )

                        def continue_flow(record=record, transaction_id=transaction_id, session_id=session_id):
                            product = record.get("product") or {}
                            amount = float(record.get("amount", 0) or 0)
                            discount = float(record.get("discount", 0) or 0)

                            self.close_error()
                            self.show_loading_then(
                                "Payment found. Continuing your transaction",
                                "ReceiptPage",
                                delay=900,
                                user_data=user_data,
                                product=product,
                                selected_product=product,
                                discount=discount,
                                total_paid=amount,
                                change=0,
                                total=amount,
                                online_payment=True,
                                payment_method="paymongo",
                                payment_session_id=session_id,
                                payment_reference=record.get("reference") or session_id,
                                payment_amount=amount,
                                payment_mode="live",
                                simulated=False,
                                transaction_id=transaction_id,
                            )

                        self.after(0, continue_flow)
                        return

                    except Exception as e:
                        report_warning(
                            "payment_recovery",
                            "Payment Resume Check Failed",
                            "A previous online payment may still be pending, but the booth could not verify it right now.",
                            details={"session_id": session_id, "error": str(e)},
                            visible=True,
                        )
                        return

            except Exception as e:
                report_warning(
                    "payment_recovery",
                    "Payment Recovery Failed",
                    f"The booth could not check pending online payments: {e}",
                    visible=True,
                )
            finally:
                self._resume_check_in_progress = False

        threading.Thread(target=worker, name="PaymentRecoveryCheck", daemon=True).start()

    def report_callback_exception(self, exc, val, tb):
        error_text = "".join(traceback.format_exception(exc, val, tb))
        print("[TK CALLBACK ERROR]", error_text, flush=True)

        self.show_error(
            f"An unexpected application error occurred.\n\n{val}",
            title="Application Error",
            action_text="Reset System",
            on_action=self.full_reset,
        )

    def _thread_exception_handler(self, args):
        try:
            error_text = "".join(
                traceback.format_exception(
                    args.exc_type,
                    args.exc_value,
                    args.exc_traceback,
                )
            )
            print("[THREAD ERROR]", error_text, flush=True)

            report_error(
                getattr(args.thread, "name", "background-thread"),
                "Background Process Error",
                str(args.exc_value),
                details=error_text,
                visible=True,
            )

            self.after(
                0,
                lambda: self.show_error(
                    f"A background process failed.\n\n{args.exc_value}",
                    title="Background Error",
                    action_text="Reset System",
                    on_action=self.full_reset,
                ),
            )
        except Exception as e:
            print(f"[APP] Thread exception handler failed: {e}", flush=True)


if __name__ == "__main__":
    ctk.set_appearance_mode("light")

    start_fastapi()
    time.sleep(1)

    app = App()
    app.mainloop()