import threading
import tkinter as tk

from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, card_body
from backend.util.dispenser_serial import (
    send_dispense_command,
    send_return_kit_home_command,
)
from config_manager import config
from backend.device_sync import mark_inventory_dirty, push_inventory_if_dirty


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


class DispensingPage(ctk.CTkFrame):
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
        self.transaction_id = None

        self.processing = False

        self._anim_job = None
        self._anim_running = False
        self._dot_count = 0
        self._base_text = config.get(
            "dispensing_page",
            "message_text",
            default="Dispensing your item"
        )
        self._config_refresh_job = None

        self.shell = AppShell(
            self,
            title_right=config.get("dispensing_page", "header_title", default="Dispensing Item")
        )
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

        self._build_top_area()
        self._build_main_content()

        self.bind("<Configure>", self._sync_layout, add="+")
        self.page.bind("<Configure>", self._sync_layout, add="+")
        self.content_wrap.bind("<Configure>", self._sync_layout, add="+")

    def _build_top_area(self):
        self.top_area = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.top_area.grid(row=0, column=0, sticky="ew", padx=28, pady=(18, 12))
        self.top_area.grid_columnconfigure(0, weight=1)

        self.page_title = ctk.CTkLabel(
            self.top_area,
            text=config.get("dispensing_page", "title", default="DISPENSING ITEM"),
            font=app_heavy(34),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=900
        )
        self.page_title.grid(row=0, column=0, sticky="ew")

    def _build_main_content(self):
        self.content_wrap = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.content_wrap.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 20))
        self.content_wrap.grid_columnconfigure(0, weight=1)
        self.content_wrap.grid_rowconfigure(0, weight=1)

        self.card = RoundedCard(
            self.content_wrap,
            fg_color=WHITE,
            radius=36,
            auto_size=False,
            pad=0,
            width=1180,
            height=610
        )
        self.card.grid(row=0, column=0, sticky="nsew")

        body = card_body(self.card)
        safe_configure(body, fg_color=WHITE)

        body.grid_columnconfigure(0, weight=8, uniform="dispensing-layout")
        body.grid_columnconfigure(1, weight=4, uniform="dispensing-layout")
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

        self._build_left_panel()
        self._build_right_panel()

    def _build_left_panel(self):
        self.left_panel.grid_columnconfigure(0, weight=1)
        self.left_panel.grid_rowconfigure(0, weight=0)
        self.left_panel.grid_rowconfigure(1, weight=1)

        self.header_row = ctk.CTkFrame(
            self.left_panel,
            fg_color=WHITE
        )
        self.header_row.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.header_row.grid_columnconfigure(0, weight=1)
        self.header_row.grid_columnconfigure(1, weight=0)

        self.header_text_wrap = ctk.CTkFrame(
            self.header_row,
            fg_color=WHITE
        )
        self.header_text_wrap.grid(row=0, column=0, sticky="ew", padx=(0, 16))
        self.header_text_wrap.grid_columnconfigure(0, weight=1)

        self.eyebrow_label = ctk.CTkLabel(
            self.header_text_wrap,
            text="Selected item",
            font=app_font(13, "bold"),
            text_color=ORANGE,
            fg_color=WHITE,
            justify="left",
            anchor="w"
        )
        self.eyebrow_label.grid(row=0, column=0, sticky="w")

        self.item_name_label = ctk.CTkLabel(
            self.header_text_wrap,
            text="Selected item",
            font=app_heavy(28),
            text_color=BLACK,
            fg_color=WHITE,
            justify="left",
            anchor="w",
            wraplength=720
        )
        self.item_name_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.state_pill = ctk.CTkFrame(
            self.header_row,
            fg_color="#EEF4FF",
            corner_radius=999
        )
        self.state_pill.grid(row=0, column=1, sticky="e")

        self.state_label = ctk.CTkLabel(
            self.state_pill,
            text="Dispensing",
            font=app_font(14, "bold"),
            text_color=INFO,
            fg_color="transparent"
        )
        self.state_label.pack(padx=18, pady=8)

        self.hero_panel = ctk.CTkFrame(
            self.left_panel,
            fg_color=CREAM,
            corner_radius=32
        )
        self.hero_panel.grid(row=1, column=0, sticky="nsew")
        self.hero_panel.grid_columnconfigure(0, weight=1)

        # Center the actual content vertically.
        self.hero_panel.grid_rowconfigure(0, weight=1)
        self.hero_panel.grid_rowconfigure(1, weight=0)
        self.hero_panel.grid_rowconfigure(2, weight=0)
        self.hero_panel.grid_rowconfigure(3, weight=1)

        self.icon_canvas = tk.Canvas(
            self.hero_panel,
            width=250,
            height=200,
            bg=CREAM,
            highlightthickness=0,
            bd=0
        )
        self.icon_canvas.grid(row=1, column=0, sticky="s", padx=24, pady=(0, 18))
        self.icon_canvas.bind("<Configure>", lambda event: self._draw_dispensing_icon())
        self._draw_dispensing_icon()

        self.message_label = ctk.CTkLabel(
            self.hero_panel,
            text=self._base_text,
            font=app_heavy(44),
            text_color=INFO,
            fg_color=CREAM,
            wraplength=820,
            justify="center",
            anchor="center"
        )
        self.message_label.grid(row=2, column=0, sticky="ew", padx=44, pady=(0, 0))

        # Hidden compatibility labels.
        # These are kept so existing backend/status code can still configure them safely,
        # but they are not displayed on the UI.
        self.short_note_label = ctk.CTkLabel(
            self.hero_panel,
            text="",
            font=app_font(1, "normal"),
            text_color=CREAM,
            fg_color=CREAM,
            wraplength=1
        )
        self.short_note_label.grid(row=99, column=0)
        self.short_note_label.grid_remove()

        self.status_panel = ctk.CTkFrame(
            self.left_panel,
            fg_color=WHITE
        )
        self.status_panel.grid(row=99, column=0)
        self.status_panel.grid_remove()

        self.status_label = ctk.CTkLabel(
            self.status_panel,
            text="",
            font=app_font(1, "normal"),
            text_color=WHITE,
            fg_color=WHITE,
            wraplength=1
        )
        self.status_label.pack()
        self.status_label.pack_forget()

    def _build_right_panel(self):
        self.right_panel.grid_columnconfigure(0, weight=1)
        self.right_panel.grid_rowconfigure(0, weight=0)
        self.right_panel.grid_rowconfigure(1, weight=1)

        self.info_card = ctk.CTkFrame(
            self.right_panel,
            fg_color="#FFF9F4",
            corner_radius=30,
            border_width=1,
            border_color="#F0E1D2"
        )
        self.info_card.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.info_card.grid_columnconfigure(0, weight=1)

        self.info_inner = ctk.CTkFrame(
            self.info_card,
            fg_color="#FFF9F4"
        )
        self.info_inner.grid(row=0, column=0, sticky="ew", padx=24, pady=24)
        self.info_inner.grid_columnconfigure(0, weight=1)

        self.info_badge = ctk.CTkFrame(
            self.info_inner,
            fg_color="#FFF2E8",
            corner_radius=999
        )
        self.info_badge.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self.info_badge_label = ctk.CTkLabel(
            self.info_badge,
            text="AUTOMATIC",
            font=app_font(12, "bold"),
            text_color=ORANGE,
            fg_color="transparent"
        )
        self.info_badge_label.pack(padx=14, pady=5)

        self.info_title = ctk.CTkLabel(
            self.info_inner,
            text="Please wait",
            font=app_heavy(30),
            text_color=BLACK,
            fg_color="#FFF9F4",
            justify="left",
            anchor="w",
            wraplength=360
        )
        self.info_title.grid(row=1, column=0, sticky="ew")

        self.dispenser_note_label = ctk.CTkLabel(
            self.info_inner,
            text="Your item is being released.",
            font=app_font(15, "bold"),
            text_color=MUTED,
            fg_color="#FFF9F4",
            justify="left",
            anchor="w",
            wraplength=360
        )
        self.dispenser_note_label.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        self.center_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=CREAM,
            corner_radius=30
        )
        self.center_card.grid(row=1, column=0, sticky="nsew")
        self.center_card.grid_columnconfigure(0, weight=1)

        # No image here. Text is centered vertically.
        self.center_card.grid_rowconfigure(0, weight=1)
        self.center_card.grid_rowconfigure(1, weight=0)
        self.center_card.grid_rowconfigure(2, weight=0)
        self.center_card.grid_rowconfigure(3, weight=1)

        self.center_title = ctk.CTkLabel(
            self.center_card,
            text="No action needed",
            font=app_heavy(30),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=360
        )
        self.center_title.grid(row=1, column=0, sticky="ew", padx=32, pady=(0, 8))

        self.center_note = ctk.CTkLabel(
            self.center_card,
            text="The next screen will open automatically.",
            font=app_font(15, "bold"),
            text_color=MUTED,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=360
        )
        self.center_note.grid(row=2, column=0, sticky="ew", padx=32, pady=(0, 0))

        # Hidden compatibility widgets.
        self.hidden_compat = ctk.CTkFrame(
            self.right_panel,
            fg_color=WHITE
        )
        self.hidden_compat.grid(row=99, column=0)
        self.hidden_compat.grid_remove()

        self.bottom_card = ctk.CTkFrame(
            self.hidden_compat,
            fg_color=WHITE
        )

        self.bottom_label = ctk.CTkLabel(
            self.hidden_compat,
            text="",
            font=app_font(1, "normal"),
            text_color=WHITE,
            fg_color=WHITE,
            wraplength=1
        )

        self.item_code_chip = self._make_hidden_chip("Item Code")
        self.transaction_chip = self._make_hidden_chip("Transaction")

        self.details_label = ctk.CTkLabel(
            self.hidden_compat,
            text="",
            font=app_font(1, "bold"),
            text_color=WHITE,
            fg_color=WHITE,
            wraplength=1,
            justify="center"
        )
        self.details_label.pack()
        self.details_label.pack_forget()

    # ---------------------------------------------------------------------
    # UI helpers
    # ---------------------------------------------------------------------

    def _make_hidden_chip(self, label):
        frame = ctk.CTkFrame(
            self.hidden_compat,
            fg_color=WHITE
        )

        label_widget = ctk.CTkLabel(
            frame,
            text=label,
            font=app_font(1, "normal"),
            text_color=WHITE,
            fg_color=WHITE
        )

        value_widget = ctk.CTkLabel(
            frame,
            text="—",
            font=app_font(1, "normal"),
            text_color=WHITE,
            fg_color=WHITE
        )

        return {
            "frame": frame,
            "label": label_widget,
            "value": value_widget,
        }

    def _draw_dispensing_icon(self):
        c = self.icon_canvas
        c.delete("all")

        w = max(180, c.winfo_width())
        h = max(150, c.winfo_height())

        c.configure(bg=CREAM)

        # Soft background glow
        c.create_oval(
            int(w * 0.10),
            int(h * 0.08),
            int(w * 0.90),
            int(h * 0.92),
            fill="#FFE4CC",
            outline=""
        )

        # Machine body
        self._canvas_round_rect(
            c,
            int(w * 0.34),
            int(h * 0.12),
            int(w * 0.66),
            int(h * 0.67),
            18,
            WHITE,
            "#EBD8C6",
            2
        )

        # Machine screen
        self._canvas_round_rect(
            c,
            int(w * 0.39),
            int(h * 0.20),
            int(w * 0.61),
            int(h * 0.38),
            8,
            "#FFF9F4",
            "#EBD8C6",
            1
        )

        c.create_oval(
            int(w * 0.47),
            int(h * 0.24),
            int(w * 0.53),
            int(h * 0.31),
            outline=ORANGE,
            width=2
        )

        c.create_line(
            int(w * 0.43),
            int(h * 0.45),
            int(w * 0.57),
            int(h * 0.45),
            fill=ORANGE,
            width=5
        )

        # Dispensed box
        self._canvas_round_rect(
            c,
            int(w * 0.32),
            int(h * 0.70),
            int(w * 0.68),
            int(h * 0.84),
            10,
            "#FFF9F4",
            "#D9B89F",
            2
        )

        c.create_line(
            int(w * 0.36),
            int(h * 0.77),
            int(w * 0.64),
            int(h * 0.77),
            fill=ORANGE,
            width=3
        )

        # Platform shadow
        c.create_oval(
            int(w * 0.25),
            int(h * 0.83),
            int(w * 0.75),
            int(h * 0.96),
            fill="#D49B72",
            outline=""
        )

    def _canvas_round_rect(self, canvas, x1, y1, x2, y2, r, fill, outline="", width=1):
        if not outline:
            outline = fill

        x1 = int(x1)
        y1 = int(y1)
        x2 = int(x2)
        y2 = int(y2)
        r = int(max(1, min(r, abs(x2 - x1) // 2, abs(y2 - y1) // 2)))

        canvas.create_arc(
            x1,
            y1,
            x1 + 2 * r,
            y1 + 2 * r,
            start=90,
            extent=90,
            fill=fill,
            outline=outline,
            width=width
        )
        canvas.create_arc(
            x2 - 2 * r,
            y1,
            x2,
            y1 + 2 * r,
            start=0,
            extent=90,
            fill=fill,
            outline=outline,
            width=width
        )
        canvas.create_arc(
            x2 - 2 * r,
            y2 - 2 * r,
            x2,
            y2,
            start=270,
            extent=90,
            fill=fill,
            outline=outline,
            width=width
        )
        canvas.create_arc(
            x1,
            y2 - 2 * r,
            x1 + 2 * r,
            y2,
            start=180,
            extent=90,
            fill=fill,
            outline=outline,
            width=width
        )
        canvas.create_rectangle(
            x1 + r,
            y1,
            x2 - r,
            y2,
            fill=fill,
            outline=outline,
            width=width
        )
        canvas.create_rectangle(
            x1,
            y1 + r,
            x2,
            y2 - r,
            fill=fill,
            outline=outline,
            width=width
        )

    def _set_chip_value(self, chip_data, value, color=None):
        try:
            chip_data["value"].configure(
                text=str(value or "—"),
                text_color=color or BLACK
            )
        except Exception:
            pass

    def _set_state(self, text, color=None):
        color = color or INFO

        try:
            self.state_label.configure(
                text=text,
                text_color=color
            )

            if color == SUCCESS:
                self.state_pill.configure(fg_color="#EAF7EF")
            elif color == ERROR:
                self.state_pill.configure(fg_color="#FFECEC")
            elif color == ORANGE:
                self.state_pill.configure(fg_color="#FFF2E8")
            else:
                self.state_pill.configure(fg_color="#EEF4FF")

        except Exception:
            pass

    def _short_text(self, value, max_chars=38):
        value = str(value or "—")

        if len(value) <= max_chars:
            return value

        return value[:max_chars - 3] + "..."

    def _sync_layout(self, event=None):
        try:
            self.update_idletasks()

            available_w = max(900, self.content_wrap.winfo_width())
            available_h = max(540, self.content_wrap.winfo_height())

            card_w = min(1240, max(960, int(available_w * 0.96)))
            card_h = min(680, max(540, int(available_h * 0.96)))

            self.card.configure(width=card_w, height=card_h)

            left_wrap = max(440, self.left_panel.winfo_width() - 80)
            right_wrap = max(260, self.right_panel.winfo_width() - 56)

            self.item_name_label.configure(wraplength=left_wrap)
            self.message_label.configure(wraplength=left_wrap)

            self.info_title.configure(wraplength=right_wrap)
            self.dispenser_note_label.configure(wraplength=right_wrap)
            self.center_title.configure(wraplength=right_wrap)
            self.center_note.configure(wraplength=right_wrap)

        except Exception:
            pass

    # ---------------------------------------------------------------------
    # Config refresh
    # ---------------------------------------------------------------------

    def _refresh_from_config(self):
        try:
            self._base_text = config.get(
                "dispensing_page",
                "message_text",
                default="Dispensing your item"
            )

            self.page_title.configure(
                text=config.get("dispensing_page", "title", default="DISPENSING ITEM")
            )

            try:
                self.shell.set_header_right(
                    config.get("dispensing_page", "header_title", default="Dispensing Item")
                    if not self.user_data
                    else f"Welcome, {self.user_data.get('username', 'User')}!"
                )
            except Exception:
                pass

            if not self.processing and not self._anim_running:
                self.message_label.configure(text=self._base_text)

        except Exception as e:
            print(f"[DISPENSING] Config refresh failed: {e}", flush=True)

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

    # ---------------------------------------------------------------------
    # Page data
    # ---------------------------------------------------------------------

    def update_data(
        self,
        user_data=None,
        product=None,
        discount=0,
        total_paid=0,
        change=0,
        total=0,
        transaction_id=None,
        **kwargs
    ):
        self.user_data = user_data or {}
        self.product = product or kwargs.get("selected_product") or {}
        self.discount = discount or 0
        self.total_paid = total_paid or 0
        self.change = change or 0
        self.total = total or 0
        self.payment_session_id = kwargs.get("payment_session_id")
        self.payment_reference = kwargs.get("payment_reference")
        self.payment_method = kwargs.get("payment_method")
        self.online_payment = bool(kwargs.get("online_payment", False))
        self.payment_amount = kwargs.get("payment_amount", 0)
        self.payment_mode = kwargs.get("payment_mode")
        self.simulated = kwargs.get("simulated", False)

        self.transaction_id = (
            transaction_id
            or kwargs.get("transaction_id")
            or self.user_data.get("transaction_id")
            or self.user_data.get("transactionID")
            or self.user_data.get("latest_transaction_id")
        )

        if self.transaction_id:
            self.user_data["transaction_id"] = self.transaction_id
            self.user_data["latest_transaction_id"] = self.transaction_id

        self.processing = False

        username = self.user_data.get("username", "User")

        product_name = (
            self.product.get("name")
            or self.product.get("type")
            or "Selected item"
        )

        product_id = (
            self.product.get("productID")
            or self.product.get("product_id")
            or self.product.get("id")
            or "N/A"
        )

        try:
            self.shell.set_header_right(f"Welcome, {username}!")
        except Exception:
            pass

        self.item_name_label.configure(text=product_name)
        self._set_state("Dispensing", INFO)

        self.message_label.configure(
            text=self._base_text,
            text_color=INFO
        )

        self.short_note_label.configure(text="")
        self.status_label.configure(text="")

        self.dispenser_note_label.configure(
            text="Your item is being released.",
            text_color=MUTED
        )

        self.info_badge_label.configure(text="AUTOMATIC")
        self.info_title.configure(text="Please wait")
        self.center_title.configure(text="No action needed")
        self.center_note.configure(text="The next screen will open automatically.")
        self.bottom_label.configure(text="")

        self._set_chip_value(self.item_code_chip, product_id)
        self._set_chip_value(
            self.transaction_chip,
            self._short_text(self.transaction_id or "N/A", 38)
        )

        self.details_label.configure(
            text=(
                f"{config.get('dispensing_page', 'user_label', default='User')}: {username}\n"
                f"{config.get('dispensing_page', 'item_label', default='Item')}: {product_name}\n"
                f"{config.get('dispensing_page', 'product_id_label', default='Product ID')}: {product_id}\n"
                f"{config.get('dispensing_page', 'transaction_id_label', default='Transaction ID')}: {self.transaction_id or 'N/A'}\n"
                f"{config.get('dispensing_page', 'do_not_leave_text', default='Please do not leave while dispensing is in progress.')}"
            )
        )

        self.start_animation()
        self.after(200, self.start_dispensing)

    # ---------------------------------------------------------------------
    # Dispensing logic
    # ---------------------------------------------------------------------

    def start_dispensing(self):
        if self.processing:
            return

        self.processing = True
        threading.Thread(target=self._dispense_item_thread, daemon=True).start()

    def _get_product_id(self):
        return (
            self.product.get("productID")
            or self.product.get("product_id")
            or self.product.get("id")
            or ""
        )

    def _get_product_name(self):
        return (
            self.product.get("name")
            or self.product.get("type")
            or "Unknown Item"
        )

    def _extract_stock_value(self, item):
        if not isinstance(item, dict):
            return None

        for key in ("stock", "quantity", "available_stock", "remaining_stock"):
            if key not in item:
                continue

            try:
                return int(float(item.get(key)))
            except Exception:
                continue

        return None

    def _read_latest_stock_before_dispense(self, product_id):
        """
        Reads stock BEFORE dispensing.

        If this returns 1, the current dispense is the last kit in the lane,
        so Arduino must be asked to return that lane home after DISPENSED.
        """
        product_id = str(product_id or "").strip()

        if not product_id:
            return None

        # Prefer the latest config/inventory value over self.product because
        # self.product can be a stale copy passed from the purchase page.
        try:
            if hasattr(config, "get_product_by_id"):
                latest_product = config.get_product_by_id(product_id)
                stock = self._extract_stock_value(latest_product)

                if stock is not None:
                    return stock
        except Exception as e:
            print(f"[DISPENSING] Could not read latest product stock: {e}", flush=True)

        # Fallback to the product object carried by the page.
        stock = self._extract_stock_value(self.product)

        if stock is not None:
            return stock

        return None

    def _stock_after_decrement(self, updated_product, product_id):
        stock = self._extract_stock_value(updated_product)

        if stock is not None:
            return stock

        try:
            if hasattr(config, "get_product_by_id"):
                latest_product = config.get_product_by_id(str(product_id or ""))
                return self._extract_stock_value(latest_product)
        except Exception:
            pass

        return None

    def _should_return_home_after_dispense(self, product_id):
        stock_before = self._read_latest_stock_before_dispense(product_id)

        if stock_before is None:
            print(
                f"[DISPENSING] Stock value unavailable for product_id={product_id}; "
                "will use post-decrement fallback for return-home.",
                flush=True,
            )
            return False

        should_return = stock_before <= 1

        print(
            f"[DISPENSING] product_id={product_id} stock_before={stock_before} "
            f"return_home_after={should_return}",
            flush=True,
        )

        return should_return

    def _dispense_was_confirmed(self, result):
        if not isinstance(result, dict):
            return False

        if result.get("actual_kit") in {"KIT1", "KIT2", "KIT3"}:
            return True

        for line in result.get("replies") or []:
            if str(line or "").strip().upper().startswith("DISPENSED:"):
                return True

        return False

    def _run_return_home_fallback(self, kit_code):
        kit_code = str(kit_code or "").strip().upper()

        if kit_code not in {"KIT1", "KIT2"}:
            print(f"[DISPENSING] Cannot run return-home fallback for kit_code={kit_code}", flush=True)
            return

        print(f"[DISPENSING] Running fallback return-home for {kit_code}", flush=True)

        try:
            result = send_return_kit_home_command(kit_code)
            print(f"[DISPENSING] Fallback return-home result for {kit_code}: {result}", flush=True)

            if not result.get("success") and hasattr(self.controller, "show_error"):
                self.after(
                    0,
                    lambda: self.controller.show_error(
                        (
                            f"The last kit was dispensed, but {kit_code} did not confirm return-home. "
                            "Please call an operator to inspect and home/reset the lane before restocking."
                        ),
                        title="Return Home Warning",
                        action_text="Continue",
                        on_close=getattr(self.controller, "_close_error_only", None),
                    ),
                )
        except Exception as e:
            print(f"[DISPENSING] Fallback return-home failed for {kit_code}: {e}", flush=True)

    def _decrement_stock_after_confirmed_dispense(self, product_id):
        """
        Decrements local stock after Arduino confirms DISPENSED.

        Returns:
          stock_after or None
        """
        if not product_id:
            return None

        try:
            updated = config.decrement_product_stock(str(product_id), 1)

            if updated:
                stock_after = self._stock_after_decrement(updated, product_id)

                if stock_after is not None and isinstance(self.product, dict):
                    self.product["stock"] = stock_after
                    self.product["available"] = stock_after > 0

                mark_inventory_dirty()
                threading.Thread(target=push_inventory_if_dirty, daemon=True).start()

                print(
                    f"[DISPENSING] Stock decremented product_id={product_id} "
                    f"stock_after={stock_after}",
                    flush=True,
                )

                return stock_after

            print(
                f"[DISPENSING] Stock not decremented for product_id={product_id}",
                flush=True,
            )

        except Exception as e:
            print(f"[DISPENSING] Failed to decrement stock: {e}", flush=True)

        return None

    def _dispense_item_thread(self):
        try:
            product_id = self._get_product_id()
            product_name = self._get_product_name()

            return_home_after = self._should_return_home_after_dispense(product_id)

            result = send_dispense_command(
                product_id=product_id,
                product_name=product_name,
                return_home_after=return_home_after,
            )

            self.after(0, lambda: self._on_dispense_done(result))

        except Exception as e:
            self.after(0, lambda: self._on_dispense_error(str(e)))

    def _show_success_and_continue(self, return_home_warning=None):
        if return_home_warning:
            self._set_state("Completed", ORANGE)

            self.message_label.configure(
                text="Item released",
                text_color=SUCCESS
            )

            self.short_note_label.configure(text="")
            self.status_label.configure(text=return_home_warning, text_color=ERROR)

            self.dispenser_note_label.configure(
                text="Item released, but the empty lane needs operator attention.",
                text_color=ERROR
            )

            self.info_badge_label.configure(text="ATTENTION")
            self.info_title.configure(text="Lane needs reset")
            self.center_title.configure(text="Operator check needed")
            self.center_note.configure(text="The next screen will still open automatically.")
            self.bottom_label.configure(text="")

            if hasattr(self.controller, "show_error"):
                self.controller.show_error(
                    return_home_warning,
                    title="Return Home Warning",
                    action_text="Continue",
                    on_close=getattr(self.controller, "_close_error_only", None),
                )
        else:
            self._set_state("Completed", SUCCESS)

            self.message_label.configure(
                text=config.get(
                    "dispensing_page",
                    "success_title_text",
                    default="Item released"
                ),
                text_color=SUCCESS
            )

            self.short_note_label.configure(text="")
            self.status_label.configure(text="")

            self.dispenser_note_label.configure(
                text="Item released.",
                text_color=MUTED
            )

            self.info_badge_label.configure(text="COMPLETED")
            self.info_title.configure(text="Item released")
            self.center_title.configure(text="Continuing")
            self.center_note.configure(text="Opening the next screen.")
            self.bottom_label.configure(text="")

        self.after(
            int(config.get("dispensing_page", "next_page_delay_ms", default=1500)),
            lambda: self.controller.show_loading_then(
                config.get(
                    "dispensing_page",
                    "next_loading_text",
                    default="Loading instructions"
                ),
                "HowToUsePage",
                delay=800,
                user_data=self.user_data,
                selected_product=self.product,
                transaction_id=self.transaction_id,
                online_payment=self.online_payment,
                payment_method=self.payment_method,
                payment_session_id=self.payment_session_id,
                payment_reference=self.payment_reference,
                payment_amount=self.payment_amount,
                payment_mode=self.payment_mode,
                simulated=self.simulated,
            )
        )

    def _on_dispense_done(self, result):
        self.processing = False
        self.stop_animation()

        if not isinstance(result, dict):
            result = {
                "success": False,
                "message": "Invalid dispense result returned by serial layer.",
                "replies": [],
            }

        success = bool(result.get("success"))
        message = result.get("message", "")

        dispensed_confirmed = success or self._dispense_was_confirmed(result)

        if dispensed_confirmed:
            product_id = self._get_product_id()
            stock_after = self._decrement_stock_after_confirmed_dispense(product_id)

            return_home_after = bool(result.get("return_home_after"))
            returned_home = bool(result.get("returned_home"))
            actual_kit = str(result.get("actual_kit") or result.get("expected_kit") or "").strip().upper()

            return_home_warning = None

            # Normal path:
            # If stock_before was 1, the serial layer should have already sent
            # RETURN_KIT1_HOME or RETURN_KIT2_HOME after DISPENSED.
            if return_home_after:
                print(
                    "[DISPENSING] Last-stock return-home result: "
                    f"returned_home={returned_home} result={result.get('return_home_result')}",
                    flush=True,
                )

                if not returned_home:
                    return_home_warning = (
                        "The item was released, but the empty kit lane did not confirm return-home. "
                        "Please call an operator to inspect and home/reset the lane before restocking."
                    )

            # Fallback path:
            # If stock_before was missing/stale and we only discover after
            # decrement that stock is now zero, return the lane home now.
            if (
                stock_after is not None
                and stock_after <= 0
                and not return_home_after
                and actual_kit in {"KIT1", "KIT2"}
            ):
                threading.Thread(
                    target=self._run_return_home_fallback,
                    args=(actual_kit,),
                    daemon=True,
                ).start()

            self._show_success_and_continue(return_home_warning)
            return

        error_message = message or config.get(
            "dispensing_page",
            "failed_status_text",
            default="Failed to dispense item."
        )

        self._set_state("Failed", ERROR)

        self.message_label.configure(
            text=config.get(
                "dispensing_page",
                "failed_title_text",
                default="Dispensing failed"
            ),
            text_color=ERROR
        )

        self.short_note_label.configure(text="")

        self.dispenser_note_label.configure(
            text="The item was not released.",
            text_color=ERROR
        )

        self.status_label.configure(
            text=error_message,
            text_color=ERROR
        )

        self.info_badge_label.configure(text="ASSISTANCE")
        self.info_title.configure(text="Dispensing issue")
        self.center_title.configure(text="Please wait")
        self.center_note.configure(text="Assistance may be needed.")
        self.bottom_label.configure(text="")

        if hasattr(self.controller, "show_error"):
            self.controller.show_error(
                f"Dispensing failed.\n{error_message}\n\nPlease call an operator. The booth will not retry the actuator automatically.",
                title="Dispensing Error",
                action_text=None,
                on_action=None,
            )

    def _on_dispense_error(self, error_message):
        self.processing = False
        self.stop_animation()

        self._set_state("Error", ERROR)

        self.message_label.configure(
            text=config.get(
                "dispensing_page",
                "failed_title_text",
                default="Dispensing failed"
            ),
            text_color=ERROR
        )

        self.short_note_label.configure(text="")

        self.dispenser_note_label.configure(
            text="The dispenser reported an error.",
            text_color=ERROR
        )

        self.status_label.configure(
            text=f"{config.get('dispensing_page', 'error_prefix', default='Error:')} {error_message}",
            text_color=ERROR
        )

        self.info_badge_label.configure(text="ERROR")
        self.info_title.configure(text="Dispensing issue")
        self.center_title.configure(text="Please wait")
        self.center_note.configure(text="Assistance may be needed.")
        self.bottom_label.configure(text="")

        if hasattr(self.controller, "show_error"):
            self.controller.show_error(
                f"Dispensing failed.\n{error_message}\n\nPlease call an operator. The booth will not retry the actuator automatically.",
                title="Dispensing Error",
                action_text=None,
                on_action=None,
            )

    # ---------------------------------------------------------------------
    # Animation
    # ---------------------------------------------------------------------

    def start_animation(self):
        self.stop_animation()
        self._anim_running = True
        self._dot_count = 0
        self._animate_text()

    def stop_animation(self):
        self._anim_running = False

        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass

            self._anim_job = None

    def _animate_text(self):
        if not self._anim_running:
            return

        self._dot_count = (self._dot_count + 1) % 4
        dots = "." * self._dot_count

        self.message_label.configure(
            text=f"{self._base_text}{dots}",
            text_color=INFO
        )

        self._anim_job = self.after(450, self._animate_text)

    def destroy(self):
        self.stop_animation()
        self._cancel_config_refresh()
        super().destroy()