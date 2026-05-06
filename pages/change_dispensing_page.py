from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, PillButton, card_body
from config_manager import config
from backend.device_sync import mark_inventory_dirty, push_inventory_if_dirty

import threading

from backend.util.dispenser_serial import send_change_command


class ChangeDispensingPage(ctk.CTkFrame):
    REFRESH_MS = 1500

    def __init__(self, master, controller):
        super().__init__(master, fg_color=theme.CREAM)
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

        self.dispense_started = False
        self.dispense_finished = False
        self.redirect_scheduled = False
        self.result = None

        self._dot_job = None
        self._dot_count = 0
        self._animating = False
        self._config_refresh_job = None
        self._base_status_text = config.get(
            "change_dispensing_page",
            "dispensing_status_base_text",
            default="Dispensing coins"
        )

        self.shell = AppShell(
            self,
            title_right=config.get(
                "change_dispensing_page",
                "header_title",
                default="Dispensing Change"
            )
        )
        self.shell.pack(fill="both", expand=True)

        top_bar = ctk.CTkFrame(self.shell.body, fg_color="transparent")
        top_bar.pack(fill="x", padx=28, pady=(16, 8))
        top_bar.grid_columnconfigure(0, weight=1)

        self.page_title = ctk.CTkLabel(
            top_bar,
            text=config.get("change_dispensing_page", "title", default="DISPENSING CHANGE"),
            font=theme.heavy(32),
            text_color=theme.BLACK,
            justify="center",
            anchor="center"
        )
        self.page_title.grid(row=0, column=0, sticky="ew")

        content_wrap = ctk.CTkFrame(self.shell.body, fg_color="transparent")
        content_wrap.pack(expand=True, fill="both", padx=28, pady=(6, 18))
        content_wrap.grid_columnconfigure(0, weight=1)
        content_wrap.grid_rowconfigure(0, weight=1)
        content_wrap.grid_rowconfigure(1, weight=0)
        content_wrap.grid_rowconfigure(2, weight=1)

        self.card = RoundedCard(content_wrap, auto_size=True, pad=18)
        self.card.grid(row=1, column=0)

        body = card_body(self.card)
        body.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            body,
            text=config.get(
                "change_dispensing_page",
                "main_title",
                default="PLEASE COLLECT YOUR CHANGE"
            ),
            font=theme.heavy(30),
            text_color=theme.BLACK,
            fg_color=theme.WHITE
        )
        self.title_label.grid(row=0, column=0, padx=20, pady=(14, 12))

        self.amount_label = ctk.CTkLabel(
            body,
            text="",
            font=theme.heavy(42),
            text_color=theme.ORANGE,
            fg_color=theme.WHITE,
            justify="center"
        )
        self.amount_label.grid(row=1, column=0, padx=20, pady=(4, 12), sticky="ew")

        self.info_label = ctk.CTkLabel(
            body,
            text=config.get(
                "change_dispensing_page",
                "info_text",
                default="The machine is preparing your coins."
            ),
            font=theme.font(20, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            wraplength=760,
            justify="center"
        )
        self.info_label.grid(row=2, column=0, padx=24, pady=(4, 10), sticky="ew")

        self.status_label = ctk.CTkLabel(
            body,
            text=config.get(
                "change_dispensing_page",
                "starting_status_text",
                default="Starting coin dispensing..."
            ),
            font=theme.font(20, "bold"),
            text_color=theme.INFO,
            fg_color=theme.WHITE,
            wraplength=760,
            justify="center"
        )
        self.status_label.grid(row=3, column=0, padx=24, pady=(0, 10), sticky="ew")

        self.breakdown_label = ctk.CTkLabel(
            body,
            text="",
            font=theme.font(18, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            wraplength=760,
            justify="center"
        )
        self.breakdown_label.grid(row=4, column=0, padx=24, pady=(0, 10), sticky="ew")

        self.stock_label = ctk.CTkLabel(
            body,
            text="",
            font=theme.font(15, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            wraplength=760,
            justify="center"
        )
        self.stock_label.grid(row=5, column=0, padx=24, pady=(0, 14), sticky="ew")

        self.continue_btn = PillButton(
            body,
            text=config.get(
                "change_dispensing_page",
                "continue_button_text",
                default="Continue to Receipt"
            ),
            width=240,
            height=56,
            command=self._redirect_to_receipt,
            font=theme.font(16, "bold"),
        )
        self.continue_btn.grid(row=6, column=0, pady=(6, 24))
        self.continue_btn.configure(state="disabled")

        self._start_config_refresh()

    def _refresh_from_config(self):
        try:
            self._base_status_text = config.get(
                "change_dispensing_page",
                "dispensing_status_base_text",
                default="Dispensing coins"
            )

            self.page_title.configure(
                text=config.get("change_dispensing_page", "title", default="DISPENSING CHANGE")
            )

            self.title_label.configure(
                text=config.get(
                    "change_dispensing_page",
                    "main_title",
                    default="PLEASE COLLECT YOUR CHANGE"
                )
            )

            self.info_label.configure(
                text=config.get(
                    "change_dispensing_page",
                    "info_text",
                    default="The machine is preparing your coins."
                )
            )

            self.continue_btn.configure(
                text=config.get(
                    "change_dispensing_page",
                    "continue_button_text",
                    default="Continue to Receipt"
                )
            )

        except Exception as e:
            print(f"[CHANGE] Config refresh failed: {e}", flush=True)

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

    def update_data(
        self,
        user_data=None,
        product=None,
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
        **kwargs
    ):
        self.user_data = user_data or {}
        self.product = product or kwargs.get("selected_product") or {}
        self.discount = float(discount or 0)
        self.total_paid = float(total_paid or 0)
        self.change = float(change or 0)
        self.total = float(total or 0)

        self.online_payment = bool(online_payment)
        self.payment_method = payment_method or "cash"
        self.payment_session_id = payment_session_id
        self.payment_reference = payment_reference
        self.payment_amount = payment_amount
        self.payment_mode = payment_mode
        self.simulated = bool(simulated)
        self.transaction_id = transaction_id

        self.dispense_started = False
        self.dispense_finished = False
        self.redirect_scheduled = False
        self.result = None

        username = self.user_data.get("username", "User")
        self.shell.set_header_right(f"Welcome, {username}!")

        self.amount_label.configure(text=f"₱{self.change:.2f}")
        self.breakdown_label.configure(text="")
        self.stock_label.configure(text="")
        self.continue_btn.configure(state="disabled")

        if self.change <= 0:
            self.status_label.configure(
                text=config.get(
                    "change_dispensing_page",
                    "no_change_text",
                    default="No change to dispense. Redirecting to receipt..."
                ),
                text_color=theme.SUCCESS
            )
            self.after(700, self._redirect_to_receipt)
            return

        self._start_status_animation(self._base_status_text, theme.INFO)
        self.after(150, self._start_dispensing_once)

    def _start_dispensing_once(self):
        if self.dispense_started:
            return

        self.dispense_started = True
        threading.Thread(target=self._dispense_change_thread, daemon=True).start()

    def _dispense_change_thread(self):
        try:
            amount = int(round(self.change))
            breakdown = config.compute_change_breakdown(amount)

            if breakdown is None:
                self.after(
                    0,
                    lambda: self._on_dispense_failed("Cannot compute exact change breakdown.")
                )
                return

            result = send_change_command(breakdown)

            self.after(
                0,
                lambda r=result: self._on_dispense_finished(r)
            )

        except Exception as e:
            self.after(0, lambda err=str(e): self._on_dispense_failed(err))

    def _on_dispense_finished(self, result):
        self.dispense_finished = True
        self.result = result or {}
        self._stop_status_animation()

        success = bool(self.result.get("success"))
        message = str(self.result.get("message") or "")

        if success:
            breakdown = self._extract_breakdown(message)
            parsed_breakdown = self._parse_breakdown_dict(breakdown)

            if parsed_breakdown:
                try:
                    applied = config.apply_change_breakdown(parsed_breakdown)

                    if applied:
                        mark_inventory_dirty()
                        threading.Thread(target=push_inventory_if_dirty, daemon=True).start()
                    else:
                        print("[CHANGE] Failed to deduct coin stock from inventory.json", flush=True)

                except Exception as e:
                    print(f"[CHANGE] Inventory deduction failed: {e}", flush=True)

            self.status_label.configure(
                text=config.get(
                    "change_dispensing_page",
                    "success_status_text",
                    default="Change dispensed successfully."
                ),
                text_color=theme.SUCCESS
            )

            if breakdown:
                self.breakdown_label.configure(
                    text=(
                        f"{config.get('change_dispensing_page', 'dispensed_prefix', default='Dispensed coins')}: "
                        f"{breakdown}"
                    ),
                    text_color=theme.BLACK
                )
            else:
                self.breakdown_label.configure(
                    text=config.get(
                        "change_dispensing_page",
                        "coin_dispense_complete_text",
                        default="Coin dispensing completed."
                    ),
                    text_color=theme.BLACK
                )

            self.stock_label.configure(
                text=self._format_stock_from_inventory(),
                text_color=theme.MUTED
            )

            self.continue_btn.configure(state="normal")

            if not self.redirect_scheduled:
                self.redirect_scheduled = True
                self.after(
                    int(config.get("change_dispensing_page", "auto_redirect_delay_ms", default=1200)),
                    self._redirect_to_receipt
                )

        else:
            self.status_label.configure(
                text=config.get(
                    "change_dispensing_page",
                    "failed_status_text",
                    default="Coin dispensing failed."
                ),
                text_color=theme.ERROR
            )

            self.breakdown_label.configure(
                text=message or config.get(
                    "change_dispensing_page",
                    "dispense_failed_text",
                    default="The machine did not confirm change dispensing."
                ),
                text_color=theme.ERROR
            )

            self.stock_label.configure(
                text=self._format_stock_from_inventory(),
                text_color=theme.MUTED
            )

            self.continue_btn.configure(state="normal")

    def _on_dispense_failed(self, error_text):
        self.dispense_finished = True
        self._stop_status_animation()

        self.status_label.configure(
            text=config.get(
                "change_dispensing_page",
                "failed_status_text",
                default="Coin dispensing failed."
            ),
            text_color=theme.ERROR
        )

        self.breakdown_label.configure(
            text=error_text or config.get(
                "change_dispensing_page",
                "unknown_error_text",
                default="Unknown dispensing error."
            ),
            text_color=theme.ERROR
        )

        self.stock_label.configure(text=self._format_stock_from_inventory())
        self.continue_btn.configure(state="normal")

    def _extract_breakdown(self, message: str) -> str:
        upper = str(message or "").strip()

        if upper.upper().startswith("CHANGE_DISPENSED:"):
            return upper.split(":", 1)[1].strip()

        return ""

    def _parse_breakdown_dict(self, breakdown: str) -> dict:
        result = {}

        if not breakdown:
            return result

        parts = [p.strip() for p in breakdown.split(",") if p.strip()]

        for part in parts:
            if "x" not in part:
                continue

            left, right = part.split("x", 1)

            try:
                denom = int(left.strip())
                qty = int(right.strip())

                if qty > 0:
                    result[denom] = qty

            except Exception:
                continue

        return result

    def _format_stock_from_inventory(self) -> str:
        coins = config.get_coin_inventory()

        if not coins:
            return ""

        parts = []

        for coin in coins:
            denom = coin.get("denomination")
            stock = coin.get("stock")
            parts.append(f"₱{denom}: {stock}")

        return (
            f"{config.get('change_dispensing_page', 'remaining_stock_prefix', default='Remaining coin stock')}: "
            + " | ".join(parts)
        )

    def _start_status_animation(self, base_text=None, color=None):
        self._stop_status_animation()

        self._animating = True
        self._base_status_text = base_text or self._base_status_text
        self._status_color = color or theme.INFO
        self._dot_count = 0

        self._animate_status_text()

    def _stop_status_animation(self):
        self._animating = False

        if self._dot_job is not None:
            try:
                self.after_cancel(self._dot_job)
            except Exception:
                pass

            self._dot_job = None

    def _animate_status_text(self):
        if not self._animating:
            return

        self._dot_count = (self._dot_count + 1) % 4
        dots = "." * self._dot_count

        self.status_label.configure(
            text=f"{self._base_status_text}{dots}",
            text_color=self._status_color
        )

        self._dot_job = self.after(450, self._animate_status_text)

    def _redirect_to_receipt(self):
        self.controller.show_loading_then(
            config.get(
                "change_dispensing_page",
                "receipt_loading_text",
                default="Preparing receipt"
            ),
            "ReceiptPage",
            delay=800,
            user_data=self.user_data,
            product=self.product,
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
            transaction_id=self.transaction_id
        )

    def destroy(self):
        self._stop_status_animation()
        self._cancel_config_refresh()
        super().destroy()