import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, card_body
from backend.util import api_client
from config_manager import config

try:
    from backend.printer import generate_token, print_discount_qr
except Exception as e:
    generate_token = None
    print_discount_qr = None
    print(f"[RECEIPT] Printer import failed: {e}", flush=True)

try:
    from backend.sync_uploader import sync_receipt_and_images
except Exception as e:
    sync_receipt_and_images = None
    print(f"[RECEIPT] Sync uploader import failed: {e}", flush=True)

try:
    from backend.util.capture_manager import (
        get_or_create_capture_session,
        remember_capture_session,
        save_receipt_json,
        get_session_timestamp,
    )
except Exception as e:
    get_or_create_capture_session = None
    remember_capture_session = None
    save_receipt_json = None
    get_session_timestamp = None
    print(f"[RECEIPT] Capture manager import failed: {e}", flush=True)


def _theme(name, fallback):
    return getattr(theme, name, fallback)


BLACK = _theme("BLACK", "#000000")
CREAM = _theme("CREAM", "#F5F2DE")
ORANGE = _theme("ORANGE", "#C46A2A")
WHITE = _theme("WHITE", "#FFFFFF")
MUTED = _theme("MUTED", "#555555")
SUCCESS = _theme("SUCCESS", "#237B4B")
ERROR = _theme("ERROR", "#B3261E")


def app_font(size, weight="normal"):
    try:
        return theme.font(size, weight)
    except Exception:
        return ("Arial", size, weight)


def app_heavy(size):
    try:
        return theme.heavy(size)
    except Exception:
        return ("Arial", size, "bold")


def safe_configure(widget, **kwargs):
    try:
        widget.configure(**kwargs)
    except Exception:
        pass


def _find_project_root(start_path: Path) -> Path:
    current = start_path.resolve()

    for parent in [current] + list(current.parents):
        if (
            (parent / ".env.local").exists()
            or (parent / "package.json").exists()
            or (parent / ".git").exists()
        ):
            return parent

    try:
        return current.parents[2]
    except Exception:
        return current


ROOT = _find_project_root(Path(__file__))
CAPTURES_DIR = ROOT / "captures"
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

print("[RECEIPT] System initialized", flush=True)


class ReceiptPage(ctk.CTkFrame):
    REFRESH_MS = 1500

    def __init__(self, master, controller):
        super().__init__(master, fg_color=CREAM)

        self.controller = controller

        self.user_data = {}
        self.product = {}
        self.discount = 0
        self.total_paid = 0
        self.change = 0
        self.total = 0

        self.online_payment = False
        self.payment_method = "cash"
        self.payment_session_id = None
        self.payment_reference = None
        self.payment_amount = None
        self.payment_mode = None
        self.simulated = False
        self.transaction_id = None

        self.receipt_data = None
        self.receipt_session_dir = None
        self.receipt_text_content = ""
        self.discount_token = None
        self.discount_token_expires_at = None
        self.pending_coupon_payload = None
        self.coupon_print_status = "not_started"
        self.coupon_print_error = None

        self.receipt_user_id = None
        self.receipt_timestamp = None
        self.receipt_product_id = None

        self.printing_in_progress = False
        self.print_error = None
        self.print_success = False
        self.redirect_scheduled = False

        self.sync_in_progress = False
        self.sync_result = None

        self._config_refresh_job = None
        self._redirect_after_job = None
        self._print_watchdog_job = None
        self._receipt_run_id = 0
        self._sync_started_for_run_id = None

        self.shell = AppShell(
            self,
            title_right=config.get("receipt_page", "header_title", default="Receipt"),
        )
        self.shell.pack(fill="both", expand=True)

        self._build_ui()
        self._show_empty_receipt()
        self._start_config_refresh()

        self.bind("<Configure>", self._sync_layout, add="+")
        self.content_wrap.bind("<Configure>", self._sync_layout, add="+")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        top_bar = ctk.CTkFrame(self.shell.body, fg_color="transparent")
        top_bar.pack(fill="x", padx=28, pady=(16, 8))

        top_bar.grid_columnconfigure(0, weight=0)
        top_bar.grid_columnconfigure(1, weight=1)
        top_bar.grid_columnconfigure(2, weight=0)

        ctk.CTkFrame(
            top_bar,
            fg_color="transparent",
            width=140,
            height=58,
        ).grid(row=0, column=0, sticky="w")

        self.page_title = ctk.CTkLabel(
            top_bar,
            text=config.get("receipt_page", "title", default="Receipt"),
            font=app_heavy(32),
            text_color=BLACK,
            fg_color="transparent",
        )
        self.page_title.grid(row=0, column=1)

        ctk.CTkFrame(
            top_bar,
            fg_color="transparent",
            width=140,
            height=58,
        ).grid(row=0, column=2, sticky="e")

        self.content_wrap = ctk.CTkFrame(
            self.shell.body,
            fg_color="transparent",
        )
        self.content_wrap.pack(fill="both", expand=True, padx=28, pady=(8, 18))
        self.content_wrap.grid_columnconfigure(0, weight=1)
        self.content_wrap.grid_rowconfigure(0, weight=1)

        self.center_wrap = ctk.CTkFrame(
            self.content_wrap,
            fg_color="transparent",
        )
        self.center_wrap.grid(row=0, column=0)
        self.center_wrap.grid_columnconfigure(0, weight=1)

        self.card = RoundedCard(
            self.center_wrap,
            auto_size=False,
            pad=16,
        )
        self.card.grid(row=0, column=0)
        self.card.grid_propagate(False)

        body = card_body(self.card)
        safe_configure(body, fg_color=WHITE)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self.receipt_paper = ctk.CTkFrame(
            body,
            fg_color="#EEEEEE",
            corner_radius=28,
        )
        self.receipt_paper.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.receipt_paper.grid_columnconfigure(0, weight=1)

        self.receipt_header = ctk.CTkFrame(
            self.receipt_paper,
            fg_color="#EEEEEE",
        )
        self.receipt_header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 10))
        self.receipt_header.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.receipt_header,
            text="Payment Receipt",
            font=app_heavy(28),
            text_color=BLACK,
            fg_color="#EEEEEE",
            justify="center",
            anchor="center",
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        self.subtitle_label = ctk.CTkLabel(
            self.receipt_header,
            text="Transaction summary",
            font=app_font(14, "bold"),
            text_color=MUTED,
            fg_color="#EEEEEE",
            justify="center",
            anchor="center",
            wraplength=560,
        )
        self.subtitle_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.total_panel = ctk.CTkFrame(
            self.receipt_paper,
            fg_color=WHITE,
            corner_radius=24,
        )
        self.total_panel.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 12))
        self.total_panel.grid_columnconfigure(0, weight=1)

        self.total_label = ctk.CTkLabel(
            self.total_panel,
            text="Total Amount",
            font=app_font(15, "bold"),
            text_color=MUTED,
            fg_color=WHITE,
            justify="center",
            anchor="center",
        )
        self.total_label.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 0))

        self.total_amount_value = ctk.CTkLabel(
            self.total_panel,
            text="₱0.00",
            font=app_heavy(44),
            text_color=BLACK,
            fg_color=WHITE,
            justify="center",
            anchor="center",
        )
        self.total_amount_value.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 10))

        self.amount_meta = ctk.CTkFrame(
            self.total_panel,
            fg_color=WHITE,
        )
        self.amount_meta.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 14))
        self.amount_meta.grid_columnconfigure(0, weight=1, uniform="amount")
        self.amount_meta.grid_columnconfigure(1, weight=1, uniform="amount")

        self.total_paid_block = self._make_amount_chip(
            self.amount_meta,
            column=0,
            title="Paid",
            value="₱0.00",
        )

        self.change_block = self._make_amount_chip(
            self.amount_meta,
            column=1,
            title="Change",
            value="₱0.00",
        )

        self.details_panel = ctk.CTkFrame(
            self.receipt_paper,
            fg_color=WHITE,
            corner_radius=24,
        )
        self.details_panel.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 8))
        self.details_panel.grid_columnconfigure(0, weight=1)

        self.detail_rows = {}

        self._make_detail_row("transaction", "Transaction")
        self._make_detail_row("date_time", "Date / Time")
        self._make_detail_row("user", "User")
        self._make_detail_row("item", "Item")
        self._make_detail_row("type", "Type")
        self._make_detail_row("price", "Price")
        self._make_detail_row("discount", "Discount")
        self._make_detail_row("payment", "Payment")

        self.receipt_text = ctk.CTkLabel(
            self.receipt_paper,
            text="",
            font=app_font(1, "bold"),
            justify="left",
            anchor="w",
            text_color="#EEEEEE",
            fg_color="#EEEEEE",
            wraplength=1,
        )
        self.receipt_text.grid(row=3, column=0, padx=1, pady=1)
        self.receipt_text.grid_remove()

        self.hidden_status_holder = ctk.CTkFrame(
            self.receipt_paper,
            fg_color="#EEEEEE",
        )
        self.hidden_status_holder.grid(row=4, column=0, padx=1, pady=1)
        self.hidden_status_holder.grid_remove()

        self.saved_path_label = ctk.CTkLabel(
            self.hidden_status_holder,
            text="",
            font=app_font(1, "bold"),
            text_color="#EEEEEE",
            fg_color="#EEEEEE",
            wraplength=1,
        )
        self.saved_path_label.grid(row=0, column=0)

        self.print_status_label = ctk.CTkLabel(
            self.hidden_status_holder,
            text="",
            font=app_font(1, "bold"),
            text_color="#EEEEEE",
            fg_color="#EEEEEE",
            wraplength=1,
        )
        self.print_status_label.grid(row=1, column=0)

    def _make_amount_chip(self, parent, column, title, value):
        chip = ctk.CTkFrame(
            parent,
            fg_color="#F3F3F3",
            corner_radius=16,
        )
        chip.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0 if column == 0 else 6, 6 if column == 0 else 0),
        )
        chip.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            chip,
            text=title,
            font=app_font(13, "bold"),
            text_color=MUTED,
            fg_color="#F3F3F3",
            justify="center",
            anchor="center",
        )
        title_label.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))

        value_label = ctk.CTkLabel(
            chip,
            text=value,
            font=app_font(20, "bold"),
            text_color=BLACK,
            fg_color="#F3F3F3",
            justify="center",
            anchor="center",
            wraplength=220,
        )
        value_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 10))

        return {
            "frame": chip,
            "title": title_label,
            "value": value_label,
        }

    def _make_detail_row(self, key, label):
        row = ctk.CTkLabel(
            self.details_panel,
            text=f"{label}: —",
            font=app_font(15, "bold"),
            text_color=BLACK,
            fg_color=WHITE,
            justify="left",
            anchor="w",
            wraplength=600,
        )
        row.grid(
            row=len(self.detail_rows),
            column=0,
            sticky="ew",
            padx=20,
            pady=(10 if len(self.detail_rows) == 0 else 4, 4),
        )

        self.detail_rows[key] = {
            "widget": row,
            "label": label,
            "value": "—",
        }

    def _set_detail(self, key, value, color=None):
        if key not in self.detail_rows:
            return

        safe_value = str(value if value is not None and value != "" else "—")
        row = self.detail_rows[key]
        row["value"] = safe_value

        try:
            row["widget"].configure(
                text=f"{row['label']}: {safe_value}",
                text_color=color or BLACK,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Data receiving
    # ------------------------------------------------------------------

    def update_data(
        self,
        user_data=None,
        product=None,
        selected_product=None,
        selected_item=None,
        discount=0,
        total_paid=0,
        change=0,
        total=0,
        online_payment=False,
        payment_method="cash",
        payment_session_id=None,
        payment_reference=None,
        payment_amount=None,
        payment_mode=None,
        simulated=False,
        transaction_id=None,
        **kwargs,
    ):
        print("[RECEIPT] update_data called", flush=True)

        # Critical fix:
        # ReceiptPage is reused after the first transaction.
        # The old code kept redirect_scheduled=True after transaction #1,
        # so transaction #2 could finish but never schedule navigation.
        self._begin_new_receipt_run()

        if product is None:
            product = (
                selected_product
                or selected_item
                or kwargs.get("product")
                or kwargs.get("selected_product")
                or kwargs.get("selected_item")
                or kwargs.get("item")
            )

        if user_data is None:
            user_data = (
                kwargs.get("user_data")
                or kwargs.get("user")
                or kwargs.get("current_user")
            )

        if product is None:
            product = self._controller_product_fallback()

        if user_data is None:
            user_data = self._controller_user_fallback()

        self.user_data = user_data.copy() if isinstance(user_data, dict) else {}
        self.product = product.copy() if isinstance(product, dict) else {}

        if not self.product:
            print("[RECEIPT WARNING] No product received.", flush=True)

        if not self.user_data:
            print("[RECEIPT WARNING] No user_data received.", flush=True)

        self.discount = self._number(discount or kwargs.get("discount"), 0)
        self.total_paid = self._number(
            total_paid
            or kwargs.get("total_paid")
            or kwargs.get("paid")
            or kwargs.get("amount_paid"),
            0,
        )
        self.change = self._number(change or kwargs.get("change"), 0)
        self.total = self._number(total or kwargs.get("total") or kwargs.get("total_amount"), 0)

        self.online_payment = bool(online_payment or kwargs.get("online_payment", False))
        self.payment_method = payment_method or kwargs.get("payment_method") or "cash"
        self.payment_session_id = payment_session_id or kwargs.get("payment_session_id")
        self.payment_reference = payment_reference or kwargs.get("payment_reference")
        self.payment_amount = payment_amount if payment_amount is not None else kwargs.get("payment_amount")
        self.payment_mode = payment_mode or kwargs.get("payment_mode")
        self.simulated = bool(simulated or kwargs.get("simulated", False))

        self.transaction_id = (
            transaction_id
            or kwargs.get("transaction_id")
            or kwargs.get("transactionID")
            or kwargs.get("transactionId")
            or self.product.get("transaction_id")
            or self.product.get("transactionID")
            or self.product.get("transactionId")
            or self.product.get("selection_id")
            or self.user_data.get("transaction_id")
            or self.user_data.get("latest_transaction_id")
            or self.payment_reference
            or self.payment_session_id
        )

        self._apply_receipt_data(self._receipt_run_id)

    def reset_fields(self, **kwargs):
        if kwargs:
            self.update_data(**kwargs)
            return

        self._begin_new_receipt_run(clear_payload=True)
        self._show_empty_receipt()

    def on_show(self, **kwargs):
        if kwargs:
            self.update_data(**kwargs)

    def set_data(self, **kwargs):
        if kwargs:
            self.update_data(**kwargs)

    def load_data(self, **kwargs):
        if kwargs:
            self.update_data(**kwargs)

    def _begin_new_receipt_run(self, clear_payload=False):
        self._receipt_run_id += 1
        self._sync_started_for_run_id = None
        self._cancel_redirect_job()
        self._cancel_print_watchdog()

        self.printing_in_progress = False
        self.print_error = None
        self.print_success = False
        self.redirect_scheduled = False
        self.sync_in_progress = False
        self.sync_result = None

        self.receipt_data = None
        self.receipt_session_dir = None
        self.receipt_text_content = ""
        self.discount_token = None
        self.discount_token_expires_at = None
        self.pending_coupon_payload = None
        self.coupon_print_status = "not_started"
        self.coupon_print_error = None
        self.receipt_user_id = None
        self.receipt_timestamp = None
        self.receipt_product_id = None

        if clear_payload:
            self.user_data = {}
            self.product = {}
            self.discount = 0
            self.total_paid = 0
            self.change = 0
            self.total = 0
            self.online_payment = False
            self.payment_method = "cash"
            self.payment_session_id = None
            self.payment_reference = None
            self.payment_amount = None
            self.payment_mode = None
            self.simulated = False
            self.transaction_id = None

    def _is_current_run(self, run_id):
        return run_id == self._receipt_run_id

    def _cancel_redirect_job(self):
        if self._redirect_after_job is not None:
            try:
                self.after_cancel(self._redirect_after_job)
            except Exception:
                pass
            self._redirect_after_job = None

    def _cancel_print_watchdog(self):
        if self._print_watchdog_job is not None:
            try:
                self.after_cancel(self._print_watchdog_job)
            except Exception:
                pass
            self._print_watchdog_job = None

    def _controller_product_fallback(self):
        for attr in ["selected_product", "current_product", "product", "latest_product"]:
            try:
                value = getattr(self.controller, attr, None)
            except Exception:
                value = None

            if isinstance(value, dict):
                return value

        return None

    def _controller_user_fallback(self):
        for attr in ["current_user", "user_data", "user"]:
            try:
                value = getattr(self.controller, attr, None)
            except Exception:
                value = None

            if isinstance(value, dict):
                return value

        return None

    # ------------------------------------------------------------------
    # Receipt logic
    # ------------------------------------------------------------------

    def _apply_receipt_data(self, run_id):
        if not self._is_current_run(run_id):
            return

        purchase_dt = datetime.now()

        username = (
            self.user_data.get("username")
            or self.user_data.get("name")
            or self.user_data.get("displayName")
            or "User"
        )

        user_id = (
            self.user_data.get("userID")
            or self.user_data.get("user_id")
            or self.user_data.get("_id")
            or self.user_data.get("id")
            or "Unknown"
        )

        product_name = (
            self.product.get("name")
            or self.product.get("productName")
            or self.product.get("product_name")
            or self.product.get("type")
            or "Unknown Item"
        )

        product_id = (
            self.product.get("productID")
            or self.product.get("product_id")
            or self.product.get("_id")
            or self.product.get("id")
            or "Unknown"
        )

        product_type = (
            self.product.get("type")
            or self.product.get("productType")
            or self.product.get("category")
            or "Unknown"
        )

        price = self._number(
            self.product.get("price")
            or self.product.get("finalPrice")
            or self.product.get("amount")
            or self.product.get("product_price"),
            0,
        )

        if self.total <= 0:
            self.total = price * (1 - float(self.discount or 0) / 100.0)

        if self.payment_amount is not None and self.total_paid <= 0:
            self.total_paid = self._number(self.payment_amount, self.total)

        if self.total_paid <= 0:
            self.total_paid = self.total

        if self.change <= 0:
            self.change = max(0.0, self.total_paid - self.total)

        if not self.transaction_id:
            self.transaction_id = f"TXN-{purchase_dt.strftime('%Y%m%d%H%M%S')}-{str(user_id)[:6]}"

        self.transaction_id = str(self.transaction_id)

        self.user_data["transaction_id"] = self.transaction_id
        self.user_data["latest_transaction_id"] = self.transaction_id

        date_str = purchase_dt.strftime("%Y-%m-%d")
        time_str = purchase_dt.strftime("%I:%M:%S %p")

        if self.online_payment or str(self.payment_method).lower() not in ["cash", ""]:
            mode_of_payment = f"Online ({self.payment_method})"
        else:
            mode_of_payment = "Cash"

        print("[RECEIPT RESOLVED DATA]", flush=True)
        print(f"  run_id={run_id}", flush=True)
        print(f"  transaction_id={self.transaction_id}", flush=True)
        print(f"  username={username}", flush=True)
        print(f"  user_id={user_id}", flush=True)
        print(f"  product_name={product_name}", flush=True)
        print(f"  product_id={product_id}", flush=True)
        print(f"  product_type={product_type}", flush=True)
        print(f"  price={price}", flush=True)
        print(f"  total={self.total}", flush=True)
        print(f"  total_paid={self.total_paid}", flush=True)
        print(f"  change={self.change}", flush=True)

        try:
            self.shell.set_header_right(f"Welcome, {username}!")
        except Exception:
            pass

        self.subtitle_label.configure(text="Transaction summary", text_color=MUTED)

        self.total_amount_value.configure(
            text=f"₱{self.total:.2f}",
            text_color=BLACK,
        )

        self.total_paid_block["value"].configure(
            text=f"₱{self.total_paid:.2f}",
            text_color=BLACK,
        )

        self.change_block["value"].configure(
            text=f"₱{self.change:.2f}",
            text_color=SUCCESS if self.change > 0 else MUTED,
        )

        self._set_detail("transaction", self.transaction_id)
        self._set_detail("date_time", f"{date_str} • {time_str}")
        self._set_detail("user", username)
        self._set_detail("item", product_name)
        self._set_detail("type", product_type)
        self._set_detail("price", f"₱{price:.2f}")
        self._set_detail(
            "discount",
            f"{self.discount:.0f}%" if self.discount > 0 else "None",
            SUCCESS if self.discount > 0 else MUTED,
        )
        self._set_detail("payment", mode_of_payment)

        actual_timestamp = self._prepare_receipt_folder(user_id, purchase_dt)

        self.receipt_user_id = str(user_id)
        self.receipt_timestamp = str(actual_timestamp)
        self.receipt_product_id = str(product_id)

        coupon_discount_percent = float(
            config.get("purchase_page", "discount_percent", default=10) or 10
        )

        self._generate_discount_token(
            user_id=user_id,
            discount_percent=coupon_discount_percent,
            receipt_transaction_id=self.transaction_id,
        )

        self.pending_coupon_payload = None
        if self.discount_token:
            self.pending_coupon_payload = {
                "token": self.discount_token,
                "qr_value": self.discount_token,
                "status": "active",
                "reason": "booth_printed_coupon",
                "discount_percent": coupon_discount_percent,
                "issued_at": purchase_dt.isoformat(),
                "expires_at": self.discount_token_expires_at,
            }

        self.coupon_print_status = "pending" if self.discount_token else "token_missing"
        self.coupon_print_error = None if self.discount_token else "Discount token missing."

        self.receipt_data = {
            "transaction_id": self.transaction_id,
            "user": {
                "user_id": str(user_id),
                "username": username,
            },
            "purchase": {
                "date": date_str,
                "time": time_str,
                "timestamp_folder": actual_timestamp,
                "datetime_iso": purchase_dt.isoformat(),
            },
            "product": {
                "name": product_name,
                "product_id": product_id,
                "type": product_type,
                "price": price,
            },
            "amounts": {
                "discount_percent": self.discount,
                "total": self.total,
                "total_paid": self.total_paid,
                "change": self.change,
            },
            "payment": {
                "mode_of_payment": mode_of_payment,
                "payment_method": self.payment_method,
                "online_payment": self.online_payment,
                "payment_session_id": self.payment_session_id,
                "payment_reference": self.payment_reference,
                "payment_amount": self.payment_amount,
                "payment_mode": self.payment_mode,
                "simulated": self.simulated,
            },
            "coupon_print": {
                "status": self.coupon_print_status,
                "token": self.discount_token,
                "attempted_at": None,
                "completed_at": None,
                "error": self.coupon_print_error,
                "printer_function_available": print_discount_qr is not None,
            },
            "support": {
                "coupon_report_allowed": self.coupon_print_status != "printed",
                "coupon_report_reason": self.coupon_print_status,
            },
            "saved_paths": {
                "session_dir": str(self.receipt_session_dir),
                "receipt_json": str((self.receipt_session_dir / "receipt.json").resolve()),
            },
        }

        self.receipt_text_content = self._build_receipt_text(
            username=username,
            user_id=user_id,
            date_str=date_str,
            time_str=time_str,
            product_name=product_name,
            product_id=product_id,
            product_type=product_type,
            price=price,
            mode_of_payment=mode_of_payment,
        )
        self.receipt_text.configure(text=self.receipt_text_content)

        self._save_receipt_json()
        self._start_printing(run_id)

    def _show_empty_receipt(self):
        self.subtitle_label.configure(
            text="Waiting for transaction details",
            text_color=MUTED,
        )

        self.total_amount_value.configure(text="₱0.00", text_color=BLACK)
        self.total_paid_block["value"].configure(text="₱0.00", text_color=BLACK)
        self.change_block["value"].configure(text="₱0.00", text_color=MUTED)

        self._set_detail("transaction", "—")
        self._set_detail("date_time", "—")
        self._set_detail("user", "—")
        self._set_detail("item", "—")
        self._set_detail("type", "—")
        self._set_detail("price", "₱0.00")
        self._set_detail("discount", "None", MUTED)
        self._set_detail("payment", "—")

    def _number(self, value, fallback=0.0):
        try:
            if value is None or value == "":
                return float(fallback or 0)

            if isinstance(value, str):
                cleaned = (
                    value.replace("₱", "")
                    .replace("PHP", "")
                    .replace(",", "")
                    .replace(" ", "")
                    .strip()
                )

                if cleaned == "":
                    return float(fallback or 0)

                return float(cleaned)

            return float(value or 0)

        except Exception:
            return float(fallback or 0)

    def _prepare_receipt_folder(self, user_id, purchase_dt):
        actual_timestamp = purchase_dt.strftime("%Y%m%d_%H%M%S")

        if get_or_create_capture_session is not None:
            try:
                self.receipt_session_dir = get_or_create_capture_session(str(user_id))

                if remember_capture_session is not None:
                    remember_capture_session(str(user_id), self.receipt_session_dir)

                if get_session_timestamp is not None:
                    actual_timestamp = get_session_timestamp(self.receipt_session_dir)

                return actual_timestamp

            except Exception as e:
                print(f"[RECEIPT] Capture session failed: {e}", flush=True)

        self.receipt_session_dir = CAPTURES_DIR / str(user_id) / actual_timestamp
        self.receipt_session_dir.mkdir(parents=True, exist_ok=True)
        return actual_timestamp

    def _build_receipt_text(
        self,
        username,
        user_id,
        date_str,
        time_str,
        product_name,
        product_id,
        product_type,
        price,
        mode_of_payment,
    ):
        return (
            f"Transaction ID: {self.transaction_id}\n\n"
            f"Username: {username}\n\n"
            f"User ID: {user_id}\n\n"
            f"Date of Purchase: {date_str}\n\n"
            f"Time of Purchase: {time_str}\n\n"
            f"Item Name: {product_name}\n\n"
            f"Product ID: {product_id}\n\n"
            f"Product Type: {product_type}\n\n"
            f"Price: ₱{price:.2f}\n\n"
            f"Discount: {self.discount:.2f}%\n\n"
            f"Total: ₱{self.total:.2f}\n\n"
            f"Total Paid: ₱{self.total_paid:.2f}\n\n"
            f"Change: ₱{self.change:.2f}\n\n"
            f"Mode of Payment: {mode_of_payment}\n\n"
        )

    def _save_receipt_json(self):
        if not self.receipt_session_dir or not self.receipt_data:
            return

        try:
            if save_receipt_json is not None:
                save_receipt_json(self.receipt_session_dir, self.receipt_data)
            else:
                receipt_path = self.receipt_session_dir / "receipt.json"

                with receipt_path.open("w", encoding="utf-8") as file:
                    json.dump(self.receipt_data, file, indent=2, ensure_ascii=False)

            print(f"[RECEIPT] Receipt JSON saved: {self.receipt_session_dir}", flush=True)

        except Exception as e:
            print(f"[RECEIPT] Failed to save receipt JSON: {e}", flush=True)

    def _maybe_start_website_sync(self, run_id):
        if not self._is_current_run(run_id):
            return

        if self._sync_started_for_run_id == run_id:
            return

        self._sync_started_for_run_id = run_id

        if self.receipt_user_id and self.receipt_timestamp and self.receipt_product_id:
            self._start_website_sync(
                self.receipt_user_id,
                self.receipt_timestamp,
                self.receipt_product_id,
                run_id,
            )

    def _start_website_sync(self, user_id, timestamp, product_id, run_id):
        if sync_receipt_and_images is None:
            return

        if not self.receipt_data or not self.receipt_session_dir:
            return

        if not self._is_current_run(run_id):
            return

        try:
            self.sync_in_progress = True

            threading.Thread(
                target=self._sync_to_website,
                args=(
                    str(user_id),
                    str(timestamp),
                    self.receipt_data.copy(),
                    str(self.receipt_session_dir),
                    str(product_id),
                    str(self.transaction_id),
                    run_id,
                ),
                daemon=True,
            ).start()

        except Exception as e:
            self.sync_in_progress = False
            print(f"[RECEIPT] Failed to start website sync: {e}", flush=True)

    def _sync_to_website(
        self,
        user_id,
        timestamp,
        receipt_data,
        session_dir,
        product_id,
        transaction_id,
        run_id,
    ):
        try:
            result = sync_receipt_and_images(
                user_id=user_id,
                timestamp=timestamp,
                receipt_data=receipt_data,
                session_dir=session_dir,
                product_id=product_id,
                transaction_id=transaction_id,
            )

            if self._is_current_run(run_id):
                self.sync_result = result or {}
                self.sync_in_progress = False

            print(f"[RECEIPT] Sync finished: {result or {}}", flush=True)

        except Exception as e:
            if self._is_current_run(run_id):
                self.sync_in_progress = False
            print(f"[RECEIPT] Sync thread failed: {e}", flush=True)

    def _on_sync_finished(self, result):
        self.sync_in_progress = False
        self.sync_result = result or {}
        print(f"[RECEIPT] Sync finished: {self.sync_result}", flush=True)

    def _on_sync_failed(self, error_text):
        self.sync_in_progress = False
        print(f"[RECEIPT] Website sync failed: {error_text}", flush=True)

    def _generate_discount_token(
        self,
        user_id,
        discount_percent=10,
        receipt_transaction_id=None,
    ):
        self.discount_token = None
        self.discount_token_expires_at = None

        if generate_token is None:
            print("[RECEIPT] generate_token unavailable.", flush=True)
            return

        try:
            valid_days = int(
                config.get("receipt_page", "coupon_valid_days", default=90) or 90
            )
            expires_at = datetime.now() + timedelta(days=valid_days)

            self.discount_token = generate_token(user_id)
            self.discount_token_expires_at = expires_at.isoformat()

            # A failed coupon upload should not stop dispensing.
            store_res = api_client.store_qr_token(
                user_id=user_id,
                token=self.discount_token,
                discount_percent=discount_percent,
                source="booth_printed_coupon",
                receipt_transaction_id=receipt_transaction_id or self.transaction_id,
                expires_at=self.discount_token_expires_at,
            )

            if not getattr(store_res, "ok", False):
                print(
                    f"[RECEIPT] Failed to store discount token. "
                    f"status={getattr(store_res, 'status_code', None)} "
                    f"text={getattr(store_res, 'text', '')}",
                    flush=True,
                )
                self.discount_token = None
                self.discount_token_expires_at = None

        except Exception as e:
            self.discount_token = None
            self.discount_token_expires_at = None
            print(f"[RECEIPT] Failed to generate/store token: {e}", flush=True)

    def _start_printing(self, run_id):
        if not self._is_current_run(run_id):
            return

        if self.printing_in_progress:
            return

        self.printing_in_progress = True
        self.print_success = False
        self.print_error = None

        threading.Thread(
            target=self._print_receipt,
            args=(run_id,),
            daemon=True,
        ).start()

        self._start_print_watchdog(run_id)

    def _start_print_watchdog(self, run_id):
        self._cancel_print_watchdog()

        try:
            timeout_ms = int(
                config.get("receipt_page", "print_timeout_ms", default=15000) or 15000
            )
        except Exception:
            timeout_ms = 15000

        timeout_ms = max(3000, timeout_ms)
        self._print_watchdog_job = self.after(
            timeout_ms,
            lambda rid=run_id: self._on_print_timeout(rid),
        )

    def _on_print_timeout(self, run_id):
        self._print_watchdog_job = None

        if not self._is_current_run(run_id):
            return

        if self.redirect_scheduled:
            return

        print(
            "[RECEIPT] Print watchdog timeout reached. Continuing to dispensing.",
            flush=True,
        )

        self.printing_in_progress = False
        self.print_success = False
        self.print_error = "Print timeout. Continuing to dispensing."
        self.coupon_print_status = "timeout"
        self.coupon_print_error = self.print_error

        self._write_print_status_to_receipt(
            status=self.coupon_print_status,
            error_text=self.coupon_print_error,
            completed_at=datetime.now().isoformat(),
        )

        self._show_coupon_print_error_notice(self.coupon_print_error)
        self._maybe_start_website_sync(run_id)

        self._schedule_redirect(
            run_id,
            force=True,
            delay_ms=int(
                config.get(
                    "receipt_page",
                    "coupon_error_notice_delay_ms",
                    default=4500,
                )
                or 4500
            ),
        )

    def _print_receipt(self, run_id):
        attempted_at = datetime.now().isoformat()

        try:
            if not self._is_current_run(run_id):
                return

            self._write_print_status_to_receipt(
                status="initializing",
                error_text=None,
                attempted_at=attempted_at,
            )

            init_delay_ms = int(
                config.get("receipt_page", "print_init_delay_ms", default=1200) or 1200
            )
            if init_delay_ms > 0:
                time_to_wait = max(0.0, init_delay_ms / 1000.0)
                threading.Event().wait(time_to_wait)

            if not self._is_current_run(run_id):
                return

            self._write_print_status_to_receipt(
                status="printing",
                error_text=None,
                attempted_at=attempted_at,
            )

            if print_discount_qr is None:
                self.print_success = False
                self.print_error = "Printer function unavailable."
                return

            if not self.discount_token:
                self.print_success = False
                self.print_error = "Discount token missing or failed to store online."
                return

            result = print_discount_qr(self.discount_token)

            if not self._is_current_run(run_id):
                return

            if result is True:
                self.print_success = True
                self.print_error = None
            else:
                self.print_success = False
                self.print_error = "Printer did not complete the print job."

        except Exception as e:
            if self._is_current_run(run_id):
                self.print_success = False
                self.print_error = str(e)
            print(f"[RECEIPT] Failed to print QR coupon: {e}", flush=True)

        finally:
            if self._is_current_run(run_id):
                self.printing_in_progress = False
                try:
                    self.after(0, lambda rid=run_id: self._on_print_finished(rid))
                except Exception:
                    pass

    def _write_print_status_to_receipt(
        self,
        status,
        error_text=None,
        attempted_at=None,
        completed_at=None,
    ):
        if self.receipt_data is None:
            return

        existing_attempted_at = None
        try:
            existing_attempted_at = self.receipt_data.get("coupon_print", {}).get("attempted_at")
        except Exception:
            existing_attempted_at = None

        self.receipt_data.setdefault("coupon_print", {})
        self.receipt_data["coupon_print"].update({
            "status": status,
            "token": self.discount_token,
            "attempted_at": attempted_at if attempted_at is not None else existing_attempted_at,
            "completed_at": completed_at,
            "error": error_text,
            "printer_function_available": print_discount_qr is not None,
        })

        self.receipt_data.setdefault("support", {})
        self.receipt_data["support"].update({
            "coupon_report_allowed": status != "printed",
            "coupon_report_reason": status,
        })

        self._save_receipt_json()

    def _on_print_finished(self, run_id):
        if not self._is_current_run(run_id):
            return

        self._cancel_print_watchdog()

        completed_at = datetime.now().isoformat()

        if self.print_success:
            self.coupon_print_status = "printed"
            self.coupon_print_error = None
            print("[RECEIPT] Coupon print complete. Redirecting to dispensing.", flush=True)
        else:
            if print_discount_qr is None:
                self.coupon_print_status = "printer_unavailable"
            elif not self.discount_token:
                self.coupon_print_status = "token_missing"
            else:
                self.coupon_print_status = "failed"

            self.coupon_print_error = self.print_error or "Unknown printer error"

            print(
                f"[RECEIPT] Coupon print failed: {self.coupon_print_error}. "
                f"Redirecting to dispensing anyway.",
                flush=True,
            )

            self._show_coupon_print_error_notice(self.coupon_print_error)

        if self.receipt_data is not None:
            if self.print_success and self.pending_coupon_payload:
                self.receipt_data["coupon"] = self.pending_coupon_payload
            else:
                self.receipt_data.pop("coupon", None)

            self._write_print_status_to_receipt(
                status=self.coupon_print_status,
                error_text=self.coupon_print_error,
                completed_at=completed_at,
            )

        self._maybe_start_website_sync(run_id)

        redirect_delay_ms = 0

        if not self.print_success:
            redirect_delay_ms = int(
                config.get(
                    "receipt_page",
                    "coupon_error_notice_delay_ms",
                    default=4500,
                )
                or 4500
            )

        self._schedule_redirect(
            run_id,
            force=False,
            delay_ms=redirect_delay_ms,
        )

    def _show_coupon_print_error_notice(self, error_text=""):
        friendly_message = (
            "Discount coupon could not be printed. "
            "Your test kit will still be dispensed. "
            "You may still use the discount QR code generated on the website "
            "for your next purchase."
        )

        try:
            self.title_label.configure(
                text="Discount Coupon Notice",
                text_color=ERROR,
            )

            self.subtitle_label.configure(
                text=friendly_message,
                text_color=ERROR,
            )
        except Exception:
            pass

        try:
            self._set_detail(
                "discount",
                "Use the website discount QR",
                ERROR,
            )
        except Exception:
            pass

        try:
            self._write_print_status_to_receipt(
                status=self.coupon_print_status or "failed",
                error_text=error_text or self.coupon_print_error,
                completed_at=datetime.now().isoformat(),
            )
        except Exception:
            pass

    def _schedule_redirect(self, run_id, force=False, delay_ms=None):
        if not self._is_current_run(run_id):
            return

        if self.redirect_scheduled:
            return

        try:
            delay_ms = int(delay_ms or 0)
        except Exception:
            delay_ms = 0

        delay_ms = max(0, delay_ms)

        self.redirect_scheduled = True

        self._redirect_after_job = self.after(
            delay_ms,
            lambda rid=run_id, force_redirect=force: self._redirect_to_dispensing(
                rid,
                force=force_redirect,
            ),
        )

    def _redirect_to_dispensing(self, run_id=None, force=False):
        if run_id is None:
            run_id = self._receipt_run_id

        if not self._is_current_run(run_id):
            return

        self._redirect_after_job = None

        if self.printing_in_progress and not force:
            self._redirect_after_job = self.after(
                100,
                lambda rid=run_id: self._redirect_to_dispensing(rid, force=False),
            )
            return

        print("[RECEIPT] Navigating to DispensingPage now.", flush=True)

        try:
            self.controller.show_loading_then(
                config.get(
                    "receipt_page",
                    "dispensing_loading_text",
                    default="Dispensing item",
                ),
                "DispensingPage",
                delay=0,
                user_data=self.user_data,
                product=self.product,
                selected_product=self.product,
                selected_item=self.product,
                discount=self.discount,
                total_paid=self.total_paid,
                change=self.change,
                total=self.total,
                online_payment=self.online_payment,
                payment_method=self.payment_method,
                payment_session_id=self.payment_session_id,
                payment_reference=self.payment_reference,
                payment_amount=self.payment_amount,
                payment_mode=self.payment_mode,
                simulated=self.simulated,
                transaction_id=self.transaction_id,
            )
        except Exception as e:
            print(f"[RECEIPT] show_loading_then failed: {e}", flush=True)
            try:
                self.controller.show_frame(
                    "DispensingPage",
                    user_data=self.user_data,
                    product=self.product,
                    selected_product=self.product,
                    selected_item=self.product,
                    discount=self.discount,
                    total_paid=self.total_paid,
                    change=self.change,
                    total=self.total,
                    online_payment=self.online_payment,
                    payment_method=self.payment_method,
                    payment_session_id=self.payment_session_id,
                    payment_reference=self.payment_reference,
                    payment_amount=self.payment_amount,
                    payment_mode=self.payment_mode,
                    simulated=self.simulated,
                    transaction_id=self.transaction_id,
                )
            except Exception as fallback_error:
                print(f"[RECEIPT] Fallback navigation failed: {fallback_error}", flush=True)

    # ------------------------------------------------------------------
    # Layout / config
    # ------------------------------------------------------------------

    def _sync_layout(self, event=None):
        try:
            self.update_idletasks()

            available_w = max(760, self.content_wrap.winfo_width())
            available_h = max(560, self.content_wrap.winfo_height())

            card_w = min(840, max(700, int(available_w * 0.70)))
            card_h = min(730, max(620, int(available_h * 0.90)))

            self.card.configure(width=card_w, height=card_h)

            wrap = max(420, card_w - 170)
            amount_wrap = max(150, (card_w - 170) // 2)

            self.total_amount_value.configure(wraplength=max(300, card_w - 160))
            self.subtitle_label.configure(wraplength=max(300, card_w - 170))

            for row in self.detail_rows.values():
                row["widget"].configure(wraplength=wrap)

            self.total_paid_block["value"].configure(wraplength=amount_wrap)
            self.change_block["value"].configure(wraplength=amount_wrap)

        except Exception:
            pass

    def _refresh_from_config(self):
        try:
            self.page_title.configure(
                text=config.get("receipt_page", "title", default="Receipt"),
            )
        except Exception as e:
            print(f"[RECEIPT] Config refresh failed: {e}", flush=True)

    def _start_config_refresh(self):
        self._refresh_from_config()
        self._config_refresh_job = self.after(self.REFRESH_MS, self._start_config_refresh)

    def destroy(self):
        if self._config_refresh_job is not None:
            try:
                self.after_cancel(self._config_refresh_job)
            except Exception:
                pass

            self._config_refresh_job = None

        self._cancel_redirect_job()
        self._cancel_print_watchdog()

        super().destroy()