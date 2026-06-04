import tkinter as tk

from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, PillButton, card_body
from frontend.components.keyboard import CashNumericKeyboard
from config_manager import config


class CashBillCheckPage(ctk.CTkFrame):
    REFRESH_MS = 1500
    DEFAULT_ACCEPTED_BILLS = [50, 100, 200, 500, 1000]
    DEFAULT_EXACT_ONLY_THRESHOLD = 500
    DEFAULT_MAX_CHANGE_COINS = 20

    def __init__(self, master, controller):
        super().__init__(master, fg_color=theme.CREAM)
        self.controller = controller

        self.user_data = {}
        self.selected_product = None
        self.discount = 0

        self.amount_var = tk.StringVar(value="")
        self._message_hide_job = None
        self._config_refresh_job = None

        self.shell = AppShell(self, title_right="Cash Bill Check")
        self.shell.pack(fill="both", expand=True)

        # =========================
        # TOP BAR
        # =========================
        top_bar = ctk.CTkFrame(self.shell.body, fg_color="transparent")
        top_bar.pack(fill="x", padx=28, pady=(16, 8))
        top_bar.grid_columnconfigure(0, weight=0)
        top_bar.grid_columnconfigure(1, weight=1)
        top_bar.grid_columnconfigure(2, weight=0)

        self.back_btn = PillButton(
            top_bar,
            text=config.get("cash_bill_check_page", "back_button_text", default="Back"),
            width=130,
            height=58,
            command=self.go_back,
            font=theme.font(18, "bold")
        )
        self.back_btn.grid(row=0, column=0, sticky="w")

        self.page_title = ctk.CTkLabel(
            top_bar,
            text=config.get("cash_bill_check_page", "title", default="ENTER CASH AMOUNT"),
            font=theme.heavy(30),
            text_color=theme.BLACK
        )
        self.page_title.grid(row=0, column=1)

        self.top_spacer = ctk.CTkFrame(top_bar, fg_color="transparent", width=130, height=58)
        self.top_spacer.grid(row=0, column=2, sticky="e")

        # =========================
        # MAIN CONTENT
        # =========================
        self.content_wrap = ctk.CTkFrame(self.shell.body, fg_color="transparent")
        self.content_wrap.pack(fill="both", expand=True, padx=28, pady=(10, 18))

        self.content_wrap.grid_columnconfigure(0, weight=1, uniform="cashcols")
        self.content_wrap.grid_columnconfigure(1, weight=1, uniform="cashcols")
        self.content_wrap.grid_rowconfigure(0, weight=1)

        self.left_col = ctk.CTkFrame(self.content_wrap, fg_color="transparent")
        self.left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.left_col.grid_columnconfigure(0, weight=1)
        self.left_col.grid_rowconfigure(0, weight=1)

        self.right_col = ctk.CTkFrame(self.content_wrap, fg_color="transparent")
        self.right_col.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        self.right_col.grid_columnconfigure(0, weight=1)
        self.right_col.grid_rowconfigure(0, weight=1)

        # =========================
        # LEFT CARD
        # =========================
        self.left_card = RoundedCard(
            self.left_col,
            auto_size=False,
            pad=18
        )
        self.left_card.grid(row=0, column=0, sticky="nsew")
        self.left_card.grid_propagate(False)

        left_body = card_body(self.left_card)
        left_body.grid_columnconfigure(0, weight=1)
        left_body.grid_rowconfigure(0, weight=1)

        self.left_center_wrap = ctk.CTkFrame(left_body, fg_color=theme.WHITE)
        self.left_center_wrap.grid(row=0, column=0, sticky="nsew", padx=16, pady=16)
        self.left_center_wrap.grid_columnconfigure(0, weight=1)

        self.left_center_wrap.grid_rowconfigure(0, weight=0)
        self.left_center_wrap.grid_rowconfigure(1, weight=0)
        self.left_center_wrap.grid_rowconfigure(2, weight=0)
        self.left_center_wrap.grid_rowconfigure(3, weight=1)
        self.left_center_wrap.grid_rowconfigure(4, weight=0)

        # =========================
        # HEADER BLOCK
        # =========================
        self.header_block = ctk.CTkFrame(self.left_center_wrap, fg_color=theme.WHITE)
        self.header_block.grid(row=0, column=0, sticky="ew", padx=12, pady=(2, 12))
        self.header_block.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.header_block,
            text=config.get("cash_bill_check_page", "prompt_title", default="Enter Cash Amount"),
            font=theme.heavy(27),
            text_color=theme.BLACK,
            fg_color=theme.WHITE,
            wraplength=500,
            justify="center",
            anchor="center"
        )
        self.title_label.grid(row=0, column=0, padx=20, pady=(0, 4), sticky="ew")

        self.helper_label = ctk.CTkLabel(
            self.header_block,
            text=config.get(
                "cash_bill_check_page",
                "helper_text",
                default="Enter the cash amount before inserting bills."
            ),
            font=theme.font(15, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            wraplength=500,
            justify="center",
            anchor="center"
        )
        self.helper_label.grid(row=1, column=0, padx=24, pady=(0, 0), sticky="ew")

        # =========================
        # SUMMARY TILES
        # =========================
        self.summary_grid = ctk.CTkFrame(self.left_center_wrap, fg_color=theme.WHITE)
        self.summary_grid.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 12))
        self.summary_grid.grid_columnconfigure(0, weight=1, uniform="summary")
        self.summary_grid.grid_columnconfigure(1, weight=1, uniform="summary")
        self.summary_grid.grid_columnconfigure(2, weight=1, uniform="summary")

        (
            self.product_tile,
            self.product_title_label,
            self.product_value_label
        ) = self._make_summary_tile(
            self.summary_grid,
            title="Product",
            value="—"
        )
        self.product_tile.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        (
            self.total_tile,
            self.total_title_label,
            self.total_value_label
        ) = self._make_summary_tile(
            self.summary_grid,
            title="Total",
            value="₱0.00",
            value_color=theme.BLACK
        )
        self.total_tile.grid(row=0, column=1, sticky="nsew", padx=6)

        (
            self.discount_tile,
            self.discount_title_label,
            self.discount_value_label
        ) = self._make_summary_tile(
            self.summary_grid,
            title="Discount",
            value="None"
        )
        self.discount_tile.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        # =========================
        # AMOUNT INPUT
        # =========================
        self.input_card = RoundedCard(
            self.left_center_wrap,
            auto_size=False,
            height=142,
            pad=8
        )
        self.input_card.grid(row=2, column=0, padx=8, pady=(0, 12), sticky="ew")
        self.input_card.grid_propagate(False)

        input_body = card_body(self.input_card)
        input_body.pack_propagate(False)

        input_inner = ctk.CTkFrame(input_body, fg_color=theme.WHITE)
        input_inner.pack(fill="both", expand=True)
        input_inner.grid_columnconfigure(0, weight=0)
        input_inner.grid_columnconfigure(1, weight=1)
        input_inner.grid_columnconfigure(2, weight=0)
        input_inner.grid_rowconfigure(0, weight=1)

        self.currency_label = ctk.CTkLabel(
            input_inner,
            text="₱",
            font=theme.heavy(46),
            text_color=theme.BLACK,
            fg_color=theme.WHITE
        )
        self.currency_label.grid(row=0, column=0, padx=(28, 10), pady=18, sticky="e")

        self.amount_entry = tk.Entry(
            input_inner,
            textvariable=self.amount_var,
            font=("Arial", 42, "bold"),
            bd=0,
            relief="flat",
            justify="center",
            bg=theme.WHITE,
            fg=theme.BLACK,
            insertbackground=theme.BLACK,
        )
        self.amount_entry.grid(row=0, column=1, padx=(0, 18), pady=24, sticky="ew")
        self.amount_entry.bind("<KeyRelease>", self._on_entry_change)
        self.amount_entry.bind("<Return>", lambda e: self.submit_amount())

        self.input_right_spacer = ctk.CTkFrame(input_inner, fg_color="transparent", width=26)
        self.input_right_spacer.grid(row=0, column=2, sticky="ns")

        # =========================
        # MESSAGE AREA
        # =========================
        self.message_card = ctk.CTkFrame(
            self.left_center_wrap,
            fg_color=theme.WHITE,
            corner_radius=18,
            border_width=2,
            border_color=theme.CREAM
        )
        self.message_card.grid(row=3, column=0, padx=8, pady=(0, 12), sticky="nsew")
        self.message_card.grid_columnconfigure(0, weight=1)
        self.message_card.grid_rowconfigure(0, weight=1)

        self.message_label = ctk.CTkLabel(
            self.message_card,
            text=config.get(
                "cash_bill_check_page",
                "default_message",
                default="Enter an amount, then press Continue."
            ),
            font=theme.font(24, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            wraplength=460,
            justify="center",
            anchor="center"
        )
        self.message_label.grid(row=0, column=0, padx=28, pady=16, sticky="nsew")

        # =========================
        # COMPACT GUIDANCE
        # =========================
        self.guidance_grid = ctk.CTkFrame(self.left_center_wrap, fg_color=theme.WHITE)
        self.guidance_grid.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 2))
        self.guidance_grid.grid_columnconfigure(0, weight=1, uniform="guides")
        self.guidance_grid.grid_columnconfigure(1, weight=1, uniform="guides")

        (
            self.bills_card,
            self.bills_title_label,
            self.bills_value_label
        ) = self._make_guidance_tile(
            self.guidance_grid,
            title="Accepted bills",
            value="₱50, ₱100, ₱200, ₱500, ₱1000"
        )
        self.bills_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        (
            self.warning_card,
            self.warning_title_label,
            self.warning_label
        ) = self._make_guidance_tile(
            self.guidance_grid,
            title="Change rule",
            value="Higher cash needs available change."
        )
        self.warning_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # =========================
        # RIGHT CARD / KEYBOARD
        # =========================
        self.right_card = RoundedCard(
            self.right_col,
            auto_size=False,
            pad=18
        )
        self.right_card.grid(row=0, column=0, sticky="nsew")
        self.right_card.grid_propagate(False)

        right_body = card_body(self.right_card)
        right_body.grid_columnconfigure(0, weight=1)
        right_body.grid_rowconfigure(0, weight=1)

        self.keyboard = CashNumericKeyboard(
            right_body,
            on_key=self.append_digit,
            on_clear=self.clear_amount,
            on_submit=self.submit_amount,
        )
        self.keyboard.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self.bind("<Configure>", self._sync_column_sizes, add="+")
        self.content_wrap.bind("<Configure>", self._sync_column_sizes, add="+")

        self._start_config_refresh()

    # =========================
    # UI HELPERS
    # =========================
    def _make_summary_tile(self, parent, title, value, value_color=None):
        tile = ctk.CTkFrame(
            parent,
            fg_color=theme.CREAM,
            corner_radius=18
        )
        tile.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            tile,
            text=title,
            font=theme.font(13, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.CREAM,
            anchor="center",
            justify="center",
            width=1
        )
        title_label.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 0))

        value_label = ctk.CTkLabel(
            tile,
            text=value,
            font=theme.font(18, "bold"),
            text_color=value_color or theme.BLACK,
            fg_color=theme.CREAM,
            anchor="center",
            justify="center",
            wraplength=130,
            width=1
        )
        value_label.grid(row=1, column=0, sticky="ew", padx=18, pady=(2, 12))

        return tile, title_label, value_label

    def _make_guidance_tile(self, parent, title, value):
        tile = ctk.CTkFrame(
            parent,
            fg_color=theme.CREAM,
            corner_radius=18
        )
        tile.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            tile,
            text=title,
            font=theme.font(14, "bold"),
            text_color=theme.BLACK,
            fg_color=theme.CREAM,
            anchor="w",
            justify="left",
            width=1
        )
        title_label.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 2))

        value_label = ctk.CTkLabel(
            tile,
            text=value,
            font=theme.font(13, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.CREAM,
            anchor="w",
            justify="left",
            wraplength=150,
            width=1
        )
        value_label.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 12))

        return tile, title_label, value_label

    def _sync_column_sizes(self, event=None):
        try:
            self.update_idletasks()

            total_w = max(200, self.content_wrap.winfo_width())
            total_h = max(200, self.content_wrap.winfo_height())

            col_gap = 24
            card_w = max(320, (total_w - col_gap) // 2)
            card_h = max(420, total_h)

            self.left_card.configure(width=card_w, height=card_h)
            self.right_card.configure(width=card_w, height=card_h)

            inner_w = max(260, card_w - 68)
            self.input_card.configure(width=inner_w, height=142)

            wrap_main = max(260, inner_w - 100)
            wrap_message = max(260, inner_w - 120)
            wrap_tile = max(95, (inner_w - 120) // 3)
            wrap_guide = max(115, (inner_w - 130) // 2)

            self.title_label.configure(wraplength=wrap_main)
            self.helper_label.configure(wraplength=wrap_main)
            self.message_label.configure(wraplength=wrap_message)

            self.product_value_label.configure(wraplength=wrap_tile)
            self.total_value_label.configure(wraplength=wrap_tile)
            self.discount_value_label.configure(wraplength=wrap_tile)

            self.bills_value_label.configure(wraplength=wrap_guide)
            self.warning_label.configure(wraplength=wrap_guide)
        except Exception:
            pass

    def _refresh_from_config(self):
        try:
            self.back_btn.configure(
                text=config.get("cash_bill_check_page", "back_button_text", default="Back")
            )
            self.page_title.configure(
                text=config.get("cash_bill_check_page", "title", default="ENTER CASH AMOUNT")
            )
            self.title_label.configure(
                text=config.get("cash_bill_check_page", "prompt_title", default="Enter Cash Amount")
            )
            self.helper_label.configure(
                text=config.get(
                    "cash_bill_check_page",
                    "helper_text",
                    default="Enter the cash amount before inserting bills."
                )
            )
        except Exception as e:
            print(f"[CASH BILL CHECK] Config refresh failed: {e}", flush=True)

    def _start_config_refresh(self):
        self._refresh_from_config()
        self._config_refresh_job = self.after(self.REFRESH_MS, self._start_config_refresh)

    # =========================
    # DATA / PAGE STATE
    # =========================
    def update_data(self, user_data=None, selected_product=None, discount=0, **kwargs):
        self.user_data = (user_data or {}).copy()
        self.selected_product = (selected_product or {}).copy() if selected_product else None
        self.discount = discount or 0
        self.amount_var.set("")
        self._clear_message_timer()

        self.shell.set_header_right(f"Welcome, {self.user_data.get('username', 'User')}!")
        self.after(10, self._sync_column_sizes)

        accepted_bills_text = ", ".join(f"₱{b}" for b in self._accepted_bills())
        self.bills_value_label.configure(text=accepted_bills_text)

        if not self.selected_product:
            self.product_value_label.configure(text="No product", text_color=theme.ERROR)
            self.total_value_label.configure(text="—", text_color=theme.ERROR)
            self.discount_value_label.configure(text="—", text_color=theme.ERROR)

            self._set_message(
                config.get(
                    "cash_bill_check_page",
                    "choose_product_first_text",
                    default="Please choose a product first."
                ),
                theme.ERROR,
                auto_hide_ms=None
            )
            self._update_warning_style(total=0)
            self.after(50, self._focus_entry)
            return

        total = self._compute_total()
        product_name = self.selected_product.get("name", "Unknown")

        self.product_value_label.configure(
            text=str(product_name),
            text_color=theme.BLACK
        )

        self.total_value_label.configure(
            text=f"₱{total:.2f}",
            text_color=theme.BLACK
        )

        if float(self.discount or 0) > 0:
            self.discount_value_label.configure(
                text=f"{float(self.discount):.0f}% off",
                text_color=theme.SUCCESS
            )
        else:
            self.discount_value_label.configure(
                text="None",
                text_color=theme.MUTED
            )

        self._set_message(
            config.get(
                "cash_bill_check_page",
                "default_message",
                default="Enter an amount, then press Continue."
            ),
            theme.MUTED,
            auto_hide_ms=None
        )

        self._update_warning_style(total=total)
        self.after(50, self._focus_entry)

    def _focus_entry(self):
        try:
            self.amount_entry.focus_set()
            self.amount_entry.icursor(tk.END)
        except Exception:
            pass

    def _clear_message_timer(self):
        if self._message_hide_job is not None:
            try:
                self.after_cancel(self._message_hide_job)
            except Exception:
                pass
            self._message_hide_job = None

    def _set_message(self, text, color, auto_hide_ms=None):
        self._clear_message_timer()
        self.message_label.configure(text=text, text_color=color)

        if auto_hide_ms:
            self._message_hide_job = self.after(
                auto_hide_ms,
                lambda: self._set_message(
                    config.get(
                        "cash_bill_check_page",
                        "default_message",
                        default="Enter an amount, then press Continue."
                    ),
                    theme.MUTED,
                    auto_hide_ms=None
                )
            )

    # =========================
    # PAYMENT LOGIC
    # =========================
    def _compute_total(self):
        if not self.selected_product:
            return 0.0
        price = float(self.selected_product.get("price", 0) or 0)
        return price * (1 - float(self.discount or 0) / 100.0)

    def _accepted_bills(self):
        raw = config.get("payment", "accepted_bills", default=self.DEFAULT_ACCEPTED_BILLS)
        cleaned = []

        for value in raw or []:
            try:
                bill = int(value)
                if bill > 0:
                    cleaned.append(bill)
            except Exception:
                pass

        cleaned = sorted(set(cleaned))
        return cleaned or self.DEFAULT_ACCEPTED_BILLS

    def _exact_only_threshold(self):
        try:
            return int(config.get(
                "cash_bill_check_page",
                "exact_only_threshold",
                default=self.DEFAULT_EXACT_ONLY_THRESHOLD
            ))
        except Exception:
            return self.DEFAULT_EXACT_ONLY_THRESHOLD

    def _coin_inventory_records(self):
        coins_raw = config.get_coin_inventory()
        records = []

        if isinstance(coins_raw, dict):
            for denom_key, data in coins_raw.items():
                try:
                    denomination = int(denom_key)
                    stock = int((data or {}).get("stock", 0))
                    enabled = bool((data or {}).get("enabled", True))
                    if enabled and denomination > 0 and stock > 0:
                        records.append({
                            "denomination": denomination,
                            "stock": stock,
                            "enabled": enabled,
                        })
                except Exception:
                    pass

        elif isinstance(coins_raw, list):
            for coin in coins_raw:
                try:
                    denomination = int(coin.get("denomination", 0))
                    stock = int(coin.get("stock", 0))
                    enabled = bool(coin.get("enabled", True))
                    if enabled and denomination > 0 and stock > 0:
                        records.append({
                            "denomination": denomination,
                            "stock": stock,
                            "enabled": enabled,
                        })
                except Exception:
                    pass

        return sorted(records, key=lambda x: x["denomination"], reverse=True)

    def _current_amount(self):
        raw = self.amount_var.get().strip()
        if not raw:
            return 0

        digits = "".join(ch for ch in raw if ch.isdigit())
        if digits != raw:
            self.amount_var.set(digits)

        try:
            return int(digits) if digits else 0
        except Exception:
            return 0

    def _on_entry_change(self, event=None):
        max_digits = int(config.get("cash_bill_check_page", "max_digits", default=4))
        raw = self.amount_var.get()
        digits = "".join(ch for ch in raw if ch.isdigit())[:max_digits]

        if raw != digits:
            self.amount_var.set(digits)

        self._set_message(
            config.get(
                "cash_bill_check_page",
                "validate_prompt_text",
                default="Press Continue to check this amount."
            ),
            theme.MUTED,
            auto_hide_ms=None
        )

    def append_digit(self, digit):
        max_digits = int(config.get("cash_bill_check_page", "max_digits", default=4))
        current = "".join(ch for ch in self.amount_var.get() if ch.isdigit())[:max_digits]

        if len(current) >= max_digits:
            self._focus_entry()
            return

        self.amount_var.set((current + digit)[:max_digits])

        self._set_message(
            config.get(
                "cash_bill_check_page",
                "validate_prompt_text",
                default="Press Continue to check this amount."
            ),
            theme.MUTED,
            auto_hide_ms=None
        )
        self._focus_entry()

    def clear_amount(self):
        self.amount_var.set("")
        self._set_message(
            config.get(
                "cash_bill_check_page",
                "default_message",
                default="Enter an amount, then press Continue."
            ),
            theme.MUTED,
            auto_hide_ms=None
        )
        self._focus_entry()

    def _can_compose_amount_from_accepted_bills(self, amount):
        if amount < 0:
            return False

        accepted = self._accepted_bills()
        reachable = [False] * (amount + 1)
        reachable[0] = True

        for current in range(amount + 1):
            if not reachable[current]:
                continue

            for bill in accepted:
                nxt = current + bill
                if nxt <= amount:
                    reachable[nxt] = True

        return reachable[amount]

    def _build_bill_combo(self, amount):
        accepted = sorted(self._accepted_bills(), reverse=True)
        remaining = amount
        combo = {}

        for bill in accepted:
            if remaining <= 0:
                break

            count = remaining // bill
            if count > 0:
                combo[bill] = count
                remaining -= bill * count

        if remaining != 0:
            return None

        return combo

    def _compute_change_breakdown_local(self, change_amount):
        if change_amount < 0:
            return None

        if change_amount == 0:
            return {}

        coins = self._coin_inventory_records()
        remaining = int(change_amount)
        breakdown = {}

        for coin in coins:
            denom = int(coin["denomination"])
            stock = int(coin["stock"])

            if denom <= 0 or stock <= 0:
                continue

            use_count = min(stock, remaining // denom)

            if use_count > 0:
                breakdown[denom] = use_count
                remaining -= use_count * denom

        if remaining != 0:
            return None

        return breakdown

    def _count_change_coins(self, breakdown):
        if not breakdown:
            return 0
        return sum(int(v) for v in breakdown.values())

    def _format_bill_combo(self, combo):
        if not combo:
            return "N/A"

        parts = []
        for bill in sorted(combo.keys(), reverse=True):
            count = combo[bill]
            if count > 0:
                parts.append(f"{count}×₱{bill}")

        return ", ".join(parts)

    def _format_change_breakdown(self, breakdown):
        if breakdown is None:
            return "N/A"

        if not breakdown:
            return "No change needed"

        parts = []
        for denom in sorted(breakdown.keys(), reverse=True):
            count = breakdown[denom]
            if count > 0:
                parts.append(f"{count}×₱{denom}")

        return ", ".join(parts)

    def _update_warning_style(self, total):
        threshold = self._exact_only_threshold()

        if int(round(total)) >= threshold:
            warning_bg = "#FBE3E0"
            self.warning_card.configure(fg_color=warning_bg)
            self.warning_title_label.configure(
                text="High amount rule",
                text_color=theme.ERROR,
                fg_color=warning_bg
            )
            self.warning_label.configure(
                text="Exact amount only.",
                text_color=theme.ERROR,
                fg_color=warning_bg,
                font=theme.font(14, "bold"),
                justify="left",
                anchor="w",
            )
        else:
            self.warning_card.configure(fg_color=theme.CREAM)
            self.warning_title_label.configure(
                text="Change rule",
                text_color=theme.BLACK,
                fg_color=theme.CREAM
            )
            self.warning_label.configure(
                text="Higher cash needs available change.",
                text_color=theme.MUTED,
                fg_color=theme.CREAM,
                font=theme.font(13, "bold"),
                justify="left",
                anchor="w",
            )

    def _validate_amount(self, amount):
        total = self._compute_total()
        total_int = int(round(total))
        threshold = self._exact_only_threshold()

        if amount <= 0:
            return False, config.get(
                "cash_bill_check_page",
                "invalid_amount_text",
                default="Enter a valid cash amount."
            )

        if not self._can_compose_amount_from_accepted_bills(amount):
            accepted_text = ", ".join(f"₱{b}" for b in self._accepted_bills())
            return False, (
                f"Use accepted bills only.\n"
                f"Accepted: {accepted_text}\n"
                f"Example: ₱150 = ₱100 + ₱50."
            )

        if amount < total_int:
            return False, (
                f"Amount is too low.\n"
                f"Enter at least ₱{total:.2f}."
            )

        if total_int >= threshold:
            if amount != total_int:
                return False, (
                    f"Exact amount required.\n"
                    f"Enter exactly ₱{total_int:.2f}."
                )

            bill_combo = self._build_bill_combo(amount)
            return True, (
                f"Exact amount confirmed.\n"
                f"Bills: {self._format_bill_combo(bill_combo)}\n\n"
                f"Proceeding to cash payment..."
            )

        change_needed = int(round(amount - total_int))
        change_breakdown = self._compute_change_breakdown_local(change_needed)

        if change_needed > 0 and change_breakdown is None:
            return False, (
                f"Change is not available.\n"
                f"Needed change: ₱{change_needed:.2f}\n"
                f"Enter exact or a smaller valid amount."
            )

        max_change_coins = int(
            config.get("cash_bill_check_page", "max_change_coins", default=self.DEFAULT_MAX_CHANGE_COINS)
        )
        change_coin_count = self._count_change_coins(change_breakdown)

        if change_needed > 0 and change_coin_count > max_change_coins:
            return False, (
                f"Too much change required.\n"
                f"Enter exact or a smaller valid amount."
            )

        bill_combo = self._build_bill_combo(amount)

        if change_needed > 0:
            return True, (
                f"Amount accepted.\n"
                f"Bills: {self._format_bill_combo(bill_combo)}\n"
                f"Change: ₱{change_needed:.2f}\n\n"
                f"Proceeding to cash payment..."
            )

        return True, (
            f"Exact amount accepted.\n"
            f"Bills: {self._format_bill_combo(bill_combo)}\n\n"
            f"Proceeding to cash payment..."
        )

    def submit_amount(self):
        if not self.selected_product:
            self._set_message(
                config.get("cash_bill_check_page", "no_product_text", default="No product selected."),
                theme.ERROR,
                auto_hide_ms=3000
            )
            return

        amount = self._current_amount()
        is_valid, message = self._validate_amount(amount)

        if not is_valid:
            self._set_message(message, theme.ERROR, auto_hide_ms=4000)
            self._focus_entry()
            return

        self._set_message(message, theme.SUCCESS, auto_hide_ms=None)

        self.controller.show_loading_then(
            config.get(
                "cash_bill_check_page",
                "next_loading_text",
                default="Preparing cash payment"
            ),
            "CashPaymentPage",
            delay=1000,
            user_data=self.user_data,
            selected_product=self.selected_product,
            discount=self.discount,
            planned_cash_bill=amount
        )

    def go_back(self):
        self._clear_message_timer()
        self.controller.show_loading_then(
            config.get(
                "cash_bill_check_page",
                "back_loading_text",
                default="Returning to payment options"
            ),
            "PaymentMethodPage",
            delay=1000,
            user_data=self.user_data,
            selected_product=self.selected_product,
            discount=self.discount
        )