import cv2
from PIL import Image, ImageTk

from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, PillButton, card_body
from backend.util.capture_manager import get_or_create_capture_session
from config_manager import config
from backend.payment_recovery import mark_payment_completed
from backend.system_events import report_error, report_warning

try:
    from backend.flow_state import clear_active_flow
except Exception as flow_import_error:
    clear_active_flow = None
    _FLOW_STATE_IMPORT_ERROR = flow_import_error
else:
    _FLOW_STATE_IMPORT_ERROR = None

try:
    from backend.util.kit_queue_worker import (
        enqueue_kit_job,
        get_latest_queue_frame,
        get_queue_job_by_transaction,
    )
except Exception as import_error:
    enqueue_kit_job = None
    get_latest_queue_frame = None
    get_queue_job_by_transaction = None
    _KIT_QUEUE_IMPORT_ERROR = import_error
else:
    _KIT_QUEUE_IMPORT_ERROR = None


# ---------------------------------------------------------------------
# Theme-safe helpers
# ---------------------------------------------------------------------

def _theme(name, fallback):
    return getattr(theme, name, fallback)


BLACK = _theme("BLACK", "#000000")
CREAM = _theme("CREAM", "#F5F2DE")
ORANGE = _theme("ORANGE", "#C46A2A")
WHITE = _theme("WHITE", "#FFFFFF")
MUTED = _theme("MUTED", "#555555")
SUCCESS = _theme("SUCCESS", "#237B4B")
ERROR = _theme("ERROR", "#B3261E")
INFO = _theme("INFO", "#2457A5")


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


def format_queue_delay(value=None):
    try:
        minutes = float(value)
    except Exception:
        minutes = 30.0

    total_seconds = max(1, int(round(minutes * 60)))

    if total_seconds < 60:
        unit = "second" if total_seconds == 1 else "seconds"
        return f"{total_seconds} {unit}"

    whole_minutes = total_seconds // 60
    remaining_seconds = total_seconds % 60

    if remaining_seconds == 0:
        unit = "minute" if whole_minutes == 1 else "minutes"
        return f"{whole_minutes} {unit}"

    minute_unit = "minute" if whole_minutes == 1 else "minutes"
    second_unit = "second" if remaining_seconds == 1 else "seconds"
    return f"{whole_minutes} {minute_unit} and {remaining_seconds} {second_unit}"


class KitInsertionPage(ctk.CTkFrame):
    """
    Queue-based kit insertion page.

    The queue worker owns the camera.
    This page only displays the worker's latest published frame.

    Important:
    - The worker now publishes a clean result-only frame after analysis.
    - The uploaded original image is handled by kit_queue_worker.py.
    """

    REFRESH_MS = 1500
    PREVIEW_MS = 180

    def __init__(self, master, controller):
        super().__init__(master, fg_color=CREAM)

        self.controller = controller
        self.user_data = {}
        self.selected_product = None
        self.transaction_id = None
        self.payment_session_id = None

        self._config_refresh_job = None
        self._preview_job = None
        self._camera_imgtk = None
        self._busy = False
        self._queue_retry_job = None
        self._queue_retry_count = 0
        self._queued_job = None
        self._preview_error_count = 0
        self.MAX_QUEUE_RETRIES = int(config.get("kit_insertion_page", "queue_retry_max_attempts", default=5))
        self.QUEUE_RETRY_DELAY_MS = int(config.get("kit_insertion_page", "queue_retry_delay_ms", default=3000))

        self._step_cards = []
        self._step_title_labels = []
        self._step_desc_labels = []

        self.shell = AppShell(
            self,
            title_right=config.get(
                "kit_insertion_page",
                "header_title",
                default="Reverse Vending Machine",
            ),
        )
        self.shell.pack(fill="both", expand=True)

        self._build_ui()
        self._start_config_refresh()
        self._start_preview_loop()

    def _queue_delay_text(self):
        return format_queue_delay(
            config.get("kit_queue", "delay_minutes", default=30)
        )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.page = ctk.CTkFrame(self.shell.body, fg_color=CREAM)
        self.page.pack(fill="both", expand=True)

        self.page.grid_columnconfigure(0, weight=1)
        self.page.grid_rowconfigure(0, weight=0)
        self.page.grid_rowconfigure(1, weight=1)

        self._build_top_area()
        self._build_main_card()

        self.bind("<Configure>", self._sync_layout, add="+")
        self.page.bind("<Configure>", self._sync_layout, add="+")
        self.main_wrap.bind("<Configure>", self._sync_layout, add="+")
        self.left_panel.bind("<Configure>", self._sync_layout, add="+")
        self.right_panel.bind("<Configure>", self._sync_layout, add="+")

    def _build_top_area(self):
        self.top_area = ctk.CTkFrame(self.page, fg_color=CREAM)
        self.top_area.grid(row=0, column=0, sticky="ew", padx=28, pady=(14, 10))
        self.top_area.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.top_area,
            text=config.get(
                "kit_insertion_page",
                "title",
                default="REVERSE VENDING MACHINE",
            ),
            font=app_heavy(32),
            text_color=BLACK,
            fg_color=CREAM,
            anchor="center",
            justify="center",
            wraplength=1200,
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

    def _build_main_card(self):
        self.main_wrap = ctk.CTkFrame(self.page, fg_color=CREAM)
        self.main_wrap.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))
        self.main_wrap.grid_columnconfigure(0, weight=1)
        self.main_wrap.grid_rowconfigure(0, weight=1)

        self.card = RoundedCard(
            self.main_wrap,
            fg_color=WHITE,
            radius=28,
            auto_size=False,
            pad=0,
            width=1180,
            height=640,
        )
        self.card.grid(row=0, column=0, sticky="nsew")

        body = card_body(self.card)
        safe_configure(body, fg_color=WHITE)

        body.grid_columnconfigure(0, weight=8, uniform="kit-layout")
        body.grid_columnconfigure(1, weight=4, uniform="kit-layout")
        body.grid_rowconfigure(0, weight=1)

        self.left_panel = ctk.CTkFrame(body, fg_color=WHITE)
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(18, 12), pady=18)

        self.right_panel = ctk.CTkFrame(body, fg_color=WHITE)
        self.right_panel.grid(row=0, column=1, sticky="nsew", padx=(12, 18), pady=18)

        self._build_camera_panel()
        self._build_instruction_panel()

    def _build_camera_panel(self):
        self.left_panel.grid_columnconfigure(0, weight=1)
        self.left_panel.grid_rowconfigure(0, weight=0)
        self.left_panel.grid_rowconfigure(1, weight=1)
        self.left_panel.grid_rowconfigure(2, weight=0)

        self.camera_header = ctk.CTkFrame(self.left_panel, fg_color=WHITE)
        self.camera_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.camera_header.grid_columnconfigure(0, weight=1)
        self.camera_header.grid_columnconfigure(1, weight=0)

        self.camera_title_stack = ctk.CTkFrame(self.camera_header, fg_color=WHITE)
        self.camera_title_stack.grid(row=0, column=0, sticky="ew", padx=(0, 14))
        self.camera_title_stack.grid_columnconfigure(0, weight=1)

        self.camera_title = ctk.CTkLabel(
            self.camera_title_stack,
            text="Queue Camera Preview",
            font=app_heavy(25),
            text_color=BLACK,
            fg_color=WHITE,
            anchor="w",
            justify="left",
        )
        self.camera_title.grid(row=0, column=0, sticky="w")

        delay_text = self._queue_delay_text()
        self.camera_hint = ctk.CTkLabel(
            self.camera_title_stack,
            text=(
                "This is the queue worker camera view. "
                f"Your timer starts when you confirm insertion. Processing happens after about {delay_text}."
            ),
            font=app_font(13, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self.camera_hint.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.camera_badge_frame = ctk.CTkFrame(
            self.camera_header,
            fg_color="#EAF7EF",
            corner_radius=999,
        )
        self.camera_badge_frame.grid(row=0, column=1, rowspan=2, sticky="ne", pady=(8, 0))

        self.camera_badge = ctk.CTkLabel(
            self.camera_badge_frame,
            text="Starting",
            font=app_font(12, "bold"),
            text_color=SUCCESS,
            fg_color="transparent",
        )
        self.camera_badge.pack(padx=16, pady=7)

        self.camera_outer = ctk.CTkFrame(
            self.left_panel,
            fg_color="#111111",
            corner_radius=18,
            border_width=2,
            border_color="#1F1F1F",
        )
        self.camera_outer.grid(row=1, column=0, sticky="nsew")
        self.camera_outer.grid_columnconfigure(0, weight=1)
        self.camera_outer.grid_rowconfigure(0, weight=1)

        self.camera_inner = ctk.CTkFrame(
            self.camera_outer,
            fg_color="#111111",
            corner_radius=14,
        )
        self.camera_inner.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.camera_inner.grid_columnconfigure(0, weight=1)
        self.camera_inner.grid_rowconfigure(0, weight=1)

        self.camera_label = ctk.CTkLabel(
            self.camera_inner,
            text=(
                "Waiting for queue camera preview...\n\n"
                "Insert the used kit into the return slot,\n"
                "then tap Confirm Insertion."
            ),
            font=app_heavy(22),
            text_color=WHITE,
            fg_color="#000000",
            justify="center",
            anchor="center",
            wraplength=760,
        )
        self.camera_label.grid(row=0, column=0, sticky="nsew")

        self.result_panel = ctk.CTkFrame(
            self.left_panel,
            fg_color="#FFFDF8",
            corner_radius=0,
            border_width=1,
            border_color="#EBD8C6",
        )
        self.result_panel.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        self.result_panel.grid_columnconfigure(0, weight=1)

        self.confirmation_label = ctk.CTkLabel(
            self.result_panel,
            text="",
            font=app_font(14, "bold"),
            text_color=SUCCESS,
            fg_color="#FFFDF8",
            justify="center",
            anchor="center",
            wraplength=900,
        )

        self.result_label = ctk.CTkLabel(
            self.result_panel,
            text=config.get(
                "kit_insertion_page",
                "initial_text",
                default="Insert your test kit",
            ),
            font=app_heavy(24),
            text_color=BLACK,
            fg_color="#FFFDF8",
            justify="center",
            anchor="center",
            wraplength=900,
        )
        self.result_label.grid(row=0, column=0, sticky="ew", padx=20, pady=18)

    def _build_instruction_panel(self):
        self.right_panel.grid_columnconfigure(0, weight=1)
        self.right_panel.grid_rowconfigure(0, weight=0)
        self.right_panel.grid_rowconfigure(1, weight=1)
        self.right_panel.grid_rowconfigure(2, weight=0)

        self.info_card = ctk.CTkFrame(
            self.right_panel,
            fg_color="#FFF9F4",
            corner_radius=0,
            border_width=1,
            border_color="#F0E1D2",
        )
        self.info_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.info_card.grid_columnconfigure(0, weight=1)

        self.info_inner = ctk.CTkFrame(self.info_card, fg_color="#FFF9F4")
        self.info_inner.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 18))
        self.info_inner.grid_columnconfigure(0, weight=1)

        self.info_badge = ctk.CTkFrame(
            self.info_inner,
            fg_color="#FFF2E8",
            corner_radius=0,
        )
        self.info_badge.grid(row=0, column=0, sticky="", pady=(0, 10))

        self.info_badge_text = ctk.CTkLabel(
            self.info_badge,
            text="KIT QUEUE",
            font=app_font(12, "bold"),
            text_color=ORANGE,
            fg_color="transparent",
        )
        self.info_badge_text.pack(padx=12, pady=5)

        self.info_title = ctk.CTkLabel(
            self.info_inner,
            text="Return the kit\nsafely",
            font=app_heavy(22),
            text_color=BLACK,
            fg_color="#FFF9F4",
            anchor="center",
            justify="center",
            wraplength=480,
        )
        self.info_title.grid(row=1, column=0, sticky="ew")

        self.info_desc = ctk.CTkLabel(
            self.info_inner,
            text="You can leave after confirming. The booth will process the kit in the background.",
            font=app_font(12, "normal"),
            text_color=MUTED,
            fg_color="#FFF9F4",
            anchor="center",
            justify="center",
            wraplength=480,
        )
        self.info_desc.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        self.steps_area = ctk.CTkFrame(self.right_panel, fg_color=WHITE)
        self.steps_area.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        self.steps_area.grid_columnconfigure(0, weight=1)
        self.steps_area.grid_rowconfigure(0, weight=1)
        self.steps_area.grid_rowconfigure(1, weight=1)
        self.steps_area.grid_rowconfigure(2, weight=2)

        self.step_1 = self._make_step_card(
            self.steps_area,
            number="1",
            title="Insert the used kit",
            text="Place the completed kit into the return slot.",
        )
        self.step_1.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        self.step_2 = self._make_step_card(
            self.steps_area,
            number="2",
            title="Confirm insertion",
            text="Tap confirm only after the kit is fully inside.",
        )
        self.step_2.grid(row=1, column=0, sticky="nsew", pady=(0, 8))

        self.step_3 = self._make_step_card(
            self.steps_area,
            number="3",
            title="Check online later",
            text="After the timer, the booth analyzes and uploads your result. The kit is then disposed safely.",
        )
        self.step_3.grid(row=2, column=0, sticky="nsew")

        self.action_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=WHITE,
            corner_radius=0,
        )
        self.action_card.grid(row=2, column=0, sticky="ew")
        self.action_card.grid_columnconfigure(0, weight=1)

        self.insert_btn = PillButton(
            self.action_card,
            text=config.get(
                "kit_insertion_page",
                "confirm_button_text",
                default="Confirm Insertion",
            ),
            command=self.confirm_insertion,
            width=310,
            height=58,
            fg_color=ORANGE,
            text_color=WHITE,
            font=app_font(18, "bold"),
        )
        self.insert_btn.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        self.action_note = ctk.CTkLabel(
            self.action_card,
            text=f"The {self._queue_delay_text()} timer starts when you confirm insertion.",
            font=app_font(12, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="center",
            justify="center",
            wraplength=480,
        )
        self.action_note.grid(row=1, column=0, sticky="ew")

    def _make_step_card(self, parent, number, title, text):
        card = ctk.CTkFrame(
            parent,
            fg_color=WHITE,
            corner_radius=0,
            border_width=1,
            border_color="#EFE1D4",
        )
        card.grid_columnconfigure(0, weight=0)
        card.grid_columnconfigure(1, weight=1)
        card.grid_rowconfigure(0, weight=1)

        number_wrap = ctk.CTkFrame(
            card,
            fg_color="#FFF2E8",
            corner_radius=0,
            width=44,
            height=44,
        )
        number_wrap.grid(row=0, column=0, sticky="nw", padx=(14, 12), pady=14)
        number_wrap.grid_propagate(False)

        number_label = ctk.CTkLabel(
            number_wrap,
            text=number,
            font=app_font(16, "bold"),
            text_color=ORANGE,
            fg_color="transparent",
            anchor="center",
            justify="center",
        )
        number_label.place(relx=0.5, rely=0.5, anchor="center")

        text_wrap = ctk.CTkFrame(card, fg_color=WHITE)
        text_wrap.grid(row=0, column=1, sticky="nsew", padx=(0, 16), pady=12)
        text_wrap.grid_columnconfigure(0, weight=1)
        text_wrap.grid_rowconfigure(0, weight=0)
        text_wrap.grid_rowconfigure(1, weight=1)

        title_label = ctk.CTkLabel(
            text_wrap,
            text=title,
            font=app_font(16, "bold"),
            text_color=BLACK,
            fg_color=WHITE,
            anchor="w",
            justify="left",
            wraplength=480,
        )
        title_label.grid(row=0, column=0, sticky="ew")

        desc_label = ctk.CTkLabel(
            text_wrap,
            text=text,
            font=app_font(12, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="nw",
            justify="left",
            wraplength=480,
        )
        desc_label.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

        self._step_cards.append(card)
        self._step_title_labels.append(title_label)
        self._step_desc_labels.append(desc_label)

        return card

    # ------------------------------------------------------------------
    # Shared camera preview from queue worker
    # ------------------------------------------------------------------

    def _start_preview_loop(self):
        self._cancel_preview_loop()
        self._preview_job = self.after(self.PREVIEW_MS, self._preview_loop)

    def _cancel_preview_loop(self):
        if self._preview_job:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
            self._preview_job = None

    def _preview_loop(self):
        try:
            self._update_shared_preview()
        except Exception as e:
            self._preview_error_count += 1
            print(f"[KIT] Preview update failed: {e}", flush=True)
            if self._preview_error_count in {5, 20, 60}:
                try:
                    report_warning(
                        "kit_insertion",
                        "Camera Preview Recovering",
                        "The booth camera preview is temporarily unavailable. The system is retrying automatically.",
                        details={"error": str(e), "attempts": self._preview_error_count},
                        visible=True,
                    )
                except Exception:
                    pass
        finally:
            self._preview_job = self.after(self.PREVIEW_MS, self._preview_loop)

    def _get_frame_from_worker(self):
        if get_latest_queue_frame is None:
            return None, None

        payload = get_latest_queue_frame()

        if payload is None:
            return None, None

        if isinstance(payload, dict):
            return payload.get("frame"), payload

        return payload, {"frame": payload}

    def _update_shared_preview(self):
        frame, meta = self._get_frame_from_worker()

        if frame is None:
            if _KIT_QUEUE_IMPORT_ERROR is not None:
                self._set_camera_badge("Worker missing", "error")
                self.camera_label.configure(
                    image=None,
                    text=(
                        "Queue worker preview is not available.\n\n"
                        "Create backend/util/kit_queue_worker.py\n"
                        "with get_latest_queue_frame()."
                    ),
                )
            return

        label_w = max(320, self.camera_label.winfo_width())
        label_h = max(240, self.camera_label.winfo_height())

        if label_w <= 1 or label_h <= 1:
            return

        try:
            if len(frame.shape) == 3 and frame.shape[2] == 3:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            else:
                rgb = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGB)

            h, w = rgb.shape[:2]
            scale = min(label_w / max(1, w), label_h / max(1, h))
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))

            resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)

            canvas = Image.new("RGB", (label_w, label_h), (0, 0, 0))
            img = Image.fromarray(resized)

            x = (label_w - new_w) // 2
            y = (label_h - new_h) // 2
            canvas.paste(img, (x, y))

            self._camera_imgtk = ImageTk.PhotoImage(canvas)
            self.camera_label.configure(image=self._camera_imgtk, text="")
            self._preview_error_count = 0

            state = str((meta or {}).get("state") or "live").strip().lower()
            message = str((meta or {}).get("message") or "").strip()

            if state in ("processing", "capturing", "analyzing", "disposing", "uploading"):
                self._set_camera_badge("Processing", "warning")
            elif state in ("dispose_pending", "upload_pending", "retrying"):
                self._set_camera_badge("Pending", "warning")
            elif state in ("result_ready", "completed", "disposed"):
                self._set_camera_badge("Done", "success")
            elif state in ("error", "camera_error", "worker_error"):
                self._set_camera_badge("Camera error", "error")
            else:
                self._set_camera_badge("Ready", "success")

            # Do not add extra UI annotations here. The frame from the worker is already clean.
            if state in ("result_ready", "completed") and message:
                print(f"[KIT] Worker status: {message}", flush=True)

        except Exception as e:
            print(f"[KIT] Failed to render shared preview: {e}", flush=True)

    # ------------------------------------------------------------------
    # UI state helpers
    # ------------------------------------------------------------------

    def _set_camera_badge(self, text, color="neutral"):
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

        safe_configure(self.camera_badge_frame, fg_color=bg)
        self.camera_badge.configure(text=text, text_color=fg)

    def _set_status(self, result_text=None, confirm_text=None, result_color=None, confirm_color=None):
        if confirm_text is not None:
            clean_confirm = str(confirm_text or "").strip()

            if clean_confirm:
                self.confirmation_label.configure(
                    text=clean_confirm,
                    text_color=confirm_color or SUCCESS,
                )
                self.confirmation_label.grid(
                    row=0,
                    column=0,
                    sticky="ew",
                    padx=20,
                    pady=(14, 2),
                )
                self.result_label.grid_configure(row=1, pady=(0, 14))
            else:
                self.confirmation_label.configure(text="")
                self.confirmation_label.grid_remove()
                self.result_label.grid_configure(row=0, pady=18)

        if result_text is not None:
            self.result_label.configure(text=result_text, text_color=result_color or BLACK)

    def _sync_layout(self, event=None):
        try:
            self.update_idletasks()

            available_w = max(1000, self.main_wrap.winfo_width())
            available_h = max(520, self.main_wrap.winfo_height())

            card_w = min(1400, max(1080, int(available_w * 0.99)))
            card_h = min(760, max(600, int(available_h * 0.99)))

            self.card.configure(width=card_w, height=card_h)

            left_wrap = max(500, self.left_panel.winfo_width() - 36)
            right_wrap = max(320, self.right_panel.winfo_width() - 26)

            badge_w = max(96, self.camera_badge_frame.winfo_width())
            header_w = max(400, self.camera_header.winfo_width())
            camera_hint_wrap = max(300, header_w - badge_w - 36)

            self.camera_hint.configure(wraplength=camera_hint_wrap)
            self.camera_label.configure(wraplength=max(420, left_wrap - 40))
            self.result_label.configure(wraplength=left_wrap)
            self.confirmation_label.configure(wraplength=left_wrap)

            info_wrap = max(280, self.info_card.winfo_width() - 44)
            self.info_title.configure(wraplength=info_wrap)
            self.info_desc.configure(wraplength=info_wrap)
            self.action_note.configure(wraplength=max(280, self.action_card.winfo_width() - 18))

            for card, title_label, desc_label in zip(
                self._step_cards, self._step_title_labels, self._step_desc_labels
            ):
                card_w = max(280, card.winfo_width())
                usable_text_w = max(220, card_w - 92)
                title_label.configure(wraplength=usable_text_w)
                desc_label.configure(wraplength=usable_text_w)

        except Exception:
            pass

    # ------------------------------------------------------------------
    # Config refresh
    # ------------------------------------------------------------------

    def _refresh_from_config(self):
        try:
            self.title_label.configure(
                text=config.get(
                    "kit_insertion_page",
                    "title",
                    default="REVERSE VENDING MACHINE",
                )
            )

            self.insert_btn.configure(
                text=config.get(
                    "kit_insertion_page",
                    "confirm_button_text",
                    default="Confirm Insertion",
                )
            )

            delay_text = self._queue_delay_text()

            self.camera_hint.configure(
                text=(
                    "This is the queue worker camera view. "
                    f"Your timer starts when you confirm insertion. Processing happens after about {delay_text}."
                )
            )

            self.action_note.configure(
                text=f"The {delay_text} timer starts when you confirm insertion."
            )

            try:
                self.shell.set_header_right(
                    config.get(
                        "kit_insertion_page",
                        "header_title",
                        default="Reverse Vending Machine",
                    )
                    if not self.user_data
                    else f"Welcome, {self.user_data.get('username', 'User')}!"
                )
            except Exception:
                pass

        except Exception as e:
            print(f"[KIT] Config refresh failed: {e}", flush=True)

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

    # ------------------------------------------------------------------
    # Queue action
    # ------------------------------------------------------------------

    def _get_user_id(self):
        return (
            self.user_data.get("user_id")
            or self.user_data.get("userID")
            or self.user_data.get("_id")
            or self.user_data.get("id")
            or ""
        )

    def _get_product_id(self):
        if not self.selected_product:
            return ""

        return (
            self.selected_product.get("productID")
            or self.selected_product.get("product_id")
            or self.selected_product.get("_id")
            or self.selected_product.get("id")
            or ""
        )

    def _get_product_name(self):
        if not self.selected_product:
            return ""

        return (
            self.selected_product.get("name")
            or self.selected_product.get("product_name")
            or self.selected_product.get("productName")
            or ""
        )

    def _get_transaction_id(self):
        return (
            self.transaction_id
            or self.user_data.get("transaction_id")
            or self.user_data.get("transactionID")
            or self.user_data.get("latest_transaction_id")
            or self.user_data.get("latestTransactionId")
            or ""
        )

    def _cancel_queue_retry(self):
        if self._queue_retry_job:
            try:
                self.after_cancel(self._queue_retry_job)
            except Exception:
                pass
            self._queue_retry_job = None

    def _schedule_queue_retry(self, error):
        self._cancel_queue_retry()

        if self._queue_retry_count >= self.MAX_QUEUE_RETRIES:
            self._set_status(
                confirm_text="",
                result_text="The booth could not queue the kit automatically. Please call an operator, then tap Retry / Continue.",
                result_color=ERROR,
            )
            return

        self._queue_retry_count += 1
        delay_ms = max(1000, int(self.QUEUE_RETRY_DELAY_MS))
        self._set_status(
            confirm_text=(
                f"Temporary queue issue. Retrying automatically "
                f"({self._queue_retry_count}/{self.MAX_QUEUE_RETRIES})..."
            ),
            result_text=str(error),
            confirm_color=INFO,
            result_color=ERROR,
        )
        self._set_camera_badge("Retrying queue", "warning")
        self._queue_retry_job = self.after(delay_ms, self._retry_queue_after_error)

    def _retry_queue_after_error(self):
        self._queue_retry_job = None
        self._busy = False
        try:
            self.insert_btn.configure(state="normal")
        except Exception:
            pass
        self.confirm_insertion()

    def recover_from_error(self):
        """Manual Retry / Continue action used by the global error dialog."""
        self._cancel_queue_retry()
        self._busy = False
        try:
            self.insert_btn.configure(state="normal")
        except Exception:
            pass
        self.start_camera()
        self.confirm_insertion()

    def schedule_auto_recovery(self, event=None):
        """Let KitInsertionPage self-heal after recoverable queue/camera errors."""
        if self._busy:
            return
        if self._queued_job:
            return
        if not self._queue_retry_job:
            self._schedule_queue_retry((event or {}).get("message") or "Recovering kit insertion step.")

    def _finish_paid_booth_flow_after_queue(self, job=None, reason="kit_queued"):
        """Clear only this user's durable paid-flow guard after the used kit is queued.

        Once the kit insertion is confirmed and a queue job exists, the user-facing
        paid booth flow is already finished. Capture, upload, analysis, and disposal
        are background queue responsibilities.

        This must clear only the current user's transaction. It must not delete
        another user's unfinished paid flow.
        """
        transaction_id = ""

        try:
            if isinstance(job, dict):
                transaction_id = str(job.get("transaction_id") or "").strip()
        except Exception:
            transaction_id = ""

        if not transaction_id:
            transaction_id = str(self._get_transaction_id() or "").strip()

        if clear_active_flow is not None:
            try:
                clear_active_flow(
                    user_data=self.user_data,
                    transaction_id=transaction_id,
                )
                print(
                    f"[KIT] Cleared this user's active flow after kit insertion was queued. "
                    f"tx={transaction_id} reason={reason}",
                    flush=True,
                )
            except Exception as e:
                print(f"[KIT] Failed to clear active flow after queueing kit: {e}", flush=True)
        else:
            print(
                f"[KIT] flow_state.clear_active_flow unavailable; import_error={_FLOW_STATE_IMPORT_ERROR}",
                flush=True,
            )

        # Also clear only the in-memory controller state for the currently logged-in user.
        # Durable flows for other users remain in active_flows.
        try:
            self.controller.current_user = None
            self.controller.selected_product = None
            self.controller.current_transaction_id = None
            self.controller.active_flow_kwargs = {}
        except Exception:
            pass

    def confirm_insertion(self):
        try:
            if self._busy:
                return

            if enqueue_kit_job is None:
                raise RuntimeError(
                    "Kit queue worker is not available. "
                    f"Import error: {_KIT_QUEUE_IMPORT_ERROR}"
                )

            if self._queued_job:
                self._set_camera_badge("Queued", "success")
                self._set_status(
                    confirm_text="Kit is already queued for analysis.",
                    result_text=f"Your result will appear on the website after about {self._queue_delay_text()}.",
                    confirm_color=SUCCESS,
                    result_color=SUCCESS,
                )
                return

            existing_tx = str(self._get_transaction_id()).strip()
            if existing_tx and get_queue_job_by_transaction is not None:
                existing_job = get_queue_job_by_transaction(existing_tx)
                if existing_job:
                    self._queued_job = existing_job
                    self._set_camera_badge("Queued", "success")
                    self._set_status(
                        confirm_text="Kit is already queued for analysis.",
                        result_text=f"Your result will appear on the website after about {self._queue_delay_text()}.",
                        confirm_color=SUCCESS,
                        result_color=SUCCESS,
                    )
                    self._finish_paid_booth_flow_after_queue(
                        existing_job,
                        reason="existing_queue_job_reused",
                    )
                    self.after(
                        int(config.get("kit_insertion_page", "logout_delay_ms", default=3000)),
                        self.logout_user,
                    )
                    return

            self._busy = True
            self.insert_btn.configure(state="disabled")
            self._set_camera_badge("Queueing", "warning")

            self._set_status(
                confirm_text="Checking transaction details...",
                result_text="Please wait while we queue your kit.",
                confirm_color=INFO,
                result_color=INFO,
            )

            user_id = str(self._get_user_id()).strip()
            username = str(self.user_data.get("username") or self.user_data.get("name") or "user").strip()
            product_id = str(self._get_product_id()).strip()
            product_name = str(self._get_product_name()).strip()
            transaction_id = str(self._get_transaction_id()).strip()

            missing = []
            if not user_id:
                missing.append("user_id")
            if not product_id:
                missing.append("product_id")
            if not transaction_id:
                missing.append("transaction_id")

            if missing:
                raise RuntimeError(
                    "Missing required queue data: "
                    + ", ".join(missing)
                    + f". user_id={user_id}, product_id={product_id}, transaction_id={transaction_id}"
                )

            session_dir = get_or_create_capture_session(user_id)

            job = enqueue_kit_job(
                user_id=user_id,
                username=username,
                product_id=product_id,
                product_name=product_name,
                transaction_id=transaction_id,
                session_dir=str(session_dir),
            )

            self._queued_job = job
            self._queue_retry_count = 0
            self._cancel_queue_retry()

            delay_text = self._queue_delay_text()

            self._set_camera_badge("Queued", "success")
            self._set_status(
                confirm_text="Kit accepted. You may now leave the booth.",
                result_text=f"Your result will appear on the website after about {delay_text}.",
                confirm_color=SUCCESS,
                result_color=SUCCESS,
            )

            print(f"[KIT] Queued kit job: {job}", flush=True)

            self._finish_paid_booth_flow_after_queue(
                job,
                reason="new_queue_job_created",
            )

            if self.payment_session_id:
                try:
                    mark_payment_completed(self.payment_session_id)
                except Exception as e:
                    print(f"[KIT] Failed to mark recovered payment completed: {e}", flush=True)

            self.after(
                int(config.get("kit_insertion_page", "logout_delay_ms", default=3000)),
                self.logout_user,
            )

        except Exception as e:
            print(f"[KIT] confirm_insertion failed: {e}", flush=True)

            self._busy = False
            self.insert_btn.configure(state="normal")
            self._set_camera_badge("Queue error", "error")

            self._schedule_queue_retry(e)

            try:
                report_warning(
                    "kit_insertion",
                    "Kit Queue Error",
                    "The booth could not queue the inserted kit yet. It will retry automatically.",
                    details={"error": str(e), "transaction_id": self._get_transaction_id()},
                    visible=True,
                )
            except Exception:
                pass

            if hasattr(self.controller, "show_error"):
                self.controller.show_error(
                    f"Failed to queue inserted kit.\n{e}\n\nThe booth will retry automatically. Keep the kit inserted and wait, or tap Retry / Continue after the issue is fixed.",
                    title="Queue Error",
                    action_text="Retry / Continue",
                    on_action=self.recover_from_error,
                    on_close=getattr(self.controller, "_close_error_only", None),
                )

    # Backward compatible name.
    def capture_image(self, run_yolo=True):
        self.confirm_insertion()

    # ------------------------------------------------------------------
    # Camera compatibility methods
    # ------------------------------------------------------------------

    def start_camera(self):
        self._set_camera_badge("Ready", "success")
        self._start_preview_loop()

    def stop_camera(self):
        # Do not stop the real camera here. The queue worker owns it.
        self._set_camera_badge("Stopped", "neutral")

    # ------------------------------------------------------------------
    # Page flow
    # ------------------------------------------------------------------

    def update_data(self, user_data=None, selected_product=None, product=None, transaction_id=None, **kwargs):
        self.user_data = user_data or {}
        self.selected_product = selected_product or product

        self.transaction_id = (
            transaction_id
            or kwargs.get("transaction_id")
            or self.user_data.get("transaction_id")
            or self.user_data.get("transactionID")
            or self.user_data.get("latest_transaction_id")
        )
        self.payment_session_id = kwargs.get("payment_session_id") or self.user_data.get("payment_session_id")

        if self.transaction_id:
            self.user_data["transaction_id"] = self.transaction_id
            self.user_data["latest_transaction_id"] = self.transaction_id

        try:
            self.shell.set_header_right(f"Welcome, {self.user_data.get('username', 'User')}!")
        except Exception:
            pass

        self._busy = False
        self._camera_imgtk = None
        self._queue_retry_count = 0
        self._queued_job = None
        self._cancel_queue_retry()

        self._set_status(
            result_text=config.get(
                "kit_insertion_page",
                "initial_text",
                default="Insert your test kit",
            ),
            confirm_text="",
            result_color=BLACK,
        )

        self._set_camera_badge("Ready", "success")
        self.insert_btn.configure(state="normal")
        self.start_camera()

    def logout_user(self):
        self.stop_camera()
        self._cancel_queue_retry()

        # Fallback clear: this is safe even if confirm_insertion already cleared it.
        if self._queued_job:
            self._finish_paid_booth_flow_after_queue(
                self._queued_job,
                reason="kit_insertion_logout",
            )

        self.user_data = {}
        self.selected_product = None
        self.transaction_id = None
        self._busy = False
        self._camera_imgtk = None
        self._queue_retry_count = 0
        self._queued_job = None
        self._cancel_queue_retry()

        self._set_status(
            result_text=config.get(
                "kit_insertion_page",
                "initial_text",
                default="Insert your test kit",
            ),
            confirm_text="",
            result_color=BLACK,
        )

        self._set_camera_badge("Stopped", "neutral")
        self.insert_btn.configure(state="normal")

        for page_name in ["QRLoginPage", "PurchasePage", "CashPaymentPage", "HowToUsePage"]:
            page = self.controller.frames.get(page_name)
            if page:
                if page_name == "HowToUsePage" and hasattr(page, "reset_video"):
                    page.reset_video()
                if hasattr(page, "reset_fields"):
                    page.reset_fields()

        self.controller.show_loading_then(
            config.get("kit_insertion_page", "logout_loading_text", default="Logging Out..."),
            "WelcomePage",
            delay=1000,
        )

    def destroy(self):
        self._cancel_config_refresh()
        self._cancel_preview_loop()
        self._cancel_queue_retry()
        self.stop_camera()
        super().destroy()