from pathlib import Path
import shutil
import sys

path = Path("pages/cash_payment_page.py")
if len(sys.argv) > 1:
    path = Path(sys.argv[1])

if not path.exists():
    raise SystemExit(
        f"File not found: {path}\n"
        "Run this from the Confidex_GUI_raspi project root, "
        "or pass the file path as an argument."
    )

text = path.read_text(encoding="utf-8")
backup = path.with_suffix(path.suffix + ".bak_homing")

if not backup.exists():
    shutil.copy2(path, backup)


def replace_required(old, new, label):
    global text

    if old not in text:
        raise SystemExit(f"Could not patch {label}: expected text not found")

    text = text.replace(old, new, 1)


def insert_after_required(old, insert, label):
    replace_required(old, old + insert, label)


def method_bounds(src, method_name):
    marker = f"    def {method_name}("
    start = src.find(marker)

    if start < 0:
        raise SystemExit(f"Could not find method: {method_name}")

    pos = start + len(marker)
    candidates = []

    for token in ("\n    def ", "\n    # ---------------------------------------------------------------------"):
        idx = src.find(token, pos)

        if idx >= 0:
            candidates.append(idx)

    if not candidates:
        end = len(src)
    else:
        end = min(candidates)

    return start, end


def replace_method(method_name, new_method):
    global text

    start, end = method_bounds(text, method_name)
    text = text[:start] + new_method.rstrip() + "\n\n" + text[end:].lstrip("\n")


old_import = """from backend.util.dispenser_serial import (
    send_bill_on_command,
    send_bill_off_command,
)"""

new_import = """from backend.util.dispenser_serial import (
    send_bill_on_command,
    send_bill_off_command,
    ensure_kits_homed,
)"""

if "ensure_kits_homed" not in text:
    replace_required(old_import, new_import, "dispenser_serial import")


state_anchor = """        self.bill_acceptor_enabled = False
        self._bill_command_lock = threading.Lock()
"""

state_insert = """
        # Homing gate: cash must not be accepted while the kit dispenser motors
        # are still returning to their home positions.
        self.waiting_for_homing = False
        self._homing_wait_token = 0
        self._homing_wait_lock = threading.Lock()
"""

if "self._homing_wait_token" not in text:
    insert_after_required(state_anchor, state_insert, "homing state variables")


old_refresh_condition = "if not self.transaction_in_progress and not self.loading_visible:"
new_refresh_condition = (
    "if not self.transaction_in_progress and not self.loading_visible "
    "and not self.waiting_for_homing:"
)

if "and not self.waiting_for_homing" not in text:
    replace_required(old_refresh_condition, new_refresh_condition, "refresh condition")


replace_method(
    "enable_bill_acceptor",
    '''    def enable_bill_acceptor(self, async_mode=True):
        if self.waiting_for_homing:
            print("[CASH] BILL_ON blocked because kit homing is still running", flush=True)
            return

        if self.bill_acceptor_enabled:
            return

        if async_mode:
            threading.Thread(target=self._enable_bill_acceptor_thread, daemon=True).start()
        else:
            self._enable_bill_acceptor_thread()
''',
)


homing_methods = '''
    # ---------------------------------------------------------------------
    # HOMING GATE BEFORE CASH INSERTION
    # ---------------------------------------------------------------------

    def _cash_ready_helper_text(self):
        helper_key = "planned_helper_text" if self.planned_cash_bill is not None else "helper_text"

        return config.get(
            "cash_payment_page",
            helper_key,
            default="Insert bills one at a time."
        )

    def _invalidate_homing_wait(self):
        with self._homing_wait_lock:
            self._homing_wait_token += 1
            self.waiting_for_homing = False

    def _show_homing_wait_state(self):
        self.stop_status_animation()
        self.hide_loading()

        self.waiting_for_homing = True
        self.cash_bypass_shortcut_enabled = False

        try:
            self.disable_bill_acceptor()
        except Exception:
            pass

        self._set_status(
            text=config.get(
                "cash_payment_page",
                "homing_wait_status_text",
                default="Please wait for the motors to finish homing."
            ),
            color=ORANGE,
            visible=True
        )

        self._set_helper(
            text=config.get(
                "cash_payment_page",
                "homing_wait_helper_text",
                default="The kiosk is preparing the kit dispenser. Please insert cash only after this message changes."
            ),
            color=MUTED,
            visible=True
        )

        self._set_progress(
            text=config.get(
                "cash_payment_page",
                "homing_wait_progress_text",
                default="Preparing dispenser motors before accepting cash..."
            ),
            color=ORANGE,
            visible=True
        )

        self._set_status_badge("Homing", ORANGE)

    def _show_cash_ready_state(self):
        self.waiting_for_homing = False
        self.cash_bypass_shortcut_enabled = True

        self.hide_loading()

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
            text=self._cash_ready_helper_text(),
            color=MUTED,
            visible=True
        )

        self._set_status_badge("Ready", INFO)
        self.enable_bill_acceptor()

    def _show_homing_failed_state(self, message=""):
        self.waiting_for_homing = False
        self.cash_bypass_shortcut_enabled = False

        self.stop_status_animation()
        self.hide_loading()
        self.disable_bill_acceptor()

        self._set_status(
            text=config.get(
                "cash_payment_page",
                "homing_failed_status_text",
                default="Dispenser motors are not ready."
            ),
            color=ERROR,
            visible=True
        )

        self._set_helper(
            text=message or config.get(
                "cash_payment_page",
                "homing_failed_helper_text",
                default="Please ask for assistance. The bill acceptor will remain disabled."
            ),
            color=ERROR,
            visible=True
        )

        self._set_progress(
            text=config.get(
                "cash_payment_page",
                "homing_failed_progress_text",
                default="Cash payment is temporarily unavailable until homing finishes successfully."
            ),
            color=ERROR,
            visible=True
        )

        self._set_status_badge("Homing Error", ERROR)

    def _start_homing_wait_before_cash(self):
        self._show_homing_wait_state()

        with self._homing_wait_lock:
            self._homing_wait_token += 1
            token = self._homing_wait_token

        threading.Thread(
            target=self._wait_for_homing_before_cash_thread,
            args=(token,),
            daemon=True
        ).start()

    def _wait_for_homing_before_cash_thread(self, token):
        print("[CASH] Waiting for kit motors to finish homing before enabling cash", flush=True)

        try:
            timeout = float(config.get(
                "cash_payment_page",
                "homing_wait_timeout_seconds",
                default=150
            ))
        except Exception:
            timeout = 150.0

        try:
            result = ensure_kits_homed(timeout=timeout)
        except Exception as e:
            result = {
                "success": False,
                "message": str(e),
            }

        print(f"[CASH] Homing wait result before cash: {result}", flush=True)

        def _finish():
            with self._homing_wait_lock:
                if token != self._homing_wait_token:
                    print("[CASH] Ignoring stale homing wait result", flush=True)
                    return

            if not self.user_data or not self.selected_product:
                print("[CASH] Homing wait finished but page no longer has active order", flush=True)
                self.waiting_for_homing = False
                return

            if result.get("success"):
                self._show_cash_ready_state()
                return

            self._show_homing_failed_state(
                message=str(result.get("message") or "Unable to confirm motor homing.")
            )

        try:
            self.controller.after(0, _finish)
        except Exception:
            try:
                self.after(0, _finish)
            except Exception:
                pass

'''

page_flow_marker = """    # ---------------------------------------------------------------------
    # PAGE FLOW
    # ---------------------------------------------------------------------"""

if "HOMING GATE BEFORE CASH INSERTION" not in text:
    replace_required(
        page_flow_marker,
        homing_methods + page_flow_marker,
        "insert homing methods"
    )


old_go_back = """        self.cash_bypass_shortcut_enabled = False

        self.disable_bill_acceptor()"""

new_go_back = """        self.cash_bypass_shortcut_enabled = False
        self._invalidate_homing_wait()

        self.disable_bill_acceptor()"""

if "_invalidate_homing_wait()\n\n        self.disable_bill_acceptor()" not in text:
    replace_required(old_go_back, new_go_back, "go_back homing cancel")


replace_method(
    "update_data",
    '''    def update_data(self, user_data=None, selected_product=None, discount=0, planned_cash_bill=None, **kwargs):
        self.user_data = (user_data or {}).copy()
        self.selected_product = (selected_product or {}).copy() if selected_product else None
        self.discount = discount or 0
        self.planned_cash_bill = planned_cash_bill
        self.total_cash_inserted = 0
        self.transaction_in_progress = False
        self.cash_bypass_shortcut_enabled = True
        self._invalidate_homing_wait()

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

        # Do not immediately enable the bill acceptor.
        # Wait for kit motor homing first so the user sees a proper preparation state.
        self._start_homing_wait_before_cash()
''',
)


replace_method(
    "_pulse_callback",
    '''    def _pulse_callback(self):
        now = time.time()

        if self.waiting_for_homing:
            print("[CASH] Ignored bill pulse because kit homing is still running", flush=True)
            return

        with self.pulse_lock:
            self.pulse_count += 1
            self.last_pulse_time = now
            current_count = self.pulse_count

        print(f"[CASH] pulse detected | count={current_count} | t={now}", flush=True)
''',
)


replace_method(
    "process_bill",
    '''    def process_bill(self, bill_value):
        print(f"[CASH] process_bill called | bill_value={bill_value}", flush=True)

        if self.waiting_for_homing:
            self._set_status(
                text=config.get(
                    "cash_payment_page",
                    "homing_wait_status_text",
                    default="Please wait for the motors to finish homing."
                ),
                color=ORANGE,
                visible=True
            )

            self._set_helper(
                text="Cash is not accepted yet. Please wait for the Ready message.",
                color=MUTED,
                visible=True
            )

            threading.Thread(target=self.reject_bill, daemon=True).start()
            return

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
''',
)


old_reset = """        self.cash_bypass_shortcut_enabled = False

        with self.pulse_lock:"""

new_reset = """        self.cash_bypass_shortcut_enabled = False
        self._invalidate_homing_wait()

        with self.pulse_lock:"""

if "self._invalidate_homing_wait()\n\n        with self.pulse_lock:" not in text:
    replace_required(old_reset, new_reset, "reset_fields homing cancel")


replace_method(
    "destroy",
    '''    def destroy(self):
        self.cash_bypass_shortcut_enabled = False
        self._invalidate_homing_wait()
        self._unbind_cash_bypass_shortcut()
        self.stop_status_animation()
        self._cancel_config_refresh()
        super().destroy()
''',
)


path.write_text(text, encoding="utf-8")

print(f"Patched successfully: {path}")
print(f"Backup saved at: {backup}")