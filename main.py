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
from backend.util.kit_queue_worker import start_kit_queue_worker
from backend.util.dispenser_serial import (
    send_bill_off_command,
    reset_servos_to_rest,
    home_kit_actuators,
    get_kit_status,
    request_disposal_cancel,
    clear_disposal_cancel_request,
)
from backend.device_sync import start_background_sync
from backend.system_events import drain_visible_events, report_error, report_warning
from backend.payment_recovery import (
    get_pending_payments_for_user,
    update_payment_status,
    mark_payment_completed,
    build_transaction_payload_from_record,
)
from backend.flow_state import save_active_flow, clear_active_flow
from backend.booth_activity import set_booth_activity, page_requires_arduino_serial
from backend.util import api_client


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


def ensure_kit_actuators_home():
    """
    Home both kit actuators from Raspberry Pi side.

    This is intentionally allowed to run in a background thread by
    start_startup_hardware_init(), so the GUI does not freeze before showing.
    """
    try:
        result = home_kit_actuators(timeout=120)
        print(f"[STARTUP] HOME_KITS result: {result}", flush=True)

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
        self._is_resetting = False
        self._event_dialog_showing = False
        self._resume_check_in_progress = False
        self._resume_candidate_user_id = None

        self.bind("<Escape>", lambda e: self.destroy())

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
        if page_name in {"ReceiptPage", "DispensingPage", "HowToUsePage", "KitInsertionPage"}:
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
            self.after(600, lambda data=user_data: self._check_online_payment_resume_after_login(data))

    def show_loading_then(self, message, next_page, delay=900, **kwargs):
        self.show_frame(
            "LoadingPage",
            message=message,
            next_page=next_page,
            next_kwargs=kwargs,
            delay=delay,
        )

    def close_error(self):
        if self.current_error_dialog is not None:
            try:
                self.current_error_dialog.destroy()
            except Exception:
                pass
            self.current_error_dialog = None

    def show_error(
        self,
        message,
        title="Something went wrong",
        action_text="Reset System",
        on_action=None,
        on_close=None,
    ):
        print(f"[APP ERROR] {title}: {message}", flush=True)

        display_only = action_text is None or str(action_text).strip() == ""

        if display_only:
            on_action = None
        elif on_action is None:
            on_action = self.full_reset

        if on_close is None:
            on_close = self._close_error_only

        try:
            if self.current_error_dialog is not None:
                try:
                    self.current_error_dialog.on_action = on_action
                    self.current_error_dialog.on_close = on_close
                    self.current_error_dialog.set_message(
                        message,
                        title=title,
                        action_text=action_text,
                    )
                    self.current_error_dialog.lift()
                    return
                except Exception:
                    try:
                        self.current_error_dialog.destroy()
                    except Exception:
                        pass
                    self.current_error_dialog = None

            self.current_error_dialog = ErrorDialog(
                self,
                message=message,
                title=title,
                action_text=action_text,
                on_action=on_action,
                on_close=on_close,
            )
        except Exception as e:
            print(f"[APP] Error dialog failed: {e}", flush=True)
            traceback.print_exc()

    def _close_error_only(self):
        self.close_error()

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

    def full_reset(self):
        """
        Full kiosk reset:
        use for pre-payment or unsafe/fatal state.
        This logs out the user and returns to WelcomePage.
        """
        if self._is_resetting:
            return

        self._is_resetting = True
        print("[SYSTEM] FULL RESET", flush=True)

        try:
            self.close_error()
            self._stop_runtime_activity()
            self._reset_all_page_state()

            self.current_user = None
            self.selected_product = None
            self.current_transaction_id = None
            self.active_flow_kwargs = {}
            try:
                clear_active_flow()
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

    def _poll_system_events(self):
        try:
            events = drain_visible_events(max_items=5)

            for event in events:
                severity = str(event.get("severity", "info")).lower()

                if severity not in {"error", "critical", "warning"}:
                    continue

                title = event.get("title") or "Booth Warning"
                message = event.get("message") or "An issue occurred."
                source = event.get("source") or "system"

                self.show_error(
                    f"{message}\n\nSource: {source}",
                    title=title,
                    action_text=None,
                    on_action=None,
                )
                break

        except Exception as e:
            print(f"[APP] Failed to poll system events: {e}", flush=True)

        self.after(500, self._poll_system_events)

    def _extract_user_id(self, user_data):
        if not isinstance(user_data, dict):
            return ""
        return str(user_data.get("_id") or user_data.get("userID") or user_data.get("id") or "").strip()

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