from frontend import tk_compat as ctk
from frontend import theme

try:
    from frontend.components import RoundedCard, PillButton
except Exception:
    # Fallback for builds where frontend/components is a folder without an
    # __init__.py exporting these classes.
    from frontend.components.rounded import RoundedCard, PillButton


def _card_body(card):
    return getattr(card, "content", card)


class ErrorDialog(ctk.CTkFrame):
    """
    Full-screen booth warning/error dialog.

    This version always gives the operator a visible way out of non-critical
    warnings through a Dismiss button. Hardware/recovery actions can still use
    a primary button such as Retry / Continue or Reset System.

    Input safety:
      * Dismiss-only dialogs do not install a Tk grab.
      * Action dialogs may install a grab so the operator chooses a recovery
        action first.
      * Every close/destroy path releases the grab to avoid the Raspberry Pi/X11
        issue where the page looks normal but buttons stop responding.
    """

    def __init__(
        self,
        master,
        message,
        title="Something went wrong",
        action_text="Reset System",
        on_action=None,
        on_close=None,
        max_width=700,
        dismiss_text="Dismiss",
    ):
        super().__init__(master, fg_color=theme.CREAM)

        self.master = master
        self.on_action = on_action
        self.on_close = on_close
        self.max_width = max_width
        self.dismiss_text = str(dismiss_text or "Dismiss")

        self.button_row = None
        self.action_button = None
        self.dismiss_button = None
        self._has_grab = False
        self._destroying = False
        self._primary_action_enabled = False

        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.lift()

        try:
            self.focus_set()
        except Exception:
            pass

        self.dialog_card = RoundedCard(
            self,
            auto_size=True,
            pad=16,
            fg_color=theme.WHITE,
            radius=32,
        )
        self.dialog_card.place(relx=0.5, rely=0.5, anchor="center")

        body = _card_body(self.dialog_card)
        try:
            body.configure(bg=theme.WHITE)
        except Exception:
            try:
                body.configure(fg_color=theme.WHITE)
            except Exception:
                pass

        self.inner = ctk.CTkFrame(body, fg_color=theme.WHITE)
        self.inner.pack(fill="both", expand=True, padx=20, pady=20)

        self.accent = ctk.CTkFrame(
            self.inner,
            height=10,
            fg_color=theme.ORANGE,
        )
        self.accent.pack(fill="x", pady=(0, 18))

        self.title_label = ctk.CTkLabel(
            self.inner,
            text=str(title or "Booth Notice"),
            font=theme.heavy(30),
            text_color=theme.BLACK,
            fg_color=theme.WHITE,
            wraplength=self.max_width - 80,
            justify="center",
        )
        self.title_label.pack(pady=(0, 10), padx=20)

        self.message_label = ctk.CTkLabel(
            self.inner,
            text=str(message or ""),
            font=theme.font(18, "bold"),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            wraplength=self.max_width - 80,
            justify="center",
        )
        self.message_label.pack(pady=(0, 22), padx=20)

        self._set_buttons(action_text)
        self._sync_modal_grab()
        self.after_idle(self._recenter)

    def _action_enabled(self, action_text):
        return action_text is not None and str(action_text).strip() != ""

    def _is_dismiss_text(self, action_text):
        clean = str(action_text or "").strip().lower()
        return clean in {"dismiss", "close", "ok", "okay"}

    def _destroy_button_row(self):
        if self.button_row is not None:
            try:
                self.button_row.destroy()
            except Exception:
                pass
        self.button_row = None
        self.action_button = None
        self.dismiss_button = None

    def _set_buttons(self, action_text):
        """Create a primary action button and/or a Dismiss button."""
        self._destroy_button_row()

        text = str(action_text or "").strip()
        has_action = self._action_enabled(text)
        dismiss_only = (not has_action) or self._is_dismiss_text(text)
        self._primary_action_enabled = bool(has_action and not dismiss_only)

        self.button_row = ctk.CTkFrame(self.inner, fg_color=theme.WHITE)
        self.button_row.pack(pady=(0, 2))

        if self._primary_action_enabled:
            self.action_button = PillButton(
                self.button_row,
                text=text,
                width=230,
                height=54,
                command=self._handle_action,
                font=theme.font(17, "bold"),
                fg_color=theme.ORANGE,
                text_color=theme.WHITE,
            )
            self.action_button.pack(side="left", padx=8)

            self.dismiss_button = PillButton(
                self.button_row,
                text=self.dismiss_text,
                width=180,
                height=54,
                command=self.close_without_action,
                font=theme.font(17, "bold"),
                fg_color=getattr(theme, "MUTED", "#6B625A"),
                text_color=theme.WHITE,
            )
            self.dismiss_button.pack(side="left", padx=8)
            return

        # Dismiss-only warning/display dialog.
        self.dismiss_button = PillButton(
            self.button_row,
            text=text or self.dismiss_text,
            width=220,
            height=54,
            command=self.close_without_action,
            font=theme.font(17, "bold"),
            fg_color=theme.ORANGE,
            text_color=theme.WHITE,
        )
        self.dismiss_button.pack(padx=8)

    def _sync_modal_grab(self):
        if self._primary_action_enabled:
            try:
                self.focus_set()
                self.grab_set()
                self._has_grab = True
            except Exception:
                self._has_grab = False
        else:
            self._safe_release()

    def _recenter(self):
        try:
            self.dialog_card.place(relx=0.5, rely=0.5, anchor="center")
        except Exception:
            pass

    def set_message(self, message, title=None, action_text=None):
        if title:
            self.title_label.configure(text=str(title))

        self.message_label.configure(text=str(message or ""))
        self._set_buttons(action_text)
        self._sync_modal_grab()
        self.lift()
        self.after_idle(self._recenter)

    def _safe_release(self):
        try:
            self.grab_release()
        except Exception:
            pass

        try:
            grabbed = self.grab_current()
        except Exception:
            grabbed = None

        if grabbed is self:
            try:
                grabbed.grab_release()
            except Exception:
                pass

        self._has_grab = False

    def close_without_action(self):
        on_close = self.on_close
        self._safe_release()
        try:
            self.destroy()
        except Exception:
            pass
        if callable(on_close):
            try:
                on_close()
            except Exception as e:
                print(f"[ERROR DIALOG] close callback failed: {e}", flush=True)

    def _handle_action(self):
        callback = self.on_action
        self._safe_release()
        try:
            self.destroy()
        except Exception:
            pass
        if callable(callback):
            try:
                callback()
            except Exception as e:
                print(f"[ERROR DIALOG] action callback failed: {e}", flush=True)

    def destroy(self):
        if self._destroying:
            return
        self._destroying = True
        self._safe_release()
        try:
            super().destroy()
        except Exception:
            pass
