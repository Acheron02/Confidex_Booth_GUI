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
)
from backend.device_sync import start_background_sync


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
        return False


def start_startup_hardware_init():
    """
    Run serial startup tasks without blocking the GUI.

    Order:
      1. BILL_OFF
      2. RESET_SERVOS
      3. HOME_KITS
      4. KIT_STATUS

    This prevents the app from freezing before WelcomePage appears.
    """

    def worker():
        print("[STARTUP] Hardware init thread started", flush=True)

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


def start_kit_queue_worker_safely():
    try:
        start_kit_queue_worker()
    except Exception as e:
        print(f"[KIT QUEUE] Failed to start worker: {e}", flush=True)
        traceback.print_exc()


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("CONFIDEX")
        self.configure(fg_color="#F5F2DE")
        self.frames = {}

        self.current_user = None
        self.selected_product = None
        self.current_transaction_id = None

        self.current_error_dialog = None
        self._is_resetting = False

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

        # Start background systems only after the GUI exists.
        self.after(300, start_startup_hardware_init)
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

        if hasattr(frame, "update_data"):
            try:
                frame.update_data(**kwargs)
            except TypeError:
                if kwargs:
                    print(f"update_data mismatch for {page_name}: {kwargs}", flush=True)
            except Exception as e:
                print(f"[APP] update_data failed for {page_name}: {e}", flush=True)
                traceback.print_exc()
                self.show_error(
                    f"Failed to load {page_name}.\n{e}",
                    title="Page Error",
                    action_text="Reset System",
                    on_action=self.full_reset,
                )
                return

        frame.tkraise()

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

        if on_action is None:
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

            self.show_frame("WelcomePage")

        except Exception as e:
            print(f"[SYSTEM] Full reset failed: {e}", flush=True)
            traceback.print_exc()
        finally:
            self._is_resetting = False

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