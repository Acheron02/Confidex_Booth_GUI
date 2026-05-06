import os
from pathlib import Path
import tkinter as tk
import vlc

from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, PillButton, card_body
from config_manager import config


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


class HowToUsePage(ctk.CTkFrame):
    REFRESH_MS = 1500
    VIDEO_MONITOR_MS = 500
    SAFE_MAX_VOLUME = 70
    DEFAULT_AUTOPLAY_VOLUME = 70

    def __init__(self, master, controller):
        super().__init__(master, fg_color=CREAM)

        self.controller = controller
        self.user_data = {}
        self.selected_product = None
        self.transaction_id = None

        self._config_refresh_job = None
        self._video_monitor_job = None
        self._redirecting = False

        self.instance = vlc.Instance()
        self.media_player = self.instance.media_player_new()
        self.video_loaded = False
        self.current_video_path = None

        self.shell = AppShell(
            self,
            title_right=config.get("how_to_use_page", "header_title", default="Instructions")
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
        self._build_main_card()

        self.bind("<Configure>", self._sync_layout, add="+")
        self.page.bind("<Configure>", self._sync_layout, add="+")
        self.main_wrap.bind("<Configure>", self._sync_layout, add="+")

    def _build_top_area(self):
        self.top_area = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.top_area.grid(row=0, column=0, sticky="ew", padx=28, pady=(18, 12))
        self.top_area.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.top_area,
            text=config.get("how_to_use_page", "title", default="HOW TO USE THE TEST KIT"),
            font=app_heavy(34),
            text_color=BLACK,
            fg_color=CREAM,
            anchor="center",
            justify="center",
            wraplength=980
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

    def _build_main_card(self):
        self.main_wrap = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.main_wrap.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 20))
        self.main_wrap.grid_columnconfigure(0, weight=1)
        self.main_wrap.grid_rowconfigure(0, weight=1)

        self.card = RoundedCard(
            self.main_wrap,
            fg_color=WHITE,
            radius=36,
            auto_size=False,
            pad=0,
            width=1180,
            height=600
        )
        self.card.grid(row=0, column=0, sticky="nsew")

        body = card_body(self.card)
        safe_configure(body, fg_color=WHITE)

        body.grid_columnconfigure(0, weight=8, uniform="howto-layout")
        body.grid_columnconfigure(1, weight=4, uniform="howto-layout")
        body.grid_rowconfigure(0, weight=1)

        self.left_panel = ctk.CTkFrame(
            body,
            fg_color=WHITE
        )
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(28, 14), pady=26)

        self.right_panel = ctk.CTkFrame(
            body,
            fg_color=WHITE
        )
        self.right_panel.grid(row=0, column=1, sticky="nsew", padx=(14, 28), pady=26)

        self._build_video_panel()
        self._build_instruction_panel()

    def _build_video_panel(self):
        self.left_panel.grid_columnconfigure(0, weight=1)
        self.left_panel.grid_rowconfigure(0, weight=0)
        self.left_panel.grid_rowconfigure(1, weight=1)
        self.left_panel.grid_rowconfigure(2, weight=0)

        self.video_title_row = ctk.CTkFrame(
            self.left_panel,
            fg_color=WHITE
        )
        self.video_title_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.video_title_row.grid_columnconfigure(0, weight=1)
        self.video_title_row.grid_columnconfigure(1, weight=0)

        self.video_title_stack = ctk.CTkFrame(
            self.video_title_row,
            fg_color=WHITE
        )
        self.video_title_stack.grid(row=0, column=0, sticky="ew")
        self.video_title_stack.grid_columnconfigure(0, weight=1)

        self.video_title = ctk.CTkLabel(
            self.video_title_stack,
            text="Tutorial Video",
            font=app_heavy(25),
            text_color=BLACK,
            fg_color=WHITE,
            anchor="w",
            justify="left"
        )
        self.video_title.grid(row=0, column=0, sticky="w")

        self.video_hint = ctk.CTkLabel(
            self.video_title_stack,
            text="Follow each step carefully before proceeding.",
            font=app_font(13, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="w",
            justify="left",
            wraplength=640
        )
        self.video_hint.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.video_badge_frame = ctk.CTkFrame(
            self.video_title_row,
            fg_color="#EEF4FF",
            corner_radius=999
        )
        self.video_badge_frame.grid(row=0, column=1, sticky="e", padx=(16, 0))

        self.video_status_badge = ctk.CTkLabel(
            self.video_badge_frame,
            text="Loading...",
            font=app_font(12, "bold"),
            text_color=INFO,
            fg_color="transparent"
        )
        self.video_status_badge.pack(padx=16, pady=7)

        self.video_outer = ctk.CTkFrame(
            self.left_panel,
            fg_color="#111111",
            corner_radius=30,
            border_width=2,
            border_color="#1F1F1F"
        )
        self.video_outer.grid(row=1, column=0, sticky="nsew")
        self.video_outer.grid_columnconfigure(0, weight=1)
        self.video_outer.grid_rowconfigure(0, weight=1)

        self.video_inner = ctk.CTkFrame(
            self.video_outer,
            fg_color="#111111",
            corner_radius=26
        )
        self.video_inner.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.video_inner.grid_columnconfigure(0, weight=1)
        self.video_inner.grid_rowconfigure(0, weight=1)

        self.video_panel = tk.Frame(
            self.video_inner,
            bg="black",
            width=780,
            height=420,
            highlightthickness=0,
            bd=0
        )
        self.video_panel.grid(row=0, column=0, sticky="nsew")
        self.video_panel.grid_propagate(False)
        self.video_panel.pack_propagate(False)

        self.video_status = ctk.CTkLabel(
            self.left_panel,
            text="",
            font=app_font(14, "bold"),
            text_color=MUTED,
            wraplength=780,
            justify="center",
            anchor="center",
            fg_color=WHITE
        )
        self.video_status.grid(row=2, column=0, sticky="ew", pady=(10, 0))

    def _build_instruction_panel(self):
        self.right_panel.grid_columnconfigure(0, weight=1)
        self.right_panel.grid_rowconfigure(0, weight=0)
        self.right_panel.grid_rowconfigure(1, weight=1)
        self.right_panel.grid_rowconfigure(2, weight=0)

        self.info_card = ctk.CTkFrame(
            self.right_panel,
            fg_color="#FFF9F4",
            corner_radius=30,
            border_width=1,
            border_color="#F0E1D2"
        )
        self.info_card.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.info_card.grid_columnconfigure(0, weight=1)

        self.info_header = ctk.CTkFrame(
            self.info_card,
            fg_color="#FFF9F4"
        )
        self.info_header.grid(row=0, column=0, sticky="ew", padx=22, pady=(20, 22))
        self.info_header.grid_columnconfigure(0, weight=1)

        self.info_badge = ctk.CTkFrame(
            self.info_header,
            fg_color="#FFF2E8",
            corner_radius=999
        )
        self.info_badge.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self.info_badge_text = ctk.CTkLabel(
            self.info_badge,
            text="BEFORE INSERTION",
            font=app_font(12, "bold"),
            text_color=ORANGE,
            fg_color="transparent"
        )
        self.info_badge_text.pack(padx=14, pady=5)

        self.info_title = ctk.CTkLabel(
            self.info_header,
            text="Review first,\ninsert next",
            font=app_heavy(24),
            text_color=BLACK,
            fg_color="#FFF9F4",
            anchor="w",
            justify="left",
            wraplength=340
        )
        self.info_title.grid(row=1, column=0, sticky="ew")

        self.steps_area = ctk.CTkFrame(
            self.right_panel,
            fg_color=WHITE
        )
        self.steps_area.grid(row=1, column=0, sticky="nsew", pady=(0, 14))
        self.steps_area.grid_columnconfigure(0, weight=1)
        self.steps_area.grid_rowconfigure(0, weight=1, uniform="howto-step")
        self.steps_area.grid_rowconfigure(1, weight=1, uniform="howto-step")
        self.steps_area.grid_rowconfigure(2, weight=1, uniform="howto-step")

        self.step_1 = self._make_step_card(
            self.steps_area,
            number="1",
            title="Watch the tutorial",
            text="Review the full guide before handling the kit."
        )
        self.step_1.grid(row=0, column=0, sticky="nsew", pady=(0, 10))

        self.step_2 = self._make_step_card(
            self.steps_area,
            number="2",
            title="Prepare your kit",
            text="Keep the kit flat, clean, and clearly visible."
        )
        self.step_2.grid(row=1, column=0, sticky="nsew", pady=(0, 10))

        self.step_3 = self._make_step_card(
            self.steps_area,
            number="3",
            title="Insert when prompted",
            text="Place the kit only when the booth is ready."
        )
        self.step_3.grid(row=2, column=0, sticky="nsew", pady=(0, 0))

        self.action_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=WHITE,
            corner_radius=24
        )
        self.action_card.grid(row=2, column=0, sticky="ew")
        self.action_card.grid_columnconfigure(0, weight=1)

        self.insert_button = PillButton(
            self.action_card,
            text=config.get(
                "how_to_use_page",
                "continue_text",
                default="Continue"
            ),
            width=310,
            height=66,
            command=self.go_to_insert_kit,
            fg_color=ORANGE,
            text_color=WHITE,
            font=app_font(18, "bold")
        )
        self.insert_button.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.action_note = ctk.CTkLabel(
            self.action_card,
            text="Proceed when ready.",
            font=app_font(12, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="center",
            justify="center",
            wraplength=360
        )
        self.action_note.grid(row=1, column=0, sticky="ew")

    def _make_step_card(self, parent, number, title, text):
        card = ctk.CTkFrame(
            parent,
            fg_color="#FFFFFF",
            corner_radius=24,
            border_width=1,
            border_color="#EFE1D4"
        )
        card.grid_columnconfigure(0, weight=0)
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(0, weight=1)

        number_wrap = ctk.CTkFrame(
            card,
            fg_color="#FFF2E8",
            corner_radius=20,
            width=54,
            height=54
        )
        number_wrap.grid(row=0, column=0, sticky="n", padx=(18, 14), pady=20)
        number_wrap.grid_propagate(False)

        number_label = ctk.CTkLabel(
            number_wrap,
            text=number,
            font=app_font(19, "bold"),
            text_color=ORANGE,
            fg_color="transparent",
            anchor="center",
            justify="center"
        )
        number_label.place(relx=0.5, rely=0.5, anchor="center")

        text_wrap = ctk.CTkFrame(
            card,
            fg_color=WHITE
        )
        text_wrap.grid(row=0, column=1, sticky="nsew", padx=(0, 18), pady=18)
        text_wrap.grid_columnconfigure(0, weight=1)
        text_wrap.grid_rowconfigure(0, weight=0)
        text_wrap.grid_rowconfigure(1, weight=1)

        title_label = ctk.CTkLabel(
            text_wrap,
            text=title,
            font=app_font(17, "bold"),
            text_color=BLACK,
            fg_color=WHITE,
            anchor="w",
            justify="left",
            wraplength=330
        )
        title_label.grid(row=0, column=0, sticky="ew")

        desc_label = ctk.CTkLabel(
            text_wrap,
            text=text,
            font=app_font(13, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="nw",
            justify="left",
            wraplength=330
        )
        desc_label.grid(row=1, column=0, sticky="new", pady=(5, 0))

        return card

    def _sync_layout(self, event=None):
        try:
            self.update_idletasks()

            available_w = max(900, self.main_wrap.winfo_width())
            available_h = max(460, self.main_wrap.winfo_height())

            card_w = min(1240, max(960, int(available_w * 0.96)))
            card_h = min(680, max(520, int(available_h * 0.96)))
            self.card.configure(width=card_w, height=card_h)

            video_area_h = max(280, self.left_panel.winfo_height() - 96)
            video_area_w = max(520, self.left_panel.winfo_width())

            self.video_panel.configure(
                width=video_area_w,
                height=video_area_h
            )

            video_wrap = max(420, self.left_panel.winfo_width() - 40)
            self.video_hint.configure(wraplength=video_wrap)
            self.video_status.configure(wraplength=video_wrap)

            right_wrap = max(260, self.right_panel.winfo_width() - 52)
            self.info_title.configure(wraplength=right_wrap)
            self.action_note.configure(wraplength=right_wrap)

        except Exception:
            pass

    # ---------------------------------------------------------------------
    # Badge helpers
    # ---------------------------------------------------------------------

    def _set_video_badge(self, text, color="neutral"):
        if color == "success":
            bg = "#EAF7EF"
            fg = SUCCESS
        elif color == "warning":
            bg = "#FFF7E0"
            fg = "#9A6700"
        elif color == "error":
            bg = "#FFECEC"
            fg = ERROR
        elif color == "dark":
            bg = "#2D2D2D"
            fg = WHITE
        else:
            bg = "#EEF4FF"
            fg = INFO

        safe_configure(self.video_badge_frame, fg_color=bg)
        self.video_status_badge.configure(text=text, text_color=fg)

    # ---------------------------------------------------------------------
    # Config refresh
    # ---------------------------------------------------------------------

    def _refresh_from_config(self):
        try:
            self.title_label.configure(
                text=config.get("how_to_use_page", "title", default="HOW TO USE THE TEST KIT")
            )

            self.insert_button.configure(
                text=config.get(
                    "how_to_use_page",
                    "continue_text",
                    default="Continue"
                )
            )

            try:
                self.shell.set_header_right(
                    config.get("how_to_use_page", "header_title", default="Instructions")
                    if not self.user_data
                    else f"Welcome, {self.user_data.get('username', 'User')}!"
                )
            except Exception:
                pass

        except Exception as e:
            print(f"[HOWTO] Config refresh failed: {e}", flush=True)

    def _start_config_refresh(self):
        self._refresh_from_config()
        self._config_refresh_job = self.after(self.REFRESH_MS, self._start_config_refresh)

    def _cancel_config_refresh(self):
        if self._config_refresh_job:
            try:
                self.after_cancel(self._config_refresh_job)
            except Exception:
                pass
            self._config_refresh_job = None

    # ---------------------------------------------------------------------
    # Video monitor
    # ---------------------------------------------------------------------

    def _cancel_video_monitor(self):
        if self._video_monitor_job:
            try:
                self.after_cancel(self._video_monitor_job)
            except Exception:
                pass
            self._video_monitor_job = None

    def _start_video_monitor(self):
        self._cancel_video_monitor()
        self._video_monitor_job = self.after(
            self.VIDEO_MONITOR_MS,
            self._monitor_video_completion
        )

    def _monitor_video_completion(self):
        self._video_monitor_job = None

        if self._redirecting:
            return

        try:
            state = self.media_player.get_state()

            if state == vlc.State.Ended:
                self.video_status.configure(
                    text=config.get(
                        "how_to_use_page",
                        "video_finished_text",
                        default="Tutorial finished. Redirecting to kit insertion..."
                    ),
                    text_color=MUTED
                )
                self._set_video_badge("Finished", "success")
                self.go_to_insert_kit()
                return

            if state not in (vlc.State.Stopped, vlc.State.Error, vlc.State.Ended):
                self._start_video_monitor()

        except Exception as e:
            print(f"[HOWTO] _monitor_video_completion failed: {e}", flush=True)

    # ---------------------------------------------------------------------
    # Video path resolution
    # ---------------------------------------------------------------------

    def _get_video_candidates(self):
        candidates = []

        product = self.selected_product or {}

        product_id = str(
            product.get("productID")
            or product.get("product_id")
            or product.get("id")
            or ""
        ).strip()

        product_name = str(product.get("name") or "").strip()
        product_type = str(product.get("type") or "").strip()
        dispense_slot = str(product.get("dispense_slot") or "").strip()

        for key in (
            "video_path",
            "tutorial_video_path",
            "how_to_use_video",
            "instruction_video",
            "instruction_video_path",
        ):
            value = product.get(key)
            if value:
                candidates.append(value)

        video_map = config.get("how_to_use_page", "video_map", default={}) or {}

        for lookup_key in (product_id, product_name, product_type, dispense_slot):
            if lookup_key and isinstance(video_map, dict):
                mapped = video_map.get(lookup_key)
                if mapped:
                    candidates.append(mapped)

        default_video = config.get(
            "how_to_use_page",
            "video_path",
            default=os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "videos",
                "tutorial.mp4"
            )
        )
        candidates.append(default_video)

        return candidates

    def _resolve_video_path(self):
        base_dir = Path(os.path.dirname(os.path.dirname(__file__)))

        for candidate in self._get_video_candidates():
            if not candidate:
                continue

            candidate_path = Path(candidate)

            if not candidate_path.is_absolute():
                candidate_path = base_dir / candidate_path

            if candidate_path.exists():
                return str(candidate_path)

        return None

    # ---------------------------------------------------------------------
    # Audio
    # ---------------------------------------------------------------------

    def _get_safe_volume(self):
        configured = config.get(
            "how_to_use_page",
            "video_volume",
            default=self.DEFAULT_AUTOPLAY_VOLUME
        )

        try:
            configured = int(configured)
        except Exception:
            configured = self.DEFAULT_AUTOPLAY_VOLUME

        configured = max(0, configured)
        configured = min(configured, self.SAFE_MAX_VOLUME)

        return configured

    def _apply_safe_audio(self):
        try:
            safe_volume = self._get_safe_volume()
            self.media_player.audio_set_mute(False)
            self.media_player.audio_set_volume(safe_volume)
        except Exception as e:
            print(f"[HOWTO] _apply_safe_audio failed: {e}", flush=True)

    # ---------------------------------------------------------------------
    # Page flow
    # ---------------------------------------------------------------------

    def update_data(self, user_data=None, selected_product=None, product=None, transaction_id=None, **kwargs):
        self.user_data = user_data or {}
        self.selected_product = selected_product or product
        self._redirecting = False

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

        try:
            self.shell.set_header_right(
                f"Welcome, {self.user_data.get('username', 'User')}!"
            )
        except Exception:
            pass

        self.video_status.configure(text="")
        self._set_video_badge("Loading...", "dark")

        self.reset_video()
        self.after(150, self.show_video)

    def go_to_insert_kit(self):
        if self._redirecting:
            return

        self._redirecting = True
        self._cancel_video_monitor()
        self.stop_video()

        self.controller.show_loading_then(
            config.get(
                "how_to_use_page",
                "next_loading_text",
                default="Preparing reverse vending machine"
            ),
            "KitInsertionPage",
            delay=1000,
            user_data=self.user_data,
            selected_product=self.selected_product,
            transaction_id=self.transaction_id
        )

    # ---------------------------------------------------------------------
    # Video controls
    # ---------------------------------------------------------------------

    def show_video(self):
        try:
            self.video_panel.update_idletasks()

            if os.name == "nt":
                self.media_player.set_hwnd(self.video_panel.winfo_id())
            else:
                self.media_player.set_xwindow(self.video_panel.winfo_id())

            video_path = self._resolve_video_path()

            if not video_path:
                msg = config.get(
                    "how_to_use_page",
                    "video_not_found_text",
                    default="Tutorial video not found."
                )
                self.video_status.configure(text=msg, text_color=ERROR)
                self._set_video_badge("Unavailable", "error")
                return

            if (not self.video_loaded) or (self.current_video_path != video_path):
                media = self.instance.media_new(video_path)
                self.media_player.set_media(media)
                self.video_loaded = True
                self.current_video_path = video_path

            self.video_status.configure(text="")
            self._set_video_badge("Ready", "success")
            self.after(100, self.play_video)

        except Exception as e:
            print(f"[HOWTO] show_video failed: {e}", flush=True)

            self.video_status.configure(
                text=f"{config.get('how_to_use_page', 'video_error_prefix', default='Video error:')} {e}",
                text_color=ERROR
            )
            self._set_video_badge("Error", "error")

    def play_video(self):
        try:
            self._apply_safe_audio()
            self.media_player.play()

            self.after(250, self._apply_safe_audio)
            self.after(1000, self._apply_safe_audio)

            self._set_video_badge(
                f"Playing • Vol {self._get_safe_volume()}",
                "success"
            )

            self._start_video_monitor()

        except Exception as e:
            print(f"[HOWTO] play_video failed: {e}", flush=True)
            self._set_video_badge("Error", "error")

    def pause_video(self):
        try:
            self.media_player.pause()
            self._cancel_video_monitor()
            self._set_video_badge("Paused", "warning")
        except Exception as e:
            print(f"[HOWTO] pause_video failed: {e}", flush=True)

    def stop_video(self):
        try:
            self._cancel_video_monitor()
            self.media_player.stop()
            self.media_player.audio_set_volume(0)
            self._set_video_badge("Stopped", "neutral")
        except Exception as e:
            print(f"[HOWTO] stop_video failed: {e}", flush=True)

    def reset_video(self):
        self.stop_video()
        self.video_loaded = False
        self.current_video_path = None
        self.video_status.configure(text="")

    def destroy(self):
        self._cancel_config_refresh()
        self._cancel_video_monitor()
        self.reset_video()
        super().destroy()