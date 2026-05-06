from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, PillButton, card_body
from backend.util import api_client
from config_manager import config

import threading
import qrcode

try:
    from PIL import ImageTk
except Exception:
    ImageTk = None


# ---------------------------------------------------------------------
# Theme-safe helpers
# ---------------------------------------------------------------------

def _theme(name, fallback):
    return getattr(theme, name, fallback)


BLACK = _theme("BLACK", "#000000")
CREAM = _theme("CREAM", "#F5F2DE")
ORANGE = _theme("ORANGE", "#C46A2A")
WHITE = _theme("WHITE", "#FFFFFF")
GRAY = _theme("GRAY", "#696969")
TEXT = _theme("TEXT", "#111111")
MUTED = _theme("MUTED", "#555555")
SUCCESS = _theme("SUCCESS", "#237B4B")
ERROR = _theme("ERROR", "#B3261E")
INFO = _theme("INFO", "#2457A5")
BORDER = _theme("BORDER", "#1E1E1E")


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


class OnlinePaymentPage(ctk.CTkFrame):
    REFRESH_MS = 1500

    def __init__(self, master, controller):
        super().__init__(master, fg_color=CREAM)

        self.controller = controller
        self.user_data = {}
        self.selected_product = None
        self.discount = 0

        self.payment_session_id = None
        self.payment_checkout_url = None
        self.payment_reference = None
        self.payment_amount = 0
        self.payment_status = None
        self.payment_mode = "test"
        self.simulated = True
        self.website_transaction_id = None

        self.poll_job = None
        self.redirect_job = None
        self.qr_photo = None

        self.request_in_progress = False
        self.status_request_in_progress = False
        self.redirecting_to_cash = False
        self.finalizing_purchase = False

        self.status_error_count = 0
        self.checkout_request_token = 0
        self.active_request_token = 0
        self._config_refresh_job = None

        self.shell = AppShell(self, title_right="Welcome, User!")
        self.shell.pack(fill="both", expand=True)

        self._build_ui()
        self._start_config_refresh()

    # ---------------------------------------------------------------------
    # UI
    # ---------------------------------------------------------------------

    def _build_ui(self):
        self.page = ctk.CTkFrame(
            self.shell.body,
            fg_color=CREAM
        )
        self.page.pack(fill="both", expand=True)

        self.page.grid_columnconfigure(0, weight=1)
        self.page.grid_rowconfigure(0, weight=0)
        self.page.grid_rowconfigure(1, weight=1)

        self._build_top_bar()
        self._build_main_content()

        self.bind("<Configure>", self._on_resize, add="+")
        self.page.bind("<Configure>", self._on_resize, add="+")
        self.content_wrap.bind("<Configure>", self._on_resize, add="+")

    def _build_top_bar(self):
        self.top_bar = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.top_bar.grid(row=0, column=0, sticky="ew", padx=28, pady=(16, 8))

        self.top_bar.grid_columnconfigure(0, weight=0)
        self.top_bar.grid_columnconfigure(1, weight=1)
        self.top_bar.grid_columnconfigure(2, weight=0)

        self.back_btn = PillButton(
            self.top_bar,
            text=config.get("online_payment_page", "back_button_text", default="Back"),
            width=130,
            height=54,
            command=self.go_back,
            font=app_font(17, "bold")
        )
        self.back_btn.grid(row=0, column=0, sticky="w", padx=(0, 18))

        self.title_wrap = ctk.CTkFrame(
            self.top_bar,
            fg_color=CREAM
        )
        self.title_wrap.grid(row=0, column=1, sticky="ew")
        self.title_wrap.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.title_wrap,
            text=config.get("online_payment_page", "title", default="ONLINE PAYMENT"),
            font=app_heavy(34),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=850
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        self.subtitle_label = ctk.CTkLabel(
            self.title_wrap,
            text="Scan the QR and wait for confirmation.",
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=820
        )
        self.subtitle_label.grid(row=1, column=0, sticky="ew", pady=(3, 0))

        self.top_spacer = ctk.CTkFrame(
            self.top_bar,
            fg_color=CREAM,
            width=148,
            height=54
        )
        self.top_spacer.grid(row=0, column=2, sticky="e")

    def _build_main_content(self):
        self.content_wrap = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.content_wrap.grid(row=1, column=0, sticky="nsew", padx=28, pady=(6, 20))

        self.content_wrap.grid_columnconfigure(0, weight=1)
        self.content_wrap.grid_rowconfigure(0, weight=1)

        self.main_card = RoundedCard(
            self.content_wrap,
            fg_color=WHITE,
            radius=36,
            auto_size=False,
            pad=0,
            width=1180,
            height=620
        )
        self.main_card.grid(row=0, column=0, sticky="nsew")

        body = card_body(self.main_card)
        safe_configure(body, fg_color=WHITE)

        body.grid_columnconfigure(0, weight=7, uniform="online-payment")
        body.grid_columnconfigure(1, weight=5, uniform="online-payment")
        body.grid_rowconfigure(0, weight=1)

        self.left_panel = ctk.CTkFrame(
            body,
            fg_color=WHITE
        )
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(30, 15), pady=28)

        self.right_panel = ctk.CTkFrame(
            body,
            fg_color=WHITE
        )
        self.right_panel.grid(row=0, column=1, sticky="nsew", padx=(15, 30), pady=28)

        self._build_qr_panel()
        self._build_info_panel()

    def _build_qr_panel(self):
        self.left_panel.grid_columnconfigure(0, weight=1)
        self.left_panel.grid_rowconfigure(0, weight=0)
        self.left_panel.grid_rowconfigure(1, weight=1)
        self.left_panel.grid_rowconfigure(2, weight=0)

        self.qr_header = ctk.CTkFrame(
            self.left_panel,
            fg_color=WHITE
        )
        self.qr_header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.qr_header.grid_columnconfigure(0, weight=1)
        self.qr_header.grid_columnconfigure(1, weight=0)

        self.qr_title_stack = ctk.CTkFrame(
            self.qr_header,
            fg_color=WHITE
        )
        self.qr_title_stack.grid(row=0, column=0, sticky="ew", padx=(0, 16))
        self.qr_title_stack.grid_columnconfigure(0, weight=1)

        self.qr_title = ctk.CTkLabel(
            self.qr_title_stack,
            text="Scan to Pay",
            font=app_heavy(28),
            text_color=BLACK,
            fg_color=WHITE,
            anchor="w",
            justify="left"
        )
        self.qr_title.grid(row=0, column=0, sticky="w")

        self.desc_label = ctk.CTkLabel(
            self.qr_title_stack,
            text=config.get(
                "online_payment_page",
                "description",
                default="Open your e-wallet app and scan the QR code."
            ),
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            wraplength=680,
            justify="left",
            anchor="w"
        )
        self.desc_label.grid(row=1, column=0, sticky="ew", pady=(3, 0))

        self.qr_badge_frame = ctk.CTkFrame(
            self.qr_header,
            fg_color="#EEF4FF",
            corner_radius=999
        )
        self.qr_badge_frame.grid(row=0, column=1, sticky="e")

        self.state_label = ctk.CTkLabel(
            self.qr_badge_frame,
            text="Preparing",
            font=app_font(13, "bold"),
            text_color=INFO,
            fg_color="transparent"
        )
        self.state_label.pack(padx=16, pady=8)

        self.qr_stage = ctk.CTkFrame(
            self.left_panel,
            fg_color=CREAM,
            corner_radius=32
        )
        self.qr_stage.grid(row=1, column=0, sticky="nsew")
        self.qr_stage.grid_columnconfigure(0, weight=1)
        self.qr_stage.grid_rowconfigure(0, weight=1)
        self.qr_stage.grid_rowconfigure(1, weight=0)
        self.qr_stage.grid_rowconfigure(2, weight=1)

        self.qr_frame = ctk.CTkFrame(
            self.qr_stage,
            fg_color=WHITE,
            corner_radius=30,
            border_width=2,
            border_color="#EBD8C6"
        )
        self.qr_frame.grid(row=1, column=0, padx=40, pady=28)
        self.qr_frame.grid_columnconfigure(0, weight=1)

        self.qr_label = ctk.CTkLabel(
            self.qr_frame,
            text=config.get("online_payment_page", "preparing_qr_text", default="Preparing QR..."),
            font=app_font(20, "bold"),
            text_color=MUTED,
            fg_color=WHITE,
            wraplength=430,
            justify="center",
            anchor="center"
        )
        self.qr_label.pack(padx=30, pady=30)

        self.qr_help_card = ctk.CTkFrame(
            self.left_panel,
            fg_color="#FFFDF8",
            corner_radius=24,
            border_width=1,
            border_color="#EBD8C6"
        )
        self.qr_help_card.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        self.qr_help_card.grid_columnconfigure(0, weight=1)

        self.qr_help_label = ctk.CTkLabel(
            self.qr_help_card,
            text="Keep this screen open until payment is confirmed.",
            font=app_font(15, "bold"),
            text_color=MUTED,
            fg_color="#FFFDF8",
            wraplength=760,
            justify="center",
            anchor="center"
        )
        self.qr_help_label.grid(row=0, column=0, sticky="ew", padx=24, pady=14)

    def _build_info_panel(self):
        self.right_panel.grid_columnconfigure(0, weight=1)
        self.right_panel.grid_rowconfigure(0, weight=0)
        self.right_panel.grid_rowconfigure(1, weight=0)
        self.right_panel.grid_rowconfigure(2, weight=1)
        self.right_panel.grid_rowconfigure(3, weight=0)

        self.amount_card = ctk.CTkFrame(
            self.right_panel,
            fg_color="#FFF9F4",
            corner_radius=30,
            border_width=1,
            border_color="#F0E1D2"
        )
        self.amount_card.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.amount_card.grid_columnconfigure(0, weight=1)

        self.amount_inner = ctk.CTkFrame(
            self.amount_card,
            fg_color="#FFF9F4"
        )
        self.amount_inner.grid(row=0, column=0, sticky="ew", padx=24, pady=24)
        self.amount_inner.grid_columnconfigure(0, weight=1)

        self.amount_badge = ctk.CTkFrame(
            self.amount_inner,
            fg_color="#FFF2E8",
            corner_radius=999
        )
        self.amount_badge.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self.amount_badge_text = ctk.CTkLabel(
            self.amount_badge,
            text="AMOUNT TO PAY",
            font=app_font(12, "bold"),
            text_color=ORANGE,
            fg_color="transparent"
        )
        self.amount_badge_text.pack(padx=14, pady=5)

        self.amount_value = ctk.CTkLabel(
            self.amount_inner,
            text="₱0.00",
            font=app_heavy(58),
            text_color=BLACK,
            fg_color="#FFF9F4",
            justify="left",
            anchor="w"
        )
        self.amount_value.grid(row=1, column=0, sticky="ew")

        self.amount_caption = ctk.CTkLabel(
            self.amount_inner,
            text="Pay the exact amount shown.",
            font=app_font(13, "bold"),
            text_color=MUTED,
            fg_color="#FFF9F4",
            justify="left",
            anchor="w",
            wraplength=360
        )
        self.amount_caption.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        self.details_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=CREAM,
            corner_radius=26
        )
        self.details_card.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        self.details_card.grid_columnconfigure(0, weight=1)

        self.details_title = ctk.CTkLabel(
            self.details_card,
            text="Payment Details",
            font=app_font(14, "bold"),
            text_color=MUTED,
            fg_color=CREAM,
            justify="center",
            anchor="center"
        )
        self.details_title.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 2))

        self.details_label = ctk.CTkLabel(
            self.details_card,
            text="Creating payment session...",
            font=app_font(18, "bold"),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=380
        )
        self.details_label.grid(row=1, column=0, sticky="ew", padx=22, pady=(0, 18))

        self.status_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=WHITE,
            corner_radius=28,
            border_width=1,
            border_color="#EFE1D4"
        )
        self.status_card.grid(row=2, column=0, sticky="nsew", pady=(0, 16))
        self.status_card.grid_columnconfigure(0, weight=1)
        self.status_card.grid_rowconfigure(0, weight=1)
        self.status_card.grid_rowconfigure(1, weight=0)
        self.status_card.grid_rowconfigure(2, weight=0)
        self.status_card.grid_rowconfigure(3, weight=1)

        self.status_icon = ctk.CTkFrame(
            self.status_card,
            fg_color="#FFF2E8",
            corner_radius=26,
            width=76,
            height=76
        )
        self.status_icon.grid(row=1, column=0, sticky="s", pady=(0, 12))
        self.status_icon.grid_propagate(False)

        self.status_icon_text = ctk.CTkLabel(
            self.status_icon,
            text="QR",
            font=app_font(18, "bold"),
            text_color=ORANGE,
            fg_color="transparent",
            justify="center",
            anchor="center"
        )
        self.status_icon_text.place(relx=0.5, rely=0.5, anchor="center")

        self.status_label = ctk.CTkLabel(
            self.status_card,
            text="Creating payment session...",
            font=app_heavy(24),
            text_color=INFO,
            fg_color=WHITE,
            wraplength=380,
            justify="center",
            anchor="center"
        )
        self.status_label.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 4))

        self.status_hint = ctk.CTkLabel(
            self.status_card,
            text="This page updates automatically.",
            font=app_font(13, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            wraplength=360,
            justify="center",
            anchor="center"
        )
        self.status_hint.grid(row=3, column=0, sticky="n", padx=26, pady=(0, 0))

        self.action_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=WHITE
        )
        self.action_card.grid(row=3, column=0, sticky="ew")
        self.action_card.grid_columnconfigure(0, weight=1)
        self.action_card.grid_columnconfigure(1, weight=1)

        self.refresh_btn = PillButton(
            self.action_card,
            text=config.get("online_payment_page", "refresh_button_text", default="Refresh"),
            width=190,
            height=58,
            command=self.manual_check_status,
            font=app_font(15, "bold")
        )
        self.refresh_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.cancel_btn = PillButton(
            self.action_card,
            text=config.get("online_payment_page", "cancel_button_text", default="Cancel"),
            width=190,
            height=58,
            command=self.cancel_and_go_back,
            font=app_font(15, "bold")
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew", padx=(8, 0))

    # ---------------------------------------------------------------------
    # UI HELPERS
    # ---------------------------------------------------------------------

    def _set_state(self, text="Waiting", color=None):
        color = color or INFO

        try:
            self.state_label.configure(
                text=text,
                text_color=color
            )

            if color == SUCCESS:
                self.qr_badge_frame.configure(fg_color="#EAF7EF")
            elif color == ERROR:
                self.qr_badge_frame.configure(fg_color="#FFECEC")
            elif color == ORANGE:
                self.qr_badge_frame.configure(fg_color="#FFF2E8")
            else:
                self.qr_badge_frame.configure(fg_color="#EEF4FF")

        except Exception:
            pass

    def _set_amount_display(self, amount=0):
        try:
            self.amount_value.configure(text=f"₱{float(amount or 0):.2f}")
        except Exception:
            pass

    def _compact_payment_details(self, reference=None, amount=None, product=None):
        parts = []

        if self.payment_mode == "test":
            parts.append("Test payment")

        if reference:
            parts.append(f"Ref: {reference}")

        if product:
            parts.append(str(product))

        return "\n".join(parts) if parts else "Payment session ready."

    def _poll_interval_ms(self):
        return int(config.get("online_payment_page", "poll_interval_ms", default=3000))

    def _error_redirect_delay_ms(self):
        return int(config.get("online_payment_page", "error_redirect_delay_ms", default=1800))

    def _qr_size(self):
        configured_size = int(config.get("online_payment_page", "qr_size", default=410))
        return max(configured_size, 410)

    def _max_status_errors(self):
        return int(config.get("online_payment_page", "max_status_errors", default=3))

    def _refresh_from_config(self):
        try:
            self.back_btn.configure(
                text=config.get("online_payment_page", "back_button_text", default="Back")
            )

            self.title_label.configure(
                text=config.get("online_payment_page", "title", default="ONLINE PAYMENT")
            )

            self.desc_label.configure(
                text=config.get(
                    "online_payment_page",
                    "description",
                    default="Open your e-wallet app and scan the QR code."
                )
            )

            self.refresh_btn.configure(
                text=config.get("online_payment_page", "refresh_button_text", default="Refresh")
            )

            self.cancel_btn.configure(
                text=config.get("online_payment_page", "cancel_button_text", default="Cancel")
            )

        except Exception as e:
            print(f"[ONLINE PAYMENT] Config refresh failed: {e}", flush=True)

    def _start_config_refresh(self):
        self._refresh_from_config()
        self._config_refresh_job = self.after(self.REFRESH_MS, self._start_config_refresh)

    def _cancel_config_refresh(self):
        if self._config_refresh_job is not None:
            try:
                self.after_cancel(self._config_refresh_job)
            except Exception:
                pass

            self._config_refresh_job = None

    def _on_resize(self, event=None):
        try:
            self.update_idletasks()

            total_width = max(self.content_wrap.winfo_width(), 900)
            total_height = max(self.content_wrap.winfo_height(), 540)

            card_w = min(1240, max(960, int(total_width * 0.96)))
            card_h = min(700, max(560, int(total_height * 0.96)))

            self.main_card.configure(width=card_w, height=card_h)

            left_wrap = max(420, self.left_panel.winfo_width() - 80)
            right_wrap = max(260, self.right_panel.winfo_width() - 56)

            self.desc_label.configure(wraplength=left_wrap)
            self.qr_label.configure(wraplength=left_wrap)
            self.qr_help_label.configure(wraplength=left_wrap)

            self.amount_caption.configure(wraplength=right_wrap)
            self.details_label.configure(wraplength=right_wrap)
            self.status_label.configure(wraplength=right_wrap)
            self.status_hint.configure(wraplength=right_wrap)

        except Exception:
            pass

    # ---------------------------------------------------------------------
    # STATE
    # ---------------------------------------------------------------------

    def _reset_state(self):
        self.payment_session_id = None
        self.payment_checkout_url = None
        self.payment_reference = None
        self.payment_amount = 0
        self.payment_status = None
        self.payment_mode = "test"
        self.simulated = True
        self.website_transaction_id = None

        self.request_in_progress = False
        self.status_request_in_progress = False
        self.redirecting_to_cash = False
        self.finalizing_purchase = False
        self.status_error_count = 0

        self.refresh_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")
        self.back_btn.configure(state="normal")

        self._set_state("Preparing", INFO)
        self._set_amount_display(0)

    def update_data(self, user_data=None, selected_product=None, discount=0, **kwargs):
        self._stop_polling()
        self._cancel_redirect()

        self.checkout_request_token += 1
        self.active_request_token = self.checkout_request_token

        self._reset_state()

        self.user_data = user_data or {}
        self.selected_product = selected_product or kwargs.get("product")
        self.discount = float(discount or 0)

        self.shell.set_header_right(f"Welcome, {self.user_data.get('username', 'User')}!")

        self._clear_qr_display()
        self.details_label.configure(text="Creating session...")
        self.status_label.configure(
            text=config.get(
                "online_payment_page",
                "creating_session_text",
                default="Creating payment session..."
            ),
            text_color=INFO
        )
        self.status_hint.configure(text="This page updates automatically.")
        self.status_icon_text.configure(text="QR")

        self._set_state("Preparing", INFO)
        self._set_amount_display(self._compute_total_amount())

        self.after(
            150,
            lambda token=self.active_request_token: self.start_online_payment(token)
        )

    def _clear_qr_display(self, placeholder=None):
        self.qr_photo = None

        placeholder = placeholder or config.get(
            "online_payment_page",
            "preparing_qr_text",
            default="Preparing QR..."
        )

        try:
            self.qr_label.image = None
        except Exception:
            pass

        try:
            self.qr_label.configure(image="")
        except Exception:
            pass

        try:
            self.qr_label.configure(text=placeholder)
        except Exception:
            pass

    def _is_stale(self, token):
        return token != self.active_request_token

    def _compute_total_amount(self):
        if not self.selected_product:
            return 0.0

        price = float(self.selected_product.get("price", 0) or 0)
        discount = float(self.discount or 0)
        total = price * (1 - discount / 100.0)
        return round(max(total, 0), 2)

    def _build_transaction_payload(self):
        product_id = (
            self.selected_product.get("productID")
            or self.selected_product.get("product_id")
            or self.selected_product.get("id")
            or ""
        )

        product_name = (
            self.selected_product.get("name")
            or self.selected_product.get("type")
            or "Confidex Kit"
        )

        product_type = self.selected_product.get("type") or ""

        total_amount = self._compute_total_amount()
        user_id = self.user_data.get("_id") or self.user_data.get("userID")

        return {
            "user_id": user_id,
            "status": "completed",
            "purchasedDate": None,
            "items": [
                {
                    "name": product_name,
                    "productID": product_id,
                    "type": product_type,
                    "price": float(self.selected_product.get("price", 0) or 0),
                    "discount": float(self.discount or 0),
                    "finalPrice": total_amount,
                    "result": "Pending",
                }
            ],
        }

    # ---------------------------------------------------------------------
    # CHECKOUT
    # ---------------------------------------------------------------------

    def start_online_payment(self, token):
        if self._is_stale(token):
            return

        if self.request_in_progress or self.redirecting_to_cash or self.finalizing_purchase:
            return

        if not self.selected_product:
            self._redirect_to_cash_with_error(
                config.get("online_payment_page", "no_product_text", default="No product selected.")
            )
            return

        self.request_in_progress = True
        threading.Thread(
            target=self._create_checkout_session,
            args=(token,),
            daemon=True
        ).start()

    def _create_checkout_session(self, token):
        try:
            amount = self._compute_total_amount()

            payload = {
                "userId": self.user_data.get("_id") or self.user_data.get("userID"),
                "username": self.user_data.get("username", "User"),
                "productId": (
                    self.selected_product.get("productID")
                    or self.selected_product.get("product_id")
                    or ""
                ),
                "productName": self.selected_product.get("name") or self.selected_product.get("type") or "Confidex Kit",
                "productType": self.selected_product.get("type") or "",
                "originalPrice": self.selected_product.get("price") or 0,
                "discountPercent": self.discount or 0,
                "amount": amount,
                "currency": "PHP",
            }

            response = api_client.create_paymongo_checkout(payload)
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}

            if not response.ok:
                raise RuntimeError(data.get("error") or "Failed to create PayMongo checkout session.")

            session_id = data.get("sessionId")
            checkout_url = data.get("checkoutUrl")
            reference = data.get("referenceNumber") or data.get("reference") or session_id

            if not session_id or not checkout_url:
                raise RuntimeError("Missing sessionId or checkoutUrl from backend.")

            payment_mode = str(data.get("mode", "test")).lower()
            simulated = bool(data.get("simulated", payment_mode == "test"))

            self.after(
                0,
                lambda: self._on_checkout_created(
                    token=token,
                    session_id=session_id,
                    checkout_url=checkout_url,
                    reference=reference,
                    amount=amount,
                    payment_mode=payment_mode,
                    simulated=simulated
                )
            )

        except Exception as e:
            self.after(0, lambda err=str(e): self._on_checkout_error(token, err))

    def _on_checkout_created(self, token, session_id, checkout_url, reference, amount, payment_mode, simulated):
        if self._is_stale(token) or self.redirecting_to_cash:
            return

        self.request_in_progress = False
        self.status_error_count = 0

        self.payment_session_id = session_id
        self.payment_checkout_url = checkout_url
        self.payment_reference = reference
        self.payment_amount = amount
        self.payment_status = "pending"
        self.payment_mode = payment_mode
        self.simulated = simulated

        try:
            self._render_qr(checkout_url)
        except Exception as e:
            self._redirect_to_cash_with_error(
                f"{config.get('online_payment_page', 'render_qr_error_prefix', default='Unable to render QR code.')} {e}"
            )
            return

        product_label = self.selected_product.get("type") or self.selected_product.get("name") or "Test Kit"

        self._set_amount_display(amount)
        self.details_label.configure(
            text=self._compact_payment_details(
                reference=reference,
                amount=amount,
                product=product_label
            )
        )

        if self.payment_mode == "test":
            self.status_label.configure(
                text=config.get(
                    "online_payment_page",
                    "waiting_simulated_text",
                    default="Waiting for confirmation."
                ),
                text_color=ORANGE
            )
        else:
            self.status_label.configure(
                text=config.get(
                    "online_payment_page",
                    "waiting_payment_text",
                    default="Waiting for payment."
                ),
                text_color=ORANGE
            )

        self.status_hint.configure(text="Keep this screen open.")
        self.status_icon_text.configure(text="...")
        self._set_state("Waiting", ORANGE)

    def _on_checkout_error(self, token, error_message):
        if self._is_stale(token):
            return

        self.request_in_progress = False
        print(f"[PAYMONGO] Checkout creation failed: {error_message}", flush=True)
        self._redirect_to_cash_with_error(error_message)

    def _render_qr(self, text):
        if ImageTk is None:
            raise RuntimeError("Pillow is not installed, so the QR image cannot be displayed.")

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=2
        )
        qr.add_data(text)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        size = self._qr_size()
        img = img.resize((size, size))

        self.qr_photo = ImageTk.PhotoImage(img)

        self.qr_label.configure(text="")
        self.qr_label.configure(image=self.qr_photo)
        self.qr_label.image = self.qr_photo

        self._start_polling()

    # ---------------------------------------------------------------------
    # POLLING
    # ---------------------------------------------------------------------

    def _start_polling(self):
        if self.redirecting_to_cash or self.finalizing_purchase:
            return

        self._stop_polling()
        self.poll_job = self.after(self._poll_interval_ms(), self._poll_status)

    def _stop_polling(self):
        if self.poll_job is not None:
            try:
                self.after_cancel(self.poll_job)
            except Exception:
                pass
            self.poll_job = None

    def _cancel_redirect(self):
        if self.redirect_job is not None:
            try:
                self.after_cancel(self.redirect_job)
            except Exception:
                pass
            self.redirect_job = None

    def _poll_status(self):
        self.poll_job = None

        if (
            not self.payment_session_id
            or self.redirecting_to_cash
            or self.finalizing_purchase
            or self.status_request_in_progress
        ):
            return

        token = self.active_request_token
        self.status_request_in_progress = True

        threading.Thread(
            target=self._fetch_status,
            args=(token, False),
            daemon=True
        ).start()

    def manual_check_status(self):
        if (
            not self.payment_session_id
            or self.redirecting_to_cash
            or self.finalizing_purchase
            or self.status_request_in_progress
        ):
            return

        self.status_label.configure(
            text=config.get("online_payment_page", "checking_status_text", default="Checking status..."),
            text_color=INFO
        )
        self.status_hint.configure(text="Checking payment confirmation.")
        self.status_icon_text.configure(text="↻")
        self._set_state("Checking", INFO)

        token = self.active_request_token
        self.status_request_in_progress = True

        threading.Thread(
            target=self._fetch_status,
            args=(token, True),
            daemon=True
        ).start()

    def _fetch_status(self, token, manual):
        try:
            response = api_client.get_paymongo_checkout_status(self.payment_session_id)
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}

            if not response.ok:
                raise RuntimeError(data.get("error") or "Failed to get payment status.")

            status = str(data.get("status", "pending")).lower()
            paid = bool(data.get("paid", False))
            payment_mode = str(data.get("mode", self.payment_mode)).lower()
            simulated = bool(data.get("simulated", payment_mode == "test"))

            self.after(
                0,
                lambda: self._handle_status(
                    token,
                    status,
                    paid,
                    payment_mode,
                    simulated
                )
            )

        except Exception as e:
            print(f"[PAYMONGO] Status check error: {e}", flush=True)
            self.after(
                0,
                lambda err=str(e): self._handle_status_error(token, err, manual)
            )

    def _handle_status(self, token, status, paid, payment_mode, simulated):
        self.status_request_in_progress = False

        if self._is_stale(token) or self.redirecting_to_cash:
            return

        self.payment_mode = payment_mode
        self.simulated = simulated
        self.payment_status = status
        self.status_error_count = 0

        if paid or status in ("paid", "completed", "succeeded"):
            print("[PAYMONGO] Payment confirmed", flush=True)

            self.status_label.configure(
                text=config.get(
                    "online_payment_page",
                    "payment_confirmed_saving_text",
                    default="Payment confirmed. Saving..."
                ),
                text_color=SUCCESS
            )
            self.status_hint.configure(text="Finalizing your transaction.")
            self.status_icon_text.configure(text="✓")
            self._set_state("Paid", SUCCESS)

            self._stop_polling()
            self.after(300, self.finish_online_payment)
            return

        if status in ("failed", "expired", "cancelled"):
            print(f"[PAYMONGO] Payment ended with status: {status}", flush=True)
            self._redirect_to_cash_with_error(
                f"{config.get('online_payment_page', 'payment_failed_prefix', default='Payment failed:')} {status}"
            )
            return

        if self.payment_mode == "test":
            self.status_label.configure(
                text=config.get(
                    "online_payment_page",
                    "waiting_simulated_text",
                    default="Waiting for confirmation."
                ),
                text_color=ORANGE
            )
        else:
            self.status_label.configure(
                text=config.get(
                    "online_payment_page",
                    "waiting_payment_text",
                    default="Waiting for payment."
                ),
                text_color=ORANGE
            )

        self.status_hint.configure(text="This page updates automatically.")
        self.status_icon_text.configure(text="...")
        self._set_state("Waiting", ORANGE)
        self._start_polling()

    def _handle_status_error(self, token, error_message, manual):
        self.status_request_in_progress = False

        if self._is_stale(token) or self.redirecting_to_cash:
            return

        self.status_error_count += 1

        if self.status_error_count < self._max_status_errors():
            if manual:
                self.status_label.configure(
                    text=config.get(
                        "online_payment_page",
                        "status_check_failed_text",
                        default="Could not check status. Try again."
                    ),
                    text_color=ERROR
                )
                self.status_hint.configure(text="You can refresh again.")
                self.status_icon_text.configure(text="!")
                self._set_state("Retry", ERROR)
            else:
                self.status_label.configure(
                    text=config.get(
                        "online_payment_page",
                        "status_timeout_retrying_text",
                        default="Status check timed out. Retrying..."
                    ),
                    text_color=ERROR
                )
                self.status_hint.configure(text="Checking again shortly.")
                self.status_icon_text.configure(text="↻")
                self._set_state("Retrying", ERROR)
                self._start_polling()
            return

        self._redirect_to_cash_with_error(
            config.get(
                "online_payment_page",
                "status_failed_repeatedly_prefix",
                default="Status check failed repeatedly."
            )
        )

    # ---------------------------------------------------------------------
    # FALLBACK / REDIRECT
    # ---------------------------------------------------------------------

    def _redirect_to_cash_with_error(self, error_message):
        if self.redirecting_to_cash:
            return

        self.redirecting_to_cash = True
        self.request_in_progress = False
        self.status_request_in_progress = False
        self.finalizing_purchase = False

        self._stop_polling()
        self._cancel_redirect()

        self.status_label.configure(
            text=config.get(
                "online_payment_page",
                "redirecting_cash_suffix",
                default="Online payment unavailable. Redirecting to cash."
            ),
            text_color=ERROR
        )
        self.status_hint.configure(text="Cash payment will open next.")
        self.status_icon_text.configure(text="!")
        self._set_state("Unavailable", ERROR)

        self._clear_qr_display(
            config.get("online_payment_page", "unavailable_qr_text", default="QR unavailable.")
        )

        self.details_label.configure(text="Please use cash payment.")

        self.refresh_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")

        if hasattr(self.controller, "show_error"):
            self.controller.show_error(
                f"Online payment failed.\n{error_message}",
                title="Online Payment Error"
            )

        self.redirect_job = self.after(
            self._error_redirect_delay_ms(),
            self._go_to_cash_payment
        )

    def _go_to_cash_payment(self):
        self.redirect_job = None

        self.controller.show_loading_then(
            config.get(
                "online_payment_page",
                "cash_redirect_loading_text",
                default="Redirecting to cash payment"
            ),
            "CashPaymentPage",
            delay=1000,
            user_data=self.user_data,
            selected_product=self.selected_product,
            discount=self.discount
        )

    # ---------------------------------------------------------------------
    # FINISH PAYMENT
    # ---------------------------------------------------------------------

    def finish_online_payment(self):
        if self.finalizing_purchase:
            return

        self.finalizing_purchase = True

        self.refresh_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")

        self.status_label.configure(
            text=config.get(
                "online_payment_page",
                "payment_confirmed_saving_text",
                default="Payment confirmed. Saving..."
            ),
            text_color=SUCCESS
        )
        self.status_hint.configure(text="Please wait.")
        self.status_icon_text.configure(text="✓")
        self._set_state("Saving", SUCCESS)

        token = self.active_request_token

        threading.Thread(
            target=self._post_transaction_and_continue,
            args=(token,),
            daemon=True
        ).start()

    def _post_transaction_and_continue(self, token):
        try:
            payload = self._build_transaction_payload()
            response = api_client.post_transaction(payload)

            if not response.ok:
                error_text = "Failed to save transaction."

                try:
                    data = response.json()
                    error_text = data.get("error") or error_text
                except Exception:
                    pass

                raise RuntimeError(error_text)

            website_transaction_id = None

            try:
                data = response.json()
                transaction_obj = data.get("transaction") or {}

                website_transaction_id = (
                    transaction_obj.get("_id")
                    or data.get("_id")
                    or data.get("transaction_id")
                    or data.get("id")
                )
            except Exception:
                pass

            self.after(
                0,
                lambda: self._on_transaction_saved(
                    token,
                    website_transaction_id
                )
            )

        except Exception as e:
            self.after(
                0,
                lambda err=str(e): self._on_transaction_save_failed(token, err)
            )

    def _on_transaction_saved(self, token, website_transaction_id=None):
        if self._is_stale(token) or self.redirecting_to_cash:
            return

        self.website_transaction_id = website_transaction_id

        print("[PAYMONGO] Transaction saved", flush=True)

        self.status_label.configure(
            text=config.get(
                "online_payment_page",
                "transaction_saved_text",
                default="Payment saved. Generating receipt..."
            ),
            text_color=SUCCESS
        )
        self.status_hint.configure(text="Opening receipt.")
        self.status_icon_text.configure(text="✓")
        self._set_state("Saved", SUCCESS)

        self.controller.show_loading_then(
            config.get(
                "online_payment_page",
                "receipt_loading_text",
                default="Payment confirmed. Generating receipt"
            ),
            "ReceiptPage",
            delay=800,
            user_data=self.user_data,
            product=self.selected_product,
            discount=self.discount,
            total_paid=self.payment_amount,
            change=0,
            total=self.payment_amount,
            online_payment=True,
            payment_method="paymongo",
            payment_session_id=self.payment_session_id,
            payment_reference=self.payment_reference,
            payment_amount=self.payment_amount,
            payment_mode=self.payment_mode,
            simulated=self.simulated,
            transaction_id=website_transaction_id
        )

    def _on_transaction_save_failed(self, token, error_message):
        if self._is_stale(token) or self.redirecting_to_cash:
            return

        self.finalizing_purchase = False

        self.refresh_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")
        self.back_btn.configure(state="normal")

        self.status_label.configure(
            text=config.get(
                "online_payment_page",
                "save_failed_prefix",
                default="Payment confirmed, but saving failed."
            ),
            text_color=ERROR
        )
        self.status_hint.configure(text="Please try again or ask for assistance.")
        self.status_icon_text.configure(text="!")
        self._set_state("Error", ERROR)

        if hasattr(self.controller, "show_error"):
            self.controller.show_error(
                f"Payment was confirmed, but saving the transaction failed.\n{error_message}",
                title="Transaction Save Error"
            )

    # ---------------------------------------------------------------------
    # NAVIGATION
    # ---------------------------------------------------------------------

    def go_back(self):
        if self.request_in_progress or self.status_request_in_progress or self.finalizing_purchase:
            return

        self._stop_polling()
        self._cancel_redirect()
        self._reset_state()

        self.controller.show_loading_then(
            config.get(
                "online_payment_page",
                "back_loading_text",
                default="Returning to payment options"
            ),
            "PaymentMethodPage",
            delay=1000,
            user_data=self.user_data,
            selected_product=self.selected_product,
            discount=self.discount
        )

    def cancel_and_go_back(self):
        self.go_back()

    def destroy(self):
        self._stop_polling()
        self._cancel_redirect()
        self._cancel_config_refresh()
        super().destroy()