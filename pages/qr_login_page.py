import os
import threading
from pathlib import Path

import requests
from pynput import keyboard

from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, card_body
from backend.util import api_client
from config_manager import config

try:
    import qrcode
    from PIL import Image, ImageTk
except Exception as e:
    qrcode = None
    Image = None
    ImageTk = None
    print(f"[QR LOGIN] QR dependencies unavailable: {e}", flush=True)


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


class QRLoginPage(ctk.CTkFrame):
    REFRESH_MS = 1500

    def __init__(self, master, controller=None):
        super().__init__(master, fg_color=CREAM)

        self.controller = controller

        self.buffer = ""
        self.disabled = False
        self.processing = False
        self.listener = None

        # Tracks Ctrl + Shift + E for emergency QR bypass
        self._pressed_keys = set()

        self.website_url = self._load_website_base_url()
        self.website_qr_photo = None

        self._waiting_base_text = config.get(
            "qr_login_page",
            "waiting_base_text",
            default="Waiting for generated login QR"
        )
        self._waiting_dots = 0
        self._waiting_anim_job = None
        self._waiting_anim_running = False
        self._config_refresh_job = None

        self._build_ui()

        self.start_waiting_animation()
        self.start_key_listener()
        self._start_config_refresh()

    # ------------------------------------------------------------------
    # ENV / WEBSITE QR
    # ------------------------------------------------------------------
    def _load_website_base_url(self):
        env_value = os.environ.get("WEBSITE_BASE_URL", "").strip()
        if env_value:
            return env_value

        possible_paths = []

        try:
            possible_paths.append(Path.cwd() / ".env.local")
        except Exception:
            pass

        try:
            possible_paths.append(Path(__file__).resolve().parents[1] / ".env.local")
        except Exception:
            pass

        try:
            possible_paths.append(Path(__file__).resolve().parent / ".env.local")
        except Exception:
            pass

        for env_path in possible_paths:
            try:
                if not env_path.exists():
                    continue

                with env_path.open("r", encoding="utf-8") as file:
                    for line in file:
                        line = line.strip()

                        if not line or line.startswith("#") or "=" not in line:
                            continue

                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")

                        if key == "WEBSITE_BASE_URL" and value:
                            return value

            except Exception as e:
                print(f"[QR LOGIN] Failed reading {env_path}: {e}", flush=True)

        return config.get(
            "qr_login_page",
            "website_base_url",
            default="https://rodney-unperfidious-laurena.ngrok-free.dev"
        )

    def _render_website_qr(self):
        try:
            if qrcode is None or ImageTk is None:
                self.website_qr_label.configure(
                    image="",
                    text="QR unavailable",
                    font=app_font(16, "bold"),
                    text_color=ERROR
                )
                return

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=10,
                border=2
            )
            qr.add_data(self.website_url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            img = img.resize((250, 250), Image.LANCZOS)

            self.website_qr_photo = ImageTk.PhotoImage(img)
            self.website_qr_label.configure(text="", image=self.website_qr_photo)
            self.website_qr_label.image = self.website_qr_photo

        except Exception as e:
            print(f"[QR LOGIN] Failed to render website QR: {e}", flush=True)
            self.website_qr_label.configure(
                image="",
                text="QR unavailable",
                font=app_font(16, "bold"),
                text_color=ERROR
            )

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        self.shell = AppShell(self)
        self.shell.pack(fill="both", expand=True)

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

    def _build_top_area(self):
        self.top_area = ctk.CTkFrame(self.page, fg_color=CREAM)
        self.top_area.grid(row=0, column=0, sticky="ew", padx=28, pady=(18, 10))
        self.top_area.grid_columnconfigure(0, weight=1)

        self.page_title = ctk.CTkLabel(
            self.top_area,
            text="QR LOGIN",
            font=app_heavy(34),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=980
        )
        self.page_title.grid(row=0, column=0, sticky="ew")

    def _build_main_card(self):
        self.main_wrap = ctk.CTkFrame(self.page, fg_color=CREAM)
        self.main_wrap.grid(row=1, column=0, sticky="nsew", padx=28, pady=(0, 20))
        self.main_wrap.grid_columnconfigure(0, weight=1)
        self.main_wrap.grid_rowconfigure(0, weight=1)

        self.main_card = RoundedCard(
            self.main_wrap,
            fg_color=WHITE,
            radius=36,
            pad=0,
            auto_size=False,
            width=1120,
            height=620
        )
        self.main_card.grid(row=0, column=0, sticky="nsew")

        body = card_body(self.main_card)
        safe_configure(body, fg_color=WHITE)

        body.grid_columnconfigure(0, weight=1, uniform="qr_login_cols")
        body.grid_columnconfigure(1, weight=1, uniform="qr_login_cols")
        body.grid_rowconfigure(0, weight=1)

        self.website_panel = ctk.CTkFrame(body, fg_color=WHITE)
        self.website_panel.grid(row=0, column=0, sticky="nsew", padx=(30, 16), pady=28)

        self.scanner_panel = ctk.CTkFrame(body, fg_color=WHITE)
        self.scanner_panel.grid(row=0, column=1, sticky="nsew", padx=(16, 30), pady=28)

        self._build_website_panel()
        self._build_scanner_panel()

    def _build_website_panel(self):
        self.website_panel.grid_columnconfigure(0, weight=1)
        self.website_panel.grid_rowconfigure(0, weight=0)
        self.website_panel.grid_rowconfigure(1, weight=1)

        self.website_header = ctk.CTkFrame(self.website_panel, fg_color=WHITE, height=1)
        self.website_header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.website_header.grid_columnconfigure(0, weight=1)
        self.website_header.grid_propagate(False)

        self.website_header_inner = ctk.CTkFrame(self.website_header, fg_color=WHITE)
        self.website_header_inner.grid(row=0, column=0, sticky="nsew")
        self.website_header_inner.grid_columnconfigure(0, weight=1)

        self.website_badge = ctk.CTkFrame(
            self.website_header_inner,
            fg_color="#FFF2E8",
            corner_radius=999
        )
        self.website_badge.grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.website_badge_label = ctk.CTkLabel(
            self.website_badge,
            text="PHONE",
            font=app_font(12, "bold"),
            text_color=ORANGE,
            fg_color="transparent"
        )
        self.website_badge_label.pack(padx=16, pady=6)

        self.website_title = ctk.CTkLabel(
            self.website_header_inner,
            text="Need a login QR?",
            font=app_heavy(30),
            text_color=BLACK,
            fg_color=WHITE,
            justify="left",
            anchor="w",
            wraplength=500
        )
        self.website_title.grid(row=1, column=0, sticky="ew")

        self.website_subtitle = ctk.CTkLabel(
            self.website_header_inner,
            text="Scan this website QR with your phone first.",
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            justify="left",
            anchor="w",
            wraplength=500
        )
        self.website_subtitle.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        self.website_card = ctk.CTkFrame(
            self.website_panel,
            fg_color=CREAM,
            corner_radius=32
        )
        self.website_card.grid(row=1, column=0, sticky="nsew")
        self.website_card.grid_columnconfigure(0, weight=1)
        self.website_card.grid_rowconfigure(0, weight=1)
        self.website_card.grid_rowconfigure(1, weight=0)
        self.website_card.grid_rowconfigure(2, weight=0)
        self.website_card.grid_rowconfigure(3, weight=0)
        self.website_card.grid_rowconfigure(4, weight=1)

        self.website_qr_holder = ctk.CTkFrame(
            self.website_card,
            fg_color=WHITE,
            corner_radius=24,
            border_width=2,
            border_color="#EBD8C6"
        )
        self.website_qr_holder.grid(row=1, column=0, pady=(0, 16))
        self.website_qr_holder.grid_columnconfigure(0, weight=1)

        self.website_qr_label = ctk.CTkLabel(
            self.website_qr_holder,
            text="Generating website QR...",
            font=app_font(15, "bold"),
            text_color=MUTED,
            fg_color=WHITE,
            justify="center",
            anchor="center"
        )
        self.website_qr_label.grid(row=0, column=0, padx=18, pady=18)

        self.website_caption = ctk.CTkLabel(
            self.website_card,
            text="Website QR",
            font=app_heavy(20),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=420
        )
        self.website_caption.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 4))

        self.website_url_label = ctk.CTkLabel(
            self.website_card,
            text=self.website_url,
            font=app_font(11, "bold"),
            text_color=MUTED,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=420
        )
        self.website_url_label.grid(row=3, column=0, sticky="ew", padx=24)

        self._render_website_qr()

    def _build_scanner_panel(self):
        self.scanner_panel.grid_columnconfigure(0, weight=1)
        self.scanner_panel.grid_rowconfigure(0, weight=0)
        self.scanner_panel.grid_rowconfigure(1, weight=1)

        self.scanner_header = ctk.CTkFrame(self.scanner_panel, fg_color=WHITE, height=1)
        self.scanner_header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.scanner_header.grid_columnconfigure(0, weight=1)
        self.scanner_header.grid_propagate(False)

        self.scanner_header_inner = ctk.CTkFrame(self.scanner_header, fg_color=WHITE)
        self.scanner_header_inner.grid(row=0, column=0, sticky="nsew")
        self.scanner_header_inner.grid_columnconfigure(0, weight=1)

        self.scanner_badge = ctk.CTkFrame(
            self.scanner_header_inner,
            fg_color="#EEF4FF",
            corner_radius=999
        )
        self.scanner_badge.grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.scanner_badge_label = ctk.CTkLabel(
            self.scanner_badge,
            text="KIOSK SCANNER",
            font=app_font(12, "bold"),
            text_color=INFO,
            fg_color="transparent"
        )
        self.scanner_badge_label.pack(padx=16, pady=6)

        self.scanner_title = ctk.CTkLabel(
            self.scanner_header_inner,
            text="Show your generated login QR",
            font=app_heavy(30),
            text_color=BLACK,
            fg_color=WHITE,
            justify="left",
            anchor="w",
            wraplength=500
        )
        self.scanner_title.grid(row=1, column=0, sticky="ew")

        self.scanner_subtitle = ctk.CTkLabel(
            self.scanner_header_inner,
            text="After signing in on your phone, show the generated login QR to the kiosk scanner.",
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            justify="left",
            anchor="w",
            wraplength=500
        )
        self.scanner_subtitle.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        self.scanner_status_card = ctk.CTkFrame(
            self.scanner_panel,
            fg_color=CREAM,
            corner_radius=32
        )
        self.scanner_status_card.grid(row=1, column=0, sticky="nsew")
        self.scanner_status_card.grid_columnconfigure(0, weight=1)
        self.scanner_status_card.grid_rowconfigure(0, weight=1)
        self.scanner_status_card.grid_rowconfigure(1, weight=0)
        self.scanner_status_card.grid_rowconfigure(2, weight=0)
        self.scanner_status_card.grid_rowconfigure(3, weight=0)
        self.scanner_status_card.grid_rowconfigure(4, weight=1)

        self.state_pill = ctk.CTkFrame(
            self.scanner_status_card,
            fg_color="#EEF4FF",
            corner_radius=999
        )
        self.state_pill.grid(row=1, column=0, pady=(0, 18))

        self.state_label = ctk.CTkLabel(
            self.state_pill,
            text="Scanner ready",
            font=app_font(13, "bold"),
            text_color=INFO,
            fg_color="transparent"
        )
        self.state_label.pack(padx=18, pady=8)

        self.result_label = ctk.CTkLabel(
            self.scanner_status_card,
            text="Waiting for generated login QR...",
            font=app_heavy(32),
            text_color=BLACK,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=500
        )
        self.result_label.grid(row=2, column=0, sticky="ew", padx=30, pady=(0, 10))

        self.status_label = ctk.CTkLabel(
            self.scanner_status_card,
            text="The kiosk scanner is active.",
            font=app_font(16, "bold"),
            text_color=MUTED,
            fg_color=CREAM,
            justify="center",
            anchor="center",
            wraplength=500
        )
        self.status_label.grid(row=3, column=0, sticky="ew", padx=30)

    # ------------------------------------------------------------------
    # Scanner state UI
    # ------------------------------------------------------------------
    def _set_scanner_state(self, text, color):
        try:
            self.state_label.configure(text=text, text_color=color)

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

    # ------------------------------------------------------------------
    # Layout / config
    # ------------------------------------------------------------------
    def _sync_layout(self, event=None):
        try:
            self.update_idletasks()

            available_w = max(920, self.main_wrap.winfo_width())
            available_h = max(560, self.main_wrap.winfo_height())

            card_w = min(1180, max(940, int(available_w * 0.94)))
            card_h = min(680, max(560, int(available_h * 0.96)))

            self.main_card.configure(width=card_w, height=card_h)

            shared_wrap = max(
                420,
                min(self.website_panel.winfo_width(), self.scanner_panel.winfo_width()) - 60
            )

            self.website_title.configure(wraplength=shared_wrap)
            self.website_subtitle.configure(wraplength=shared_wrap)
            self.website_caption.configure(wraplength=shared_wrap)
            self.website_url_label.configure(wraplength=shared_wrap)

            self.scanner_title.configure(wraplength=shared_wrap)
            self.scanner_subtitle.configure(wraplength=shared_wrap)
            self.result_label.configure(wraplength=shared_wrap)
            self.status_label.configure(wraplength=shared_wrap)

            self.update_idletasks()

            website_header_req = self.website_header_inner.winfo_reqheight()
            scanner_header_req = self.scanner_header_inner.winfo_reqheight()
            shared_header_h = max(website_header_req, scanner_header_req) + 2

            self.website_header.configure(height=shared_header_h)
            self.scanner_header.configure(height=shared_header_h)

        except Exception:
            pass

    def _refresh_from_config(self):
        try:
            self._waiting_base_text = config.get(
                "qr_login_page",
                "waiting_base_text",
                default="Waiting for generated login QR"
            )

            if not self.processing and not self.disabled:
                self.result_label.configure(
                    text=config.get(
                        "qr_login_page",
                        "waiting_text",
                        default="Waiting for generated login QR..."
                    ),
                    text_color=BLACK
                )
                self.status_label.configure(
                    text="The kiosk scanner is active.",
                    text_color=MUTED
                )
                self._set_scanner_state("Scanner ready", INFO)

        except Exception as e:
            print(f"[QR LOGIN] Config refresh failed: {e}", flush=True)

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

    # ------------------------------------------------------------------
    # Waiting animation
    # ------------------------------------------------------------------
    def start_waiting_animation(self):
        self.stop_waiting_animation()
        self._waiting_anim_running = True
        self._waiting_dots = 0
        self._animate_waiting_text()

    def stop_waiting_animation(self):
        self._waiting_anim_running = False
        if self._waiting_anim_job is not None:
            try:
                self.after_cancel(self._waiting_anim_job)
            except Exception:
                pass
            self._waiting_anim_job = None

    def _animate_waiting_text(self):
        if not self._waiting_anim_running:
            return

        self._waiting_dots = (self._waiting_dots + 1) % 4
        dots = "." * self._waiting_dots

        self.result_label.configure(
            text=f"{self._waiting_base_text}{dots}",
            text_color=BLACK
        )

        self._waiting_anim_job = self.after(450, self._animate_waiting_text)

    # ------------------------------------------------------------------
    # Emergency bypass shortcut
    # ------------------------------------------------------------------
    def _normalize_key(self, key):
        """
        Normalizes pynput key events so Ctrl + Shift + E can be detected reliably.

        Note:
        Ctrl + E can sometimes appear as '\x05', so that is also treated as E.
        """
        try:
            char = key.char
            if char:
                if char.lower() == "e" or char == "\x05":
                    return "e"
                return f"char:{char}"
        except AttributeError:
            pass

        if key in (
            keyboard.Key.ctrl,
            keyboard.Key.ctrl_l,
            keyboard.Key.ctrl_r,
        ):
            return "ctrl"

        if key in (
            keyboard.Key.shift,
            keyboard.Key.shift_l,
            keyboard.Key.shift_r,
        ):
            return "shift"

        if key == keyboard.Key.enter:
            return "enter"

        return str(key)

    def _is_bypass_combo_active(self):
        return (
            "ctrl" in self._pressed_keys
            and "shift" in self._pressed_keys
            and "e" in self._pressed_keys
        )

    def trigger_emergency_bypass(self):
        """
        Staff-only QR bypass.

        This skips:
        - website login QR generation
        - scanner QR verification
        - SMS gateway dependency

        It moves directly to PurchasePage with a local emergency/walk-in user.
        """
        if self.disabled or self.processing:
            return

        self.processing = True
        self.buffer = ""

        self.stop_waiting_animation()
        self._set_scanner_state("Bypass active", ORANGE)

        self.status_label.configure(
            text="QR login was bypassed using the staff shortcut.",
            text_color=ORANGE
        )

        self.result_label.configure(
            text="Emergency bypass",
            text_color=ORANGE
        )

        print("[QR LOGIN] Emergency bypass triggered using Ctrl + Shift + E", flush=True)

        self.after(650, self._complete_emergency_bypass)

    def _complete_emergency_bypass(self):
        if self.disabled:
            return

        user_data = {
            "username": config.get(
                "qr_login_page",
                "bypass_username",
                default="Emergency Bypass"
            ),
            "userID": config.get(
                "qr_login_page",
                "bypass_user_id",
                default="qr-bypass-user"
            ),
            "loginMode": "qr_bypass"
        }

        self._set_scanner_state("Bypass verified", SUCCESS)

        self.status_label.configure(
            text="Proceeding without QR login.",
            text_color=SUCCESS
        )

        self.result_label.configure(
            text=f"Logged in as: {user_data['username']}",
            text_color=SUCCESS
        )

        self.disable_page()
        self.redirect_to_purchase(user_data)

    # ------------------------------------------------------------------
    # Scanner listener
    # ------------------------------------------------------------------
    def start_key_listener(self):
        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass

        self._pressed_keys = set()

        def on_press(key):
            if self.disabled or self.processing:
                return

            normalized = self._normalize_key(key)

            if normalized in ("ctrl", "shift", "e"):
                self._pressed_keys.add(normalized)

            if self._is_bypass_combo_active():
                self.after(0, self.trigger_emergency_bypass)
                return

            try:
                if key.char:
                    # Do not inject Ctrl-generated control characters into the scanner buffer.
                    if "ctrl" in self._pressed_keys:
                        return

                    self.buffer += key.char

            except AttributeError:
                if key == keyboard.Key.enter:
                    scanned = self.buffer.strip()
                    self.buffer = ""

                    if not scanned or self.processing:
                        return

                    self.processing = True
                    self.after(0, self.show_loading)

                    threading.Thread(
                        target=self.process_scan,
                        args=(scanned,),
                        daemon=True
                    ).start()

        def on_release(key):
            normalized = self._normalize_key(key)

            if normalized in self._pressed_keys:
                self._pressed_keys.discard(normalized)

            # Some systems report Ctrl+E as '\x05', but release may not match cleanly.
            # This prevents E from getting stuck in the pressed set.
            try:
                if key.char and (key.char.lower() == "e" or key.char == "\x05"):
                    self._pressed_keys.discard("e")
            except AttributeError:
                pass

        self.listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.listener.start()

    # ------------------------------------------------------------------
    # Login flow
    # ------------------------------------------------------------------
    def show_loading(self):
        self.stop_waiting_animation()

        self._set_scanner_state("Checking QR", ORANGE)

        self.status_label.configure(
            text=config.get(
                "qr_login_page",
                "validating_text",
                default="Validating QR... Please wait"
            ),
            text_color=ORANGE
        )

        self.result_label.configure(
            text="Checking login QR",
            text_color=ORANGE
        )

    def process_scan(self, scanned_code):
        if self.disabled:
            return

        if not scanned_code.startswith("LOGIN-"):
            def invalid_qr():
                self._set_scanner_state("Invalid QR", ERROR)

                self.status_label.configure(
                    text="This QR is not a login QR.",
                    text_color=ERROR
                )

                self.result_label.configure(
                    text="Scan the login QR generated on your phone.",
                    text_color=ERROR
                )

                self.processing = False
                self.after(1600, self.start_waiting_animation)

            self.after(0, invalid_qr)
            return

        try:
            response = api_client.verify_login_qr(scanned_code)

            try:
                data = response.json()
            except ValueError:
                data = {"error": f"Non-JSON response ({response.status_code})"}

            if response.ok and data.get("user"):
                user = data["user"]
                user_data = {
                    "username": user.get("username", "User"),
                    "userID": user.get("_id"),
                    "loginMode": "qr"
                }

                def success():
                    self._set_scanner_state("Login verified", SUCCESS)

                    self.status_label.configure(
                        text=config.get(
                            "qr_login_page",
                            "login_success_status",
                            default="Login successful"
                        ),
                        text_color=SUCCESS
                    )

                    self.result_label.configure(
                        text=f"{config.get('qr_login_page', 'login_success_prefix', default='Logged in as:')} {user_data['username']}",
                        text_color=SUCCESS
                    )

                    self.disable_page()
                    self.redirect_to_purchase(user_data)

                self.after(0, success)

            else:
                err = data.get("error") or config.get(
                    "qr_login_page",
                    "login_failed_result",
                    default="Login failed"
                )

                def fail():
                    if self.disabled:
                        return

                    self._set_scanner_state("Login failed", ERROR)

                    self.status_label.configure(
                        text=config.get(
                            "qr_login_page",
                            "login_failed_status",
                            default="Login failed"
                        ),
                        text_color=ERROR
                    )

                    self.result_label.configure(
                        text=err,
                        text_color=ERROR
                    )

                    self.processing = False
                    self.after(1600, self.start_waiting_animation)

                self.after(0, fail)

        except requests.RequestException as e:
            error_message = f"{config.get('qr_login_page', 'network_error_prefix', default='Request failed:')} {e}"

            def network_fail():
                if self.disabled:
                    return

                self._set_scanner_state("Network error", ERROR)

                self.status_label.configure(
                    text=config.get(
                        "qr_login_page",
                        "network_error_status",
                        default="Network error"
                    ),
                    text_color=ERROR
                )

                self.result_label.configure(
                    text=error_message,
                    text_color=ERROR
                )

                self.processing = False
                self.after(1800, self.start_waiting_animation)

            self.after(0, network_fail)

    def disable_page(self):
        self.disabled = True
        self.processing = False
        self.stop_waiting_animation()

        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None

    def redirect_to_purchase(self, user_data):
        if self.controller:
            self.controller.current_user = user_data
            self.controller.show_frame("PurchasePage", user_data=user_data)

    def reset_fields(self, **kwargs):
        self.buffer = ""
        self.disabled = False
        self.processing = False
        self._pressed_keys = set()

        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None

        self.website_url = self._load_website_base_url()
        self.website_url_label.configure(text=self.website_url)
        self._render_website_qr()

        self.status_label.configure(
            text="The kiosk scanner is active.",
            text_color=MUTED
        )

        self.result_label.configure(
            text=config.get(
                "qr_login_page",
                "waiting_text",
                default="Waiting for generated login QR..."
            ),
            text_color=BLACK
        )

        self._set_scanner_state("Scanner ready", INFO)

        self.start_waiting_animation()
        self.start_key_listener()

    def destroy(self):
        self.stop_waiting_animation()
        self._cancel_config_refresh()

        if self.listener:
            try:
                self.listener.stop()
            except Exception:
                pass
            self.listener = None

        super().destroy()