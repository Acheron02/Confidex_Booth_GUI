import time
import threading
import tkinter as tk

from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, PillButton, card_body
from backend.util import api_client
from config_manager import config

GPIOZERO_AVAILABLE = True

try:
    from gpiozero import Button, LED
except Exception as e:
    GPIOZERO_AVAILABLE = False
    print(f"[CASH] gpiozero import failed: {e}", flush=True)

    class Button:
        def __init__(self, *a, **k):
            self.when_pressed = None

    class LED:
        def __init__(self, *a, **k):
            pass

        def on(self):
            pass

        def off(self):
            pass


from backend.util.dispenser_serial import (
    send_bill_on_command,
    send_bill_off_command,
)


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


class CashPaymentPage(ctk.CTkFrame):
    REFRESH_MS = 1500

    def __init__(self, master, controller):
        super().__init__(master, fg_color=CREAM)

        self.controller = controller
        self.selected_product = None
        self.user_data = {}
        self.discount = 0
        self.total_cash_inserted = 0
        self.transaction_in_progress = False
        self.loading_visible = False
        self.planned_cash_bill = None

        self.cash_bypass_shortcut_bound = False
        self.cash_bypass_shortcut_enabled = False

        self.pulse_count = 0
        self.last_pulse_time = 0.0
        self.last_processed_time = 0.0
        self.tolerance = float(config.get("cash_payment_page", "pulse_tolerance_seconds", default=0.30))
        self.pulse_lock = threading.Lock()

        self._status_anim_job = None
        self._status_anim_running = False
        self._status_base_text = config.get("cash_payment_page", "status_text", default="Insert bills to pay")
        self._status_dot_count = 0
        self._config_refresh_job = None

        self.bill_acceptor_enabled = False
        self._bill_command_lock = threading.Lock()

        print(f"[CASH] Initializing CashPaymentPage | GPIOZERO_AVAILABLE={GPIOZERO_AVAILABLE}", flush=True)

        try:
            bill_pulse_pin = int(config.get("hardware", "bill_acceptor_pulse_pin", default=17))
            bill_reject_pin = int(config.get("hardware", "bill_reject_pin", default=27))

            if GPIOZERO_AVAILABLE:
                self.bill_acceptor = Button(bill_pulse_pin, pull_up=True, bounce_time=0.001)
                self.reject_pin = LED(bill_reject_pin)
                print(
                    f"[CASH] GPIO initialized: bill_acceptor=GPIO{bill_pulse_pin}, reject_pin=GPIO{bill_reject_pin}",
                    flush=True
                )
            else:
                self.bill_acceptor = Button(bill_pulse_pin)
                self.reject_pin = LED(bill_reject_pin)

        except Exception as e:
            print(f"[CASH] GPIO setup failed: {e}", flush=True)
            self.bill_acceptor = Button(17)
            self.reject_pin = LED(27)

        self.bill_acceptor.when_pressed = self._pulse_callback
        threading.Thread(target=self._pulse_watcher, daemon=True).start()

        self.shell = AppShell(self, title_right="")
        self.shell.pack(fill="both", expand=True)

        self._build_ui()
        self._bind_cash_bypass_shortcut()
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
        self._build_loading_overlay()

        self.bind("<Configure>", self._sync_layout, add="+")
        self.page.bind("<Configure>", self._sync_layout, add="+")
        self.content_wrap.bind("<Configure>", self._sync_layout, add="+")

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
            text=config.get("cash_payment_page", "back_button_text", default="Back"),
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

        self.page_title = ctk.CTkLabel(
            self.title_wrap,
            text=config.get("cash_payment_page", "title", default="CASH PAYMENT"),
            font=app_heavy(32),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=760
        )
        self.page_title.grid(row=0, column=0, sticky="ew")

        self.page_subtitle = ctk.CTkLabel(
            self.title_wrap,
            text="Insert the required amount using the bill acceptor.",
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=760
        )
        self.page_subtitle.grid(row=1, column=0, sticky="ew", pady=(3, 0))

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

        self.card = RoundedCard(
            self.content_wrap,
            fg_color=WHITE,
            radius=36,
            auto_size=False,
            pad=0,
            width=1120,
            height=580
        )
        self.card.grid(row=0, column=0, sticky="nsew")

        body = card_body(self.card)
        safe_configure(body, fg_color=WHITE)

        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=0)
        body.grid_rowconfigure(1, weight=1)
        body.grid_rowconfigure(2, weight=0)

        self._build_order_header(body)
        self._build_payment_body(body)
        self._build_bottom_actions(body)

    def _build_order_header(self, parent):
        self.card_header = ctk.CTkFrame(
            parent,
            fg_color=WHITE
        )
        self.card_header.grid(row=0, column=0, sticky="ew", padx=30, pady=(26, 14))

        self.card_header.grid_columnconfigure(0, weight=1)
        self.card_header.grid_columnconfigure(1, weight=0)

        self.order_stack = ctk.CTkFrame(
            self.card_header,
            fg_color=WHITE
        )
        self.order_stack.grid(row=0, column=0, sticky="ew", padx=(0, 18))
        self.order_stack.grid_columnconfigure(0, weight=1)

        self.order_caption = ctk.CTkLabel(
            self.order_stack,
            text="Selected product",
            font=app_font(13, "bold"),
            text_color=ORANGE,
            fg_color=WHITE,
            anchor="w",
            justify="left"
        )
        self.order_caption.grid(row=0, column=0, sticky="w")

        self.order_text = ctk.CTkLabel(
            self.order_stack,
            text=config.get("cash_payment_page", "no_item_text", default="No item selected"),
            font=app_heavy(25),
            text_color=MUTED,
            justify="left",
            anchor="w",
            wraplength=680,
            fg_color=WHITE
        )
        self.order_text.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.state_pill = ctk.CTkFrame(
            self.card_header,
            fg_color="#EEF4FF",
            corner_radius=999
        )
        self.state_pill.grid(row=0, column=1, sticky="e")

        self.state_label = ctk.CTkLabel(
            self.state_pill,
            text="Ready",
            font=app_font(14, "bold"),
            text_color=INFO,
            fg_color="transparent"
        )
        self.state_label.pack(padx=20, pady=9)

    def _build_payment_body(self, parent):
        self.payment_body = ctk.CTkFrame(
            parent,
            fg_color=WHITE
        )
        self.payment_body.grid(row=1, column=0, sticky="nsew", padx=30, pady=(0, 14))

        self.payment_body.grid_columnconfigure(0, weight=7, uniform="cash-body")
        self.payment_body.grid_columnconfigure(1, weight=5, uniform="cash-body")
        self.payment_body.grid_rowconfigure(0, weight=1)

        self._build_amount_panel(self.payment_body)
        self._build_status_panel(self.payment_body)

    def _build_amount_panel(self, parent):
        self.amount_panel = ctk.CTkFrame(
            parent,
            fg_color=CREAM,
            corner_radius=30
        )
        self.amount_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        self.amount_panel.grid_columnconfigure(0, weight=1)
        self.amount_panel.grid_rowconfigure(0, weight=0)
        self.amount_panel.grid_rowconfigure(1, weight=0)
        self.amount_panel.grid_rowconfigure(2, weight=0)
        self.amount_panel.grid_rowconfigure(3, weight=1)
        self.amount_panel.grid_rowconfigure(4, weight=0)

        self.total_caption = ctk.CTkLabel(
            self.amount_panel,
            text="Amount to pay",
            font=app_font(17, "bold"),
            text_color=MUTED,
            fg_color=CREAM,
            justify="center",
            anchor="center"
        )
        self.total_caption.grid(row=0, column=0, sticky="ew", padx=24, pady=(26, 0))

        self.total_due_value = ctk.CTkLabel(
            self.amount_panel,
            text="₱0.00",
            font=app_heavy(68),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center"
        )
        self.total_due_value.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 16))

        self.amount_tiles = ctk.CTkFrame(
            self.amount_panel,
            fg_color=CREAM,
            height=104
        )
        self.amount_tiles.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 18))
        self.amount_tiles.grid_propagate(False)

        self.amount_tiles.grid_columnconfigure(0, weight=1, uniform="amount-tiles")
        self.amount_tiles.grid_columnconfigure(1, weight=1, uniform="amount-tiles")
        self.amount_tiles.grid_columnconfigure(2, weight=1, uniform="amount-tiles")
        self.amount_tiles.grid_rowconfigure(0, weight=1)

        (
            self.inserted_tile,
            self.inserted_title,
            self.inserted_value
        ) = self._make_amount_tile(
            self.amount_tiles,
            title="Inserted",
            value="₱0.00",
            accent=INFO
        )
        self.inserted_tile.grid(row=0, column=0, sticky="nsew", padx=(0, 7))

        (
            self.remaining_tile,
            self.remaining_title,
            self.remaining_value
        ) = self._make_amount_tile(
            self.amount_tiles,
            title="Remaining",
            value="—",
            accent=ERROR
        )
        self.remaining_tile.grid(row=0, column=1, sticky="nsew", padx=7)

        (
            self.planned_tile,
            self.planned_title,
            self.planned_value
        ) = self._make_amount_tile(
            self.amount_tiles,
            title="Expected",
            value="—",
            accent=ORANGE
        )
        self.planned_tile.grid(row=0, column=2, sticky="nsew", padx=(7, 0))

        self.amount_spacer = ctk.CTkFrame(
            self.amount_panel,
            fg_color=CREAM
        )
        self.amount_spacer.grid(row=3, column=0, sticky="nsew")

        self.progress = ctk.CTkLabel(
            self.amount_panel,
            text=self._accepted_bills_text(),
            font=app_font(14, "bold"),
            text_color=MUTED,
            justify="center",
            anchor="center",
            wraplength=620,
            fg_color=CREAM
        )
        self.progress.grid(row=4, column=0, sticky="ew", padx=26, pady=(0, 24))

    def _build_status_panel(self, parent):
        self.status_panel = ctk.CTkFrame(
            parent,
            fg_color="#FFFDF8",
            corner_radius=30,
            border_width=2,
            border_color="#EBD8C6"
        )
        self.status_panel.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        self.status_panel.grid_columnconfigure(0, weight=1)
        self.status_panel.grid_rowconfigure(0, weight=0)
        self.status_panel.grid_rowconfigure(1, weight=0)
        self.status_panel.grid_rowconfigure(2, weight=1)
        self.status_panel.grid_rowconfigure(3, weight=0)

        self.cash_icon = tk.Canvas(
            self.status_panel,
            width=116,
            height=116,
            bg="#FFFDF8",
            highlightthickness=0,
            bd=0
        )
        self.cash_icon.grid(row=0, column=0, pady=(28, 12))
        self.cash_icon.bind("<Configure>", lambda event: self._draw_cash_icon())
        self._draw_cash_icon()

        self.status_text = ctk.CTkLabel(
            self.status_panel,
            text=config.get("cash_payment_page", "status_text", default="Insert bills to pay"),
            font=app_heavy(28),
            text_color=INFO,
            justify="center",
            anchor="center",
            wraplength=420,
            fg_color="#FFFDF8"
        )
        self.status_text.grid(row=1, column=0, sticky="ew", padx=28, pady=(0, 8))

        self.helper_text = ctk.CTkLabel(
            self.status_panel,
            text=config.get(
                "cash_payment_page",
                "helper_text",
                default="Insert bills one at a time."
            ),
            font=app_font(15, "bold"),
            text_color=MUTED,
            justify="center",
            anchor="center",
            wraplength=400,
            fg_color="#FFFDF8"
        )
        self.helper_text.grid(row=2, column=0, sticky="n", padx=30, pady=(0, 12))

        self.reader_hint = ctk.CTkLabel(
            self.status_panel,
            text="The kiosk will update automatically after each accepted bill.",
            font=app_font(12, "normal"),
            text_color=MUTED,
            justify="center",
            anchor="center",
            wraplength=380,
            fg_color="#FFFDF8"
        )
        self.reader_hint.grid(row=3, column=0, sticky="ew", padx=30, pady=(0, 24))

    def _build_bottom_actions(self, parent):
        self.bottom_bar = ctk.CTkFrame(
            parent,
            fg_color=WHITE
        )
        self.bottom_bar.grid(row=2, column=0, sticky="ew", padx=30, pady=(0, 24))

        self.bottom_bar.grid_columnconfigure(0, weight=1)

        self.bottom_note = ctk.CTkLabel(
            self.bottom_bar,
            text="Please wait for the kiosk to confirm every bill before inserting the next one.",
            font=app_font(13, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="w",
            justify="left",
            wraplength=900
        )
        self.bottom_note.grid(row=0, column=0, sticky="w")

    def _build_loading_overlay(self):
        self.loading_overlay = ctk.CTkFrame(
            self,
            fg_color="#E6E1C9"
        )
        self.loading_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.loading_overlay.lower()

        self.loading_card = ctk.CTkFrame(
            self.loading_overlay,
            fg_color=WHITE,
            corner_radius=30
        )
        self.loading_card.place(relx=0.5, rely=0.48, anchor="center")
        self.loading_card.grid_columnconfigure(0, weight=1)

        self.loading_icon = tk.Canvas(
            self.loading_card,
            width=96,
            height=96,
            bg=WHITE,
            highlightthickness=0,
            bd=0
        )
        self.loading_icon.grid(row=0, column=0, sticky="ew", padx=70, pady=(34, 10))
        self.loading_icon.bind("<Configure>", lambda event: self._draw_loading_icon())
        self._draw_loading_icon()

        self.loading_label = ctk.CTkLabel(
            self.loading_card,
            text=config.get("cash_payment_page", "loading_text", default="Processing payment..."),
            font=app_heavy(30),
            text_color=BLACK,
            fg_color=WHITE,
            justify="center",
            anchor="center",
            wraplength=460
        )
        self.loading_label.grid(row=1, column=0, sticky="ew", padx=70, pady=(0, 4))

        self.loading_dots = ctk.CTkLabel(
            self.loading_card,
            text="",
            font=app_heavy(30),
            text_color=BLACK,
            fg_color=WHITE,
            justify="center",
            anchor="center"
        )
        self.loading_dots.grid(row=2, column=0, sticky="ew", padx=70, pady=(0, 34))

        self._dot_count = 0
        self._animate_dots_running = False

    # ---------------------------------------------------------------------
    # UI helpers
    # ---------------------------------------------------------------------

    def _make_amount_tile(self, parent, title, value, accent=ORANGE):
        tile = ctk.CTkFrame(
            parent,
            fg_color=WHITE,
            corner_radius=18,
            height=96
        )
        tile.grid_propagate(False)
        tile.grid_columnconfigure(0, weight=1)
        tile.grid_rowconfigure(0, weight=0)
        tile.grid_rowconfigure(1, weight=0)
        tile.grid_rowconfigure(2, weight=1)

        accent_line = ctk.CTkFrame(
            tile,
            fg_color=accent,
            height=4,
            corner_radius=999
        )
        accent_line.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))

        title_label = ctk.CTkLabel(
            tile,
            text=title,
            font=app_font(12, "bold"),
            text_color=MUTED,
            fg_color=WHITE,
            justify="center",
            anchor="center"
        )
        title_label.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 1))

        value_label = ctk.CTkLabel(
            tile,
            text=value,
            font=app_heavy(20),
            text_color=BLACK,
            fg_color=WHITE,
            justify="center",
            anchor="center",
            wraplength=150
        )
        value_label.grid(row=2, column=0, sticky="n", padx=10, pady=(0, 10))

        return tile, title_label, value_label

    def _draw_cash_icon(self):
        c = self.cash_icon
        c.delete("all")

        w = max(96, c.winfo_width())
        h = max(96, c.winfo_height())

        c.configure(bg="#FFFDF8")

        c.create_oval(
            int(w * 0.08),
            int(h * 0.08),
            int(w * 0.92),
            int(h * 0.92),
            fill="#FFF0E3",
            outline="#FFF0E3"
        )

        self._canvas_round_rect(
            c,
            int(w * 0.24),
            int(h * 0.32),
            int(w * 0.76),
            int(h * 0.62),
            12,
            WHITE,
            "#EBD8C6",
            2
        )

        c.create_oval(
            int(w * 0.43),
            int(h * 0.38),
            int(w * 0.57),
            int(h * 0.52),
            outline=ORANGE,
            width=3
        )

        c.create_line(
            int(w * 0.32),
            int(h * 0.72),
            int(w * 0.68),
            int(h * 0.72),
            fill="#E3C8B2",
            width=7
        )

        c.create_text(
            int(w * 0.50),
            int(h * 0.82),
            text="₱",
            fill=ORANGE,
            font=("Arial", 23, "bold"),
            anchor="center"
        )

    def _draw_loading_icon(self):
        c = self.loading_icon
        c.delete("all")

        w = max(88, c.winfo_width())
        h = max(88, c.winfo_height())

        c.configure(bg=WHITE)

        c.create_oval(
            int(w * 0.08),
            int(h * 0.08),
            int(w * 0.92),
            int(h * 0.92),
            fill="#FFF0E3",
            outline="#FFF0E3"
        )

        c.create_arc(
            int(w * 0.26),
            int(h * 0.26),
            int(w * 0.74),
            int(h * 0.74),
            start=25,
            extent=300,
            style="arc",
            outline=ORANGE,
            width=6
        )

        c.create_oval(
            int(w * 0.48),
            int(h * 0.48),
            int(w * 0.52),
            int(h * 0.52),
            fill=BLACK,
            outline=BLACK
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

    def _sync_layout(self, event=None):
        try:
            self.update_idletasks()

            available_w = max(900, self.content_wrap.winfo_width())
            available_h = max(520, self.content_wrap.winfo_height())

            card_w = min(1240, max(940, int(available_w * 0.96)))
            card_h = min(680, max(560, int(available_h * 0.96)))

            self.card.configure(width=card_w, height=card_h)

            header_wrap = max(360, card_w - 360)
            self.order_text.configure(wraplength=header_wrap)

            if card_w < 980:
                self.payment_body.grid_columnconfigure(0, weight=1, uniform="")
                self.payment_body.grid_columnconfigure(1, weight=1, uniform="")
            else:
                self.payment_body.grid_columnconfigure(0, weight=7, uniform="cash-body")
                self.payment_body.grid_columnconfigure(1, weight=5, uniform="cash-body")

            status_wrap = max(280, int(card_w * 0.32))
            self.status_text.configure(wraplength=status_wrap)
            self.helper_text.configure(wraplength=status_wrap)
            self.reader_hint.configure(wraplength=status_wrap)

            amount_wrap = max(420, int(card_w * 0.52))
            self.progress.configure(wraplength=amount_wrap)

            tile_wrap = max(110, int((amount_wrap - 80) / 3))
            self.inserted_value.configure(wraplength=tile_wrap)
            self.remaining_value.configure(wraplength=tile_wrap)
            self.planned_value.configure(wraplength=tile_wrap)

            self.bottom_note.configure(wraplength=max(360, card_w - 120))
        except Exception:
            pass

    def _set_status_badge(self, text="Ready", color=None):
        color = color or INFO

        try:
            self.state_label.configure(
                text=text,
                text_color=color
            )

            if color == ERROR:
                self.state_pill.configure(fg_color="#FFECEC")
            elif color == SUCCESS:
                self.state_pill.configure(fg_color="#EAF7EF")
            elif color == ORANGE:
                self.state_pill.configure(fg_color="#FFF2E8")
            else:
                self.state_pill.configure(fg_color="#EEF4FF")

        except Exception:
            pass

    def _total_price(self):
        if not self.selected_product:
            return 0.0

        try:
            price = float(self.selected_product.get("price", 0) or 0)
        except Exception:
            price = 0.0

        try:
            discount = float(self.discount or 0)
        except Exception:
            discount = 0.0

        return price * (1 - discount / 100.0)

    def _update_payment_overview(self, total_price=None):
        try:
            total = float(total_price if total_price is not None else self._total_price())
            inserted = float(self.total_cash_inserted or 0)
            remaining = max(0.0, total - inserted)

            self.total_due_value.configure(text=f"₱{total:.2f}")
            self.inserted_value.configure(text=f"₱{inserted:.2f}")

            if remaining > 0:
                self.remaining_value.configure(text=f"₱{remaining:.2f}", text_color=ERROR)
            else:
                self.remaining_value.configure(text="Paid", text_color=SUCCESS)

            if self.planned_cash_bill is not None:
                self.planned_value.configure(
                    text=f"₱{float(self.planned_cash_bill):.2f}",
                    text_color=BLACK
                )
            else:
                self.planned_value.configure(text="—", text_color=MUTED)

        except Exception:
            pass

    def _accepted_bills_text(self):
        accepted = config.get("payment", "accepted_bills", default=[50, 100, 200, 500, 1000])
        joined = "  •  ".join(f"₱{value}" for value in accepted)
        return f"Accepted bills: {joined}"

    def _refresh_from_config(self):
        try:
            self.tolerance = float(config.get("cash_payment_page", "pulse_tolerance_seconds", default=0.30))

            self.back_btn.configure(
                text=config.get("cash_payment_page", "back_button_text", default="Back")
            )

            self.page_title.configure(
                text=config.get("cash_payment_page", "title", default="CASH PAYMENT")
            )

            self.loading_label.configure(
                text=config.get("cash_payment_page", "loading_text", default="Processing payment...")
            )

            self.progress.configure(text=self._accepted_bills_text())

            if not self.transaction_in_progress and not self.loading_visible:
                self.helper_text.configure(
                    text=config.get(
                        "cash_payment_page",
                        "helper_text",
                        default="Insert bills one at a time."
                    )
                )

        except Exception as e:
            print(f"[CASH PAYMENT] Config refresh failed: {e}", flush=True)

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
    # STAFF CASH BYPASS SHORTCUT
    # ---------------------------------------------------------------------

    def _bind_cash_bypass_shortcut(self):
        if self.cash_bypass_shortcut_bound:
            return

        try:
            self.bind_all("<Control-Shift-KeyPress-E>", self._on_cash_bypass_shortcut, add="+")
            self.bind_all("<Control-Shift-KeyPress-e>", self._on_cash_bypass_shortcut, add="+")
            self.cash_bypass_shortcut_bound = True
            print("[CASH] Cash bypass shortcut bound: Ctrl + Shift + E", flush=True)
        except Exception as e:
            print(f"[CASH] Failed to bind cash bypass shortcut: {e}", flush=True)

    def _unbind_cash_bypass_shortcut(self):
        if not self.cash_bypass_shortcut_bound:
            return

        try:
            self.unbind_all("<Control-Shift-KeyPress-E>")
            self.unbind_all("<Control-Shift-KeyPress-e>")
        except Exception:
            pass

        self.cash_bypass_shortcut_bound = False

    def _is_active_page(self):
        try:
            current_frame = getattr(self.controller, "current_frame", None)
            if current_frame is self:
                return True
        except Exception:
            pass

        try:
            current_page = getattr(self.controller, "current_page", None)
            if current_page == "CashPaymentPage":
                return True
        except Exception:
            pass

        try:
            current_page_name = getattr(self.controller, "current_page_name", None)
            if current_page_name == "CashPaymentPage":
                return True
        except Exception:
            pass

        try:
            return bool(self.winfo_ismapped() and self.winfo_viewable())
        except Exception:
            return True

    def _on_cash_bypass_shortcut(self, event=None):
        if not self.cash_bypass_shortcut_enabled:
            return None

        if not self._is_active_page():
            return None

        self.trigger_cash_bypass()
        return "break"

    def trigger_cash_bypass(self):
        """
        Staff-only cash bypass.

        Ctrl + Shift + E performs the old TEMP: Next behavior without showing
        any temporary button on the UI.

        This skips:
        - bill hardware waiting
        - backend transaction posting

        It proceeds directly to receipt generation using the selected product.
        """
        if self.loading_visible or self.transaction_in_progress:
            return

        if not self.user_data or not self.selected_product:
            self._set_status(
                text="Bypass needs a selected product.",
                color=ERROR,
                visible=True
            )
            return

        print("[CASH] Staff cash bypass triggered using Ctrl + Shift + E", flush=True)

        self.cash_bypass_shortcut_enabled = False

        try:
            self.disable_bill_acceptor()
        except Exception:
            pass

        self.stop_status_animation()
        self.hide_loading()

        total = self._total_price()

        if self.planned_cash_bill is not None:
            paid = float(self.planned_cash_bill or 0)

            if paid < total:
                paid = total
        else:
            paid = total

        change = max(0.0, paid - total)
        self.total_cash_inserted = paid
        self.transaction_in_progress = True

        self._update_payment_overview(total)
        self._set_status(
            text="Cash payment bypassed.",
            color=SUCCESS,
            visible=True
        )
        self._set_helper(
            text="Proceeding without bill hardware.",
            color=MUTED,
            visible=True
        )
        self._set_status_badge("Bypassed", SUCCESS)

        bypass_transaction_id = f"CASH-BYPASS-{int(time.time())}"

        self.controller.show_loading_then(
            config.get(
                "cash_payment_page",
                "generate_receipt_loading_text",
                default="Generating receipt"
            ),
            "ReceiptPage",
            delay=800,
            user_data=self.user_data,
            product=self.selected_product,
            discount=self.discount,
            total_paid=paid,
            change=change,
            total=total,
            payment_method="cash",
            online_payment=False,
            transaction_id=bypass_transaction_id
        )

    # ---------------------------------------------------------------------
    # BILL ACCEPTOR CONTROL
    # ---------------------------------------------------------------------

    def _enable_bill_acceptor_thread(self):
        with self._bill_command_lock:
            try:
                result = send_bill_on_command()
                print(f"[CASH] BILL_ON result: {result}", flush=True)

                if result.get("success"):
                    self.bill_acceptor_enabled = True
                else:
                    print(f"[CASH] Failed to enable bill acceptor: {result.get('message')}", flush=True)

            except Exception as e:
                print(f"[CASH] BILL_ON exception: {e}", flush=True)

    def _disable_bill_acceptor_thread(self):
        with self._bill_command_lock:
            try:
                result = send_bill_off_command()
                print(f"[CASH] BILL_OFF result: {result}", flush=True)

                if result.get("success"):
                    self.bill_acceptor_enabled = False
                else:
                    print(f"[CASH] Failed to disable bill acceptor: {result.get('message')}", flush=True)

            except Exception as e:
                print(f"[CASH] BILL_OFF exception: {e}", flush=True)

    def enable_bill_acceptor(self, async_mode=True):
        if self.bill_acceptor_enabled:
            return

        if async_mode:
            threading.Thread(target=self._enable_bill_acceptor_thread, daemon=True).start()
        else:
            self._enable_bill_acceptor_thread()

    def disable_bill_acceptor(self, async_mode=True):
        if async_mode:
            threading.Thread(target=self._disable_bill_acceptor_thread, daemon=True).start()
        else:
            self._disable_bill_acceptor_thread()

    # ---------------------------------------------------------------------
    # PAGE FLOW
    # ---------------------------------------------------------------------

    def go_back(self):
        print("[CASH] go_back called", flush=True)

        if self.transaction_in_progress or self.loading_visible:
            print("[CASH] go_back blocked", flush=True)
            return

        self.cash_bypass_shortcut_enabled = False

        self.disable_bill_acceptor()
        self.stop_status_animation()
        self.hide_loading()

        self.controller.show_loading_then(
            config.get(
                "cash_payment_page",
                "back_loading_text",
                default="Returning to payment options"
            ),
            "PaymentMethodPage",
            delay=1000,
            user_data=self.user_data,
            selected_product=self.selected_product,
            discount=self.discount
        )

    def update_data(self, user_data=None, selected_product=None, discount=0, planned_cash_bill=None, **kwargs):
        self.user_data = (user_data or {}).copy()
        self.selected_product = (selected_product or {}).copy() if selected_product else None
        self.discount = discount or 0
        self.planned_cash_bill = planned_cash_bill
        self.total_cash_inserted = 0
        self.transaction_in_progress = False
        self.cash_bypass_shortcut_enabled = True

        try:
            self.shell.set_header_right(f"Welcome, {self.user_data.get('username', 'User')}!")
        except Exception:
            pass

        with self.pulse_lock:
            self.pulse_count = 0
            self.last_pulse_time = 0.0

        if not self.user_data or not self.selected_product:
            print("[CASH] missing user_data or selected_product", flush=True)

            self.cash_bypass_shortcut_enabled = False

            self.order_text.configure(
                text=config.get(
                    "cash_payment_page",
                    "missing_selection_text",
                    default="No product selected"
                ),
                text_color=ERROR
            )

            self.total_due_value.configure(text="₱0.00")
            self.inserted_value.configure(text="₱0.00")
            self.remaining_value.configure(text="—", text_color=ERROR)
            self.planned_value.configure(text="—", text_color=MUTED)

            self._set_status(
                text=config.get(
                    "cash_payment_page",
                    "choose_product_first_text",
                    default="Please choose a product first."
                ),
                color=ERROR,
                visible=True
            )
            self._set_progress(visible=False)
            self._set_helper(visible=False)
            self._set_status_badge("Error", ERROR)
            self.disable_bill_acceptor()
            return

        total = self._total_price()

        self.order_text.configure(
            text=self.selected_product.get("name", "Selected product"),
            text_color=BLACK
        )

        self._update_payment_overview(total)

        self.start_status_animation(
            config.get("cash_payment_page", "status_text", default="Insert bills to pay"),
            INFO
        )

        self._set_progress(
            text=self._accepted_bills_text(),
            color=MUTED,
            visible=True
        )

        helper_key = "planned_helper_text" if self.planned_cash_bill is not None else "helper_text"

        self._set_helper(
            text=config.get(
                "cash_payment_page",
                helper_key,
                default="Insert bills one at a time."
            ),
            color=MUTED,
            visible=True
        )

        self._set_status_badge("Ready", INFO)
        self.hide_loading()
        self.enable_bill_acceptor()

    # ---------------------------------------------------------------------
    # STATUS SETTERS
    # ---------------------------------------------------------------------

    def _set_status(self, text="", color=None, visible=True):
        self.stop_status_animation()

        if not visible or not text:
            self.status_text.grid_remove()
            return

        print(f"[CASH] STATUS: {text}", flush=True)

        self.status_text.configure(
            text=text,
            text_color=color or INFO
        )
        self.status_text.grid()
        self.status_text.update_idletasks()

        if color == ERROR:
            self._set_status_badge("Error", ERROR)
        elif color == SUCCESS:
            self._set_status_badge("Paid", SUCCESS)
        else:
            self._set_status_badge("Active", INFO)

    def _set_progress(self, text="", color=None, visible=True):
        if not visible or not text:
            self.progress.grid_remove()
            return

        self.progress.configure(
            text=text,
            text_color=color or MUTED
        )
        self.progress.grid()
        self.progress.update_idletasks()

    def _set_helper(self, text="", color=None, visible=True):
        if not visible or not text:
            self.helper_text.grid_remove()
            return

        self.helper_text.configure(
            text=text,
            text_color=color or MUTED
        )
        self.helper_text.grid()
        self.helper_text.update_idletasks()

    # ---------------------------------------------------------------------
    # LOADING
    # ---------------------------------------------------------------------

    def show_loading(self):
        if self.loading_visible:
            return

        print("[CASH] show_loading", flush=True)

        self.loading_visible = True
        self.loading_overlay.lift()
        self._animate_dots_running = True
        self._set_status_badge("Reading", INFO)
        self._animate_dots()

    def hide_loading(self):
        print("[CASH] hide_loading", flush=True)

        self.loading_visible = False
        self.loading_overlay.lower()
        self._animate_dots_running = False
        self.loading_dots.configure(text="")

    def _animate_dots(self):
        if not self._animate_dots_running:
            return

        self._dot_count = (self._dot_count + 1) % 4
        self.loading_dots.configure(text="." * self._dot_count)
        self.after(450, self._animate_dots)

    # ---------------------------------------------------------------------
    # BILL PULSE HANDLING
    # ---------------------------------------------------------------------

    def reject_bill(self, duration=0.5):
        print(f"[CASH] reject_bill called duration={duration}", flush=True)

        try:
            self.reject_pin.on()
            time.sleep(duration)
            self.reject_pin.off()
            print("[CASH] reject_bill finished", flush=True)

        except Exception as e:
            print(f"[CASH] reject_bill error: {e}", flush=True)

    def _pulse_callback(self):
        now = time.time()

        with self.pulse_lock:
            self.pulse_count += 1
            self.last_pulse_time = now
            current_count = self.pulse_count

        print(f"[CASH] pulse detected | count={current_count} | t={now}", flush=True)

    def _pulse_watcher(self):
        print("[CASH] pulse watcher started", flush=True)

        while True:
            try:
                with self.pulse_lock:
                    count = self.pulse_count
                    last_pulse = self.last_pulse_time

                if count > 0 and (time.time() - last_pulse) > self.tolerance:
                    with self.pulse_lock:
                        stable_count = self.pulse_count
                        self.pulse_count = 0
                        self.last_processed_time = time.time()

                    print(f"[CASH] pulse batch complete | stable_count={stable_count}", flush=True)

                    bill_value = self.map_pulses_to_bill(stable_count)
                    print(f"[CASH] mapped bill_value={bill_value}", flush=True)

                    if bill_value:
                        self.controller.after(0, lambda v=bill_value: self.process_bill(v))
                    else:
                        self.controller.after(
                            0,
                            lambda: self._set_status(
                                text="Unknown bill. Returning...",
                                color=ERROR,
                                visible=True
                            )
                        )
                        threading.Thread(target=self.reject_bill, daemon=True).start()
                        self.controller.after(
                            2500,
                            lambda: self.start_status_animation(
                                config.get("cash_payment_page", "status_text", default="Insert bills to pay"),
                                INFO
                            )
                        )

                time.sleep(0.01)

            except Exception as e:
                print(f"[CASH] pulse watcher error: {e}", flush=True)
                time.sleep(0.2)

    def map_pulses_to_bill(self, count):
        pulse_map = config.get(
            "cash_payment_page",
            "pulse_to_bill_map",
            default={
                "5": 50,
                "10": 100,
                "20": 200,
                "50": 500,
                "100": 1000
            }
        )

        value = pulse_map.get(str(count))
        print(f"[CASH] map_pulses_to_bill | count={count} | value={value}", flush=True)
        return value

    def process_bill(self, bill_value):
        print(f"[CASH] process_bill called | bill_value={bill_value}", flush=True)
        self.show_loading()

        if not self.selected_product:
            print("[CASH] no selected product, rejecting bill", flush=True)
            self._set_status(
                text="No product selected. Returning bill.",
                color=ERROR,
                visible=True
            )
            threading.Thread(target=self.reject_bill, daemon=True).start()
            self.after(1000, self.hide_loading)
            return

        self.total_cash_inserted += bill_value
        total_price = self._total_price()
        remaining = total_price - self.total_cash_inserted

        self._update_payment_overview(total_price)

        print(
            f"[CASH] total_cash_inserted={self.total_cash_inserted} | total_price={total_price} | remaining={remaining}",
            flush=True
        )

        if remaining > 0:
            self._set_status(
                text=f"Inserted ₱{self.total_cash_inserted:.2f}. Add ₱{remaining:.2f} more.",
                color=INFO,
                visible=True
            )
            self._set_helper(
                text="Insert the next bill.",
                color=MUTED,
                visible=True
            )
            self.after(700, self.hide_loading)
        else:
            if not self.transaction_in_progress:
                self.transaction_in_progress = True
                self.cash_bypass_shortcut_enabled = False
                print("[CASH] payment complete, proceeding to confirm_payment", flush=True)

                self._set_status(
                    text="Payment complete.",
                    color=SUCCESS,
                    visible=True
                )
                self._set_helper(
                    text="Saving transaction...",
                    color=MUTED,
                    visible=True
                )
                self.after(1200, self.confirm_payment)

            self.after(700, self.hide_loading)

    # ---------------------------------------------------------------------
    # TRANSACTION
    # ---------------------------------------------------------------------

    def confirm_payment(self):
        print("[CASH] confirm_payment called", flush=True)

        if not self.selected_product or not self.user_data:
            print("[CASH] confirm_payment aborted: missing data", flush=True)

            self.transaction_in_progress = False
            self.cash_bypass_shortcut_enabled = False
            self._set_status(
                text="Missing product or user data.",
                color=ERROR,
                visible=True
            )
            self.hide_loading()
            self.disable_bill_acceptor()
            return

        cash = self.total_cash_inserted
        total = self._total_price()
        change = cash - total

        transaction_data = {
            "user_id": self.user_data.get("_id") or self.user_data.get("userID"),
            "status": "completed",
            "items": [
                {
                    "name": self.selected_product.get("name", "Unknown"),
                    "productID": self.selected_product.get("productID") or self.selected_product.get("product_id") or "",
                    "type": self.selected_product.get("type", ""),
                    "price": float(self.selected_product.get("price", 0) or 0),
                    "discount": float(self.discount or 0),
                    "finalPrice": float(total),
                    "result": "Pending",
                }
            ],
            "purchasedDate": None,
        }

        threading.Thread(
            target=self.post_transaction_and_continue,
            args=(transaction_data, cash, change, total),
            daemon=True
        ).start()

    def start_status_animation(self, base_text=None, color=None):
        self.stop_status_animation()

        self._status_anim_running = True
        self._status_base_text = base_text or config.get("cash_payment_page", "status_text", default="Insert bills to pay")
        self._status_dot_count = 0

        self.status_text.configure(
            text=self._status_base_text,
            text_color=color or INFO
        )
        self.status_text.grid()
        self._set_status_badge("Ready", color or INFO)
        self._animate_status_text()

    def stop_status_animation(self):
        self._status_anim_running = False

        if self._status_anim_job is not None:
            try:
                self.after_cancel(self._status_anim_job)
            except Exception:
                pass

            self._status_anim_job = None

    def _animate_status_text(self):
        if not self._status_anim_running:
            return

        self._status_dot_count = (self._status_dot_count + 1) % 4
        dots = "." * self._status_dot_count

        self.status_text.configure(
            text=f"{self._status_base_text}{dots}",
            text_color=INFO
        )

        self._status_anim_job = self.after(450, self._animate_status_text)

    def post_transaction_and_continue(self, transaction_data_local, cash, change, total):
        print("[CASH] post_transaction_and_continue started", flush=True)

        try:
            response = api_client.post_transaction(transaction_data_local)
            print(f"[CASH] post_transaction status_code={response.status_code} ok={response.ok}", flush=True)

            if not response.ok:
                self.controller.after(
                    0,
                    lambda: self._handle_transaction_failure(
                        f"{config.get('cash_payment_page', 'transaction_failed_prefix', default='Transaction failed')} ({response.status_code})."
                    )
                )
                return

            transaction_id = None

            try:
                data = response.json()
                transaction_obj = data.get("transaction") or {}
                transaction_id = (
                    transaction_obj.get("_id")
                    or data.get("_id")
                    or data.get("transaction_id")
                    or data.get("id")
                )
            except Exception as e:
                print(f"[CASH] failed to parse response json: {e}", flush=True)

            self.controller.after(
                0,
                lambda: self._handle_transaction_success(
                    cash=cash,
                    change=change,
                    total=total,
                    transaction_id=transaction_id
                )
            )

        except Exception as e:
            print(f"[CASH] post_transaction exception: {e}", flush=True)
            self.controller.after(
                0,
                lambda: self._handle_transaction_failure(
                    f"{config.get('cash_payment_page', 'network_error_prefix', default='Network/API error:')} {e}"
                )
            )

    def _handle_transaction_success(self, cash, change, total, transaction_id=None):
        print(f"[CASH] transaction success | transaction_id={transaction_id} | change={change}", flush=True)

        self.cash_bypass_shortcut_enabled = False
        self.disable_bill_acceptor(async_mode=False)
        self.hide_loading()
        self._set_status_badge("Saved", SUCCESS)

        if change > 0:
            self._set_status(
                text="Payment saved. Preparing change.",
                color=SUCCESS,
                visible=True
            )
            self._set_helper(
                text="Please wait.",
                color=MUTED,
                visible=True
            )

            self.controller.show_loading_then(
                config.get(
                    "cash_payment_page",
                    "prepare_change_loading_text",
                    default="Preparing change"
                ),
                "ChangeDispensingPage",
                delay=800,
                user_data=self.user_data,
                product=self.selected_product,
                discount=self.discount,
                total_paid=cash,
                change=change,
                total=total,
                payment_method="cash",
                online_payment=False,
                transaction_id=transaction_id
            )
        else:
            self._set_status(
                text="Payment saved. Generating receipt.",
                color=SUCCESS,
                visible=True
            )
            self._set_helper(
                text="No change needed.",
                color=MUTED,
                visible=True
            )

            self.controller.show_loading_then(
                config.get(
                    "cash_payment_page",
                    "generate_receipt_loading_text",
                    default="Generating receipt"
                ),
                "ReceiptPage",
                delay=800,
                user_data=self.user_data,
                product=self.selected_product,
                discount=self.discount,
                total_paid=cash,
                change=change,
                total=total,
                payment_method="cash",
                online_payment=False,
                transaction_id=transaction_id
            )

        self.after(7000, self.reset_fields)

    def _handle_transaction_failure(self, error_message):
        print(f"[CASH] transaction failure | error={error_message}", flush=True)

        self.transaction_in_progress = False
        self.cash_bypass_shortcut_enabled = True
        self.hide_loading()
        self.disable_bill_acceptor()

        self._set_status(
            text="Transaction failed.",
            color=ERROR,
            visible=True
        )
        self._set_helper(
            text="Please try again or ask for assistance.",
            color=ERROR,
            visible=True
        )

        threading.Thread(target=self.reject_bill, daemon=True).start()

    def reset_fields(self, **kwargs):
        print("[CASH] reset_fields called", flush=True)

        self.selected_product = None
        self.user_data = {}
        self.discount = 0
        self.total_cash_inserted = 0
        self.transaction_in_progress = False
        self.planned_cash_bill = None
        self.cash_bypass_shortcut_enabled = False

        with self.pulse_lock:
            self.pulse_count = 0
            self.last_pulse_time = 0.0

        try:
            self.shell.set_header_right("")
        except Exception:
            pass

        self.order_text.configure(
            text=config.get("cash_payment_page", "no_item_text", default="No item selected"),
            text_color=MUTED
        )

        self.total_due_value.configure(text="₱0.00")
        self.inserted_value.configure(text="₱0.00")
        self.remaining_value.configure(text="—", text_color=MUTED)
        self.planned_value.configure(text="—", text_color=MUTED)

        self.start_status_animation(
            config.get("cash_payment_page", "status_text", default="Insert bills to pay"),
            INFO
        )

        self._set_progress(
            text=self._accepted_bills_text(),
            color=MUTED,
            visible=True
        )

        self._set_helper(
            text=config.get(
                "cash_payment_page",
                "helper_text",
                default="Insert bills one at a time."
            ),
            color=MUTED,
            visible=True
        )

        self._set_status_badge("Ready", INFO)
        self.hide_loading()

    def destroy(self):
        self.cash_bypass_shortcut_enabled = False
        self._unbind_cash_bypass_shortcut()
        self.stop_status_animation()
        self._cancel_config_refresh()
        super().destroy()