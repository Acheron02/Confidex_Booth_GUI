from frontend import tk_compat as ctk
from frontend import theme
from frontend.components import RoundedCard, PillButton
from frontend.widgets import card_body


class ErrorDialog(ctk.CTkFrame):
    """
    Embedded dialog using RoundedCard with auto-size.

    If action_text is None or empty, the dialog is display-only. This is used
    for vending/actuator/hardware errors where the system must not offer a
    retry/reset action from the user screen.
    """

    def __init__(
        self,
        master,
        message,
        title="Something went wrong",
        action_text="Reset System",
        on_action=None,
        on_close=None,  # kept for compatibility
        max_width=640,
    ):
        super().__init__(master, fg_color=theme.CREAM)

        self.master = master
        self.on_action = on_action
        self.on_close = on_close
        self.max_width = max_width
        self.action_button = None
        self.button_row = None

        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.lift()

        try:
            self.focus_set()
            self.grab_set()
        except Exception:
            pass

        self.dialog_card = RoundedCard(
            self,
            auto_size=True,
            pad=16,
        )
        self.dialog_card.place(relx=0.5, rely=0.5, anchor="center")

        body = card_body(self.dialog_card)
        body.configure(bg=theme.WHITE)

        self.inner = ctk.CTkFrame(body, fg_color=theme.WHITE)
        self.inner.pack(fill="both", expand=True, padx=18, pady=18)

        self.accent = ctk.CTkFrame(
            self.inner,
            height=10,
            fg_color=theme.ORANGE,
        )
        self.accent.pack(fill="x", pady=(0, 18))

        self.title_label = ctk.CTkLabel(
            self.inner,
            text=title,
            font=theme.heavy(28),
            text_color=theme.BLACK,
            fg_color=theme.WHITE,
            wraplength=self.max_width - 80,
            justify="center",
        )
        self.title_label.pack(pady=(0, 10), padx=20)

        self.message_label = ctk.CTkLabel(
            self.inner,
            text=message,
            font=theme.font(18, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            wraplength=self.max_width - 80,
            justify="center",
        )
        self.message_label.pack(pady=(0, 22), padx=20)

        self._set_action_button(action_text)
        self.after_idle(self._recenter)

    def _action_enabled(self, action_text):
        return action_text is not None and str(action_text).strip() != ""

    def _set_action_button(self, action_text):
        enabled = self._action_enabled(action_text)

        if not enabled:
            if self.button_row is not None:
                try:
                    self.button_row.destroy()
                except Exception:
                    pass
            self.button_row = None
            self.action_button = None
            return

        if self.button_row is None:
            self.button_row = ctk.CTkFrame(self.inner, fg_color=theme.WHITE)
            self.button_row.pack(pady=(0, 2))

        if self.action_button is None:
            self.action_button = PillButton(
                self.button_row,
                text=str(action_text),
                width=220,
                height=52,
                command=self._handle_action,
                font=theme.font(17, "bold"),
                fg_color=theme.ORANGE,
                text_color=theme.WHITE,
            )
            self.action_button.pack(padx=8)
        else:
            self.action_button.configure(text=str(action_text))

    def _recenter(self):
        try:
            self.dialog_card.place(relx=0.5, rely=0.5, anchor="center")
        except Exception:
            pass

    def set_message(self, message, title=None, action_text=None):
        if title:
            self.title_label.configure(text=title)

        # Passing None intentionally turns this into a display-only dialog.
        self._set_action_button(action_text)

        self.message_label.configure(text=message)
        self.lift()
        self.after_idle(self._recenter)

    def _safe_release(self):
        try:
            self.grab_release()
        except Exception:
            pass

    def _handle_action(self):
        self._safe_release()
        self.destroy()
        if callable(self.on_action):
            try:
                self.on_action()
            except Exception as e:
                print(f"[ERROR DIALOG] action callback failed: {e}", flush=True)
