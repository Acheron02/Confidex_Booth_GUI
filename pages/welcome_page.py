import os
import tkinter as tk
from PIL import Image, ImageTk, ImageDraw, ImageFilter, ImageOps

from frontend import tk_compat as ctk
from frontend import theme
from config_manager import config


class WelcomePage(ctk.CTkFrame):
    def __init__(self, master, controller):
        super().__init__(master, fg_color=getattr(theme, "CREAM", "#F5F2DE"))
        self.controller = controller

        self.CREAM = getattr(theme, "CREAM", "#F5F2DE")
        self.WHITE = getattr(theme, "WHITE", "#FFFFFF")
        self.ORANGE = getattr(theme, "ORANGE", "#F97316")
        self.TEXT = getattr(theme, "TEXT", "#171717")
        self.MUTED = getattr(theme, "MUTED", "#6B625A")

        self.canvas = tk.Canvas(
            self,
            bg=self.CREAM,
            bd=0,
            highlightthickness=0,
            relief="flat",
            cursor="hand2",
        )
        self.canvas.pack(fill="both", expand=True)

        self._bg_ref = None
        self._hero_ref = None
        self._logo_ref = None
        self._cta_ref = None
        self._image_refs = []

        self._redraw_after_id = None
        self._config_snapshot = {}
        self._system_notice = None

        self.canvas.bind("<Button-1>", self.go_to_login)
        self.canvas.bind("<Configure>", self._schedule_redraw)
        self.bind("<Button-1>", self.go_to_login)

        self._start_config_refresh()

    # ---------------------------------------------------------------------
    # Main drawing
    # ---------------------------------------------------------------------

    def _schedule_redraw(self, event=None):
        if self._redraw_after_id is not None:
            try:
                self.after_cancel(self._redraw_after_id)
            except Exception:
                pass

        self._redraw_after_id = self.after(60, self._draw)

    def _draw(self):
        self._redraw_after_id = None

        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())

        if w < 300 or h < 300:
            return

        self.canvas.delete("all")
        self._image_refs.clear()

        bg = self._make_background(w, h)
        self._bg_ref = ImageTk.PhotoImage(bg)
        self._image_refs.append(self._bg_ref)
        self.canvas.create_image(0, 0, image=self._bg_ref, anchor="nw")

        pad = max(18, int(min(w, h) * 0.022))
        content_pad = max(38, int(w * 0.028))
        gap = max(34, int(w * 0.032))

        left_x = pad + content_pad
        top_y = pad + content_pad
        bottom_y = h - pad - content_pad

        available_w = w - (pad * 2) - (content_pad * 2)
        right_w = max(430, int(available_w * 0.43))
        right_x = w - pad - content_pad - right_w

        left_w = right_x - gap - left_x

        if left_w < 500:
            right_w = max(360, int(available_w * 0.36))
            right_x = w - pad - content_pad - right_w
            left_w = right_x - gap - left_x

        hero_y = top_y
        hero_h = bottom_y - top_y

        # Soft main surface
        self._rounded_rect(
            self.canvas,
            pad,
            pad,
            w - pad,
            h - pad,
            42,
            fill="#FFFCF5",
            outline="#EFE4D6",
            width=1,
        )

        # Connecting warm glow behind the right visual panel
        self._rounded_rect(
            self.canvas,
            max(left_x + int(left_w * 0.62), 0),
            top_y + 24,
            w - pad - 8,
            bottom_y - 24,
            44,
            fill="#FFF0DF",
            outline="#FFF0DF",
            width=1,
        )

        self._draw_hero_panel(right_x, hero_y, right_w, hero_h)
        self._draw_left_content(left_x, top_y, left_w, bottom_y)

    def _draw_left_content(self, x, y, width, bottom_y):
        c = self.canvas
        data = self._config_snapshot or self._read_config()

        logo_h = self._draw_logo(x, y)

        eyebrow_y = y + max(108, logo_h + 36)
        self._draw_pill(
            x,
            eyebrow_y,
            292,
            42,
            text="PRIVATE HEALTH SCREENING",
            fill="#FFF2E8",
            outline="#F5D9C6",
            text_fill=self.ORANGE,
            font=("Helvetica", 13, "bold"),
        )

        screen_h = max(720, self.winfo_screenheight())
        title_size = max(54, min(86, int(screen_h * 0.075)))
        subtitle_size = max(22, min(31, int(screen_h * 0.027)))

        title_y = eyebrow_y + 70
        title_text = data.get("title", "WELCOME")

        title_id = c.create_text(
            x,
            title_y,
            text=title_text,
            fill=self.TEXT,
            font=("Helvetica", title_size, "bold"),
            anchor="nw",
            width=max(420, width - 20),
        )

        title_bbox = c.bbox(title_id)
        title_bottom = title_bbox[3] if title_bbox else title_y + 90

        subtitle_text = data.get("subtitle", "Anonymous Health Screening")
        subtitle_id = c.create_text(
            x,
            title_bottom + 16,
            text=subtitle_text,
            fill=self.MUTED,
            font=("Helvetica", subtitle_size, "normal"),
            anchor="nw",
            width=max(390, width - 80),
        )

        subtitle_bbox = c.bbox(subtitle_id)
        subtitle_bottom = subtitle_bbox[3] if subtitle_bbox else title_bottom + 60

        cta_w = min(max(500, int(width * 0.86)), width)
        cta_h = 114

        preferred_cta_y = max(
            subtitle_bottom + 180,
            int(self.canvas.winfo_height() * 0.66),
        )
        max_cta_y = bottom_y - cta_h - 76
        cta_y = min(preferred_cta_y, max_cta_y)

        self._draw_cta(x, cta_y, cta_w, cta_h, data)
        self._draw_system_notice(x, cta_y + cta_h + 18, cta_w)

        c.create_text(
            x,
            bottom_y - 24,
            text="Anonymous • Secure • Self-service",
            fill="#9A8F84",
            font=("Helvetica", 14, "normal"),
            anchor="sw",
        )

    def _draw_logo(self, x, y):
        logo_path = self._asset_path("logo2.png")

        if os.path.exists(logo_path):
            try:
                img = Image.open(logo_path).convert("RGBA")
                img.thumbnail((330, 96), Image.LANCZOS)

                self._logo_ref = ImageTk.PhotoImage(img)
                self._image_refs.append(self._logo_ref)

                self.canvas.create_image(
                    x,
                    y,
                    image=self._logo_ref,
                    anchor="nw",
                )

                return img.height

            except Exception as e:
                print("Logo load failed:", e, flush=True)

        app_name = config.get("branding", "app_name", default="CONFIDEX")
        self.canvas.create_text(
            x,
            y,
            text=app_name,
            fill=self.TEXT,
            font=("Helvetica", 36, "bold"),
            anchor="nw",
        )
        return 48

    def _draw_cta(self, x, y, w, h, data):
        c = self.canvas

        # Shadow
        self._rounded_rect(
            c,
            x + 8,
            y + 10,
            x + w + 8,
            y + h + 10,
            30,
            fill="#E3B58F",
            outline="#E3B58F",
            width=1,
        )

        cta_img = self._make_rounded_gradient(
            w,
            h,
            self.ORANGE,
            "#FF9A3D",
            radius=30,
        )
        self._cta_ref = ImageTk.PhotoImage(cta_img)
        self._image_refs.append(self._cta_ref)

        c.create_image(x, y, image=self._cta_ref, anchor="nw")

        # No left icon anymore.
        text_x = x + 72
        text_y = y + 26

        c.create_text(
            text_x,
            text_y,
            text=data.get("tap_text", "TAP TO PROCEED"),
            fill="#FFFFFF",
            font=("Helvetica", 30, "bold"),
            anchor="nw",
            width=max(250, w - 160),
        )

        c.create_text(
            text_x,
            text_y + 45,
            text="Continue with your secure QR login",
            fill="#FFF5ED",
            font=("Helvetica", 15, "normal"),
            anchor="nw",
            width=max(250, w - 160),
        )

        circle_r = 24
        circle_cx = x + w - 48
        circle_cy = y + h / 2

        c.create_oval(
            circle_cx - circle_r,
            circle_cy - circle_r,
            circle_cx + circle_r,
            circle_cy + circle_r,
            fill="#FFFFFF",
            outline="#FFFFFF",
        )

        c.create_text(
            circle_cx,
            circle_cy - 2,
            text="›",
            fill=self.ORANGE,
            font=("Helvetica", 34, "bold"),
            anchor="center",
        )


    def set_system_notice(self, title="", message="", severity="warning"):
        title = str(title or "").strip()
        message = str(message or "").strip()
        severity = str(severity or "warning").strip().lower()

        if not title and not message:
            self._system_notice = None
        else:
            self._system_notice = {
                "title": title or "Booth Notice",
                "message": message,
                "severity": severity,
            }

        self._schedule_redraw()

    def clear_system_notice(self):
        self._system_notice = None
        self._schedule_redraw()

    def _draw_system_notice(self, x, y, w):
        notice = self._system_notice
        if not notice:
            return

        c = self.canvas
        severity = str(notice.get("severity") or "warning").lower()

        if severity in {"error", "critical"}:
            fill = "#FFF1F1"
            outline = "#F7B4B4"
            title_fill = "#B42318"
            icon = "!"
        else:
            fill = "#FFF8E6"
            outline = "#F5D38C"
            title_fill = "#9A6700"
            icon = "i"

        title = str(notice.get("title") or "Booth Notice")
        message = str(notice.get("message") or "")
        box_h = 92 if message else 68

        self._rounded_rect(
            c,
            x,
            y,
            x + w,
            y + box_h,
            22,
            fill=fill,
            outline=outline,
            width=1,
        )

        c.create_oval(
            x + 24,
            y + 22,
            x + 54,
            y + 52,
            fill="#FFFFFF",
            outline=outline,
            width=1,
        )
        c.create_text(
            x + 39,
            y + 37,
            text=icon,
            fill=title_fill,
            font=("Helvetica", 16, "bold"),
            anchor="center",
        )

        c.create_text(
            x + 70,
            y + 18,
            text=title,
            fill=title_fill,
            font=("Helvetica", 15, "bold"),
            anchor="nw",
            width=max(260, w - 96),
        )

        if message:
            c.create_text(
                x + 70,
                y + 43,
                text=message,
                fill="#6B625A",
                font=("Helvetica", 12, "normal"),
                anchor="nw",
                width=max(260, w - 96),
            )

    def _draw_hero_panel(self, x, y, w, h):
        c = self.canvas

        # Shadow
        self._rounded_rect(
            c,
            x + 14,
            y + 16,
            x + w + 14,
            y + h + 16,
            42,
            fill="#DCC8B5",
            outline="#DCC8B5",
            width=1,
        )

        hero_img = self._make_hero_panel_image(w, h)
        self._hero_ref = ImageTk.PhotoImage(hero_img)
        self._image_refs.append(self._hero_ref)

        c.create_image(x, y, image=self._hero_ref, anchor="nw")

        # Removed:
        # - top-left "CONFIDEX BOOTH" badge
        # - bottom "Private by design" card

    # ---------------------------------------------------------------------
    # Image generation helpers
    # ---------------------------------------------------------------------

    def _make_background(self, w, h):
        cream = self._hex_to_rgb(self.CREAM)
        warm = self._hex_to_rgb("#FFF9F2")

        line = Image.new("RGB", (w, 1))
        pixels = []

        for x in range(w):
            t = x / max(1, w - 1)
            pixels.append(self._mix_rgb(cream, warm, t))

        line.putdata(pixels)
        bg = line.resize((w, h))

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        draw.ellipse(
            (int(w * 0.50), int(h * -0.12), int(w * 1.15), int(h * 0.72)),
            fill=(255, 151, 67, 48),
        )
        draw.ellipse(
            (int(w * 0.68), int(h * 0.38), int(w * 1.18), int(h * 1.15)),
            fill=(255, 120, 36, 50),
        )
        draw.ellipse(
            (int(w * -0.16), int(h * 0.58), int(w * 0.25), int(h * 1.08)),
            fill=(255, 255, 255, 130),
        )

        overlay = overlay.filter(ImageFilter.GaussianBlur(radius=52))
        bg = Image.alpha_composite(bg.convert("RGBA"), overlay)

        return bg.convert("RGB")

    def _make_hero_panel_image(self, w, h):
        image_path = self._asset_path("Poster.png")

        if os.path.exists(image_path):
            try:
                img = Image.open(image_path).convert("RGB")
                img = ImageOps.fit(
                    img,
                    (w, h),
                    method=Image.LANCZOS,
                    centering=(0.55, 0.50),
                )
            except Exception as e:
                print("Welcome visual load failed:", e, flush=True)
                img = self._make_placeholder_visual(w, h)
        else:
            img = self._make_placeholder_visual(w, h)

        panel = img.convert("RGBA")

        # Left-side fade: makes the background visually blend into the photo.
        fade = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        fade_draw = ImageDraw.Draw(fade)

        fade_width = int(w * 0.46)
        cream_rgb = self._hex_to_rgb(self.CREAM)

        for px in range(fade_width):
            t = px / max(1, fade_width - 1)
            alpha = int(190 * (1 - t))
            fade_draw.line(
                (px, 0, px, h),
                fill=(cream_rgb[0], cream_rgb[1], cream_rgb[2], alpha),
            )

        # Bottom warm depth.
        for py in range(h):
            t = py / max(1, h - 1)
            if t > 0.62:
                alpha = int((t - 0.62) / 0.38 * 82)
                fade_draw.line(
                    (0, py, w, py),
                    fill=(78, 45, 24, alpha),
                )

        panel = Image.alpha_composite(panel, fade)

        # Slight warm polish overlay.
        polish = Image.new("RGBA", (w, h), (255, 134, 45, 24))
        panel = Image.alpha_composite(panel, polish)

        mask = Image.new("L", (w, h), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle(
            (0, 0, w - 1, h - 1),
            radius=42,
            fill=255,
        )

        rounded = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        rounded.paste(panel, (0, 0), mask)

        return rounded

    def _make_placeholder_visual(self, w, h):
        bg1 = self._hex_to_rgb("#FFF3E6")
        bg2 = self._hex_to_rgb("#FFFFFF")

        line = Image.new("RGB", (w, 1))
        pixels = []

        for x in range(w):
            t = x / max(1, w - 1)
            pixels.append(self._mix_rgb(bg1, bg2, t))

        img = line.resize((w, h)).convert("RGBA")

        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        draw.ellipse(
            (int(w * 0.45), int(h * 0.12), int(w * 1.18), int(h * 0.92)),
            fill=(255, 143, 54, 42),
        )
        draw.ellipse(
            (int(w * -0.12), int(h * 0.55), int(w * 0.36), int(h * 1.08)),
            fill=(255, 255, 255, 130),
        )

        overlay = overlay.filter(ImageFilter.GaussianBlur(radius=34))
        img = Image.alpha_composite(img, overlay)

        draw = ImageDraw.Draw(img)

        # Floor shadow
        draw.ellipse(
            (int(w * 0.20), int(h * 0.79), int(w * 0.87), int(h * 0.93)),
            fill=(123, 82, 48, 42),
        )

        # Booth body
        bx1 = int(w * 0.31)
        bx2 = int(w * 0.77)
        by1 = int(h * 0.16)
        by2 = int(h * 0.84)

        draw.rounded_rectangle(
            (bx1, by1, bx2, by2),
            radius=38,
            fill=(255, 255, 255, 255),
            outline=(232, 214, 198, 255),
            width=3,
        )

        # Screen
        sx1 = bx1 + int(w * 0.055)
        sx2 = bx2 - int(w * 0.055)
        sy1 = by1 + int(h * 0.08)
        sy2 = sy1 + int(h * 0.26)

        draw.rounded_rectangle(
            (sx1, sy1, sx2, sy2),
            radius=24,
            fill=(18, 18, 18, 255),
        )

        inner = 13
        draw.rounded_rectangle(
            (sx1 + inner, sy1 + inner, sx2 - inner, sy2 - inner),
            radius=18,
            fill=(255, 250, 246, 255),
        )

        cx = int((sx1 + sx2) / 2)
        cy = int((sy1 + sy2) / 2) - 10

        orange = self._hex_to_rgb(self.ORANGE)

        draw.ellipse(
            (cx - 26, cy - 46, cx + 26, cy + 6),
            outline=orange + (255,),
            width=4,
        )

        draw.arc(
            (cx - 52, cy - 6, cx + 52, cy + 76),
            start=0,
            end=180,
            fill=orange + (255,),
            width=4,
        )

        draw.text(
            (cx, sy2 - 48),
            "Private screening",
            fill=(40, 36, 32, 255),
            anchor="mm",
        )

        # Orange slot
        slot_x1 = bx1 + int(w * 0.07)
        slot_y1 = by1 + int(h * 0.43)
        slot_x2 = slot_x1 + int(w * 0.22)
        slot_y2 = slot_y1 + 16

        draw.rounded_rectangle(
            (slot_x1, slot_y1, slot_x2, slot_y2),
            radius=8,
            fill=orange + (255,),
        )

        # Reader module
        rx1 = bx2 - int(w * 0.13)
        ry1 = by1 + int(h * 0.43)
        rx2 = bx2 - int(w * 0.055)
        ry2 = ry1 + int(h * 0.17)

        draw.rounded_rectangle(
            (rx1, ry1, rx2, ry2),
            radius=16,
            fill=(248, 244, 238, 255),
            outline=(219, 204, 189, 255),
            width=2,
        )

        draw.rounded_rectangle(
            (rx1 - 7, ry1, rx1, ry2),
            radius=4,
            fill=orange + (255,),
        )

        draw.ellipse(
            (rx1 + 20, ry1 + 26, rx1 + 45, ry1 + 51),
            outline=(190, 176, 162, 255),
            width=3,
        )

        # Side floating sample tube
        tx1 = bx2 + int(w * 0.035)
        ty1 = by1 + int(h * 0.36)
        tx2 = tx1 + int(w * 0.055)
        ty2 = ty1 + int(h * 0.25)

        draw.rounded_rectangle(
            (tx1, ty1, tx2, ty2),
            radius=14,
            fill=(255, 255, 255, 230),
            outline=(226, 210, 194, 255),
            width=2,
        )

        draw.rectangle(
            (tx1 + 6, ty1 + 8, tx2 - 6, ty1 + 28),
            fill=orange + (255,),
        )

        return img.convert("RGB")

    def _make_rounded_gradient(self, w, h, left_color, right_color, radius=28):
        left = self._hex_to_rgb(left_color)
        right = self._hex_to_rgb(right_color)

        line = Image.new("RGBA", (w, 1))
        pixels = []

        for x in range(w):
            t = x / max(1, w - 1)
            rgb = self._mix_rgb(left, right, t)
            pixels.append((rgb[0], rgb[1], rgb[2], 255))

        line.putdata(pixels)
        img = line.resize((w, h))

        mask = Image.new("L", (w, h), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle(
            (0, 0, w - 1, h - 1),
            radius=radius,
            fill=255,
        )

        rounded = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        rounded.paste(img, (0, 0), mask)

        return rounded

    # ---------------------------------------------------------------------
    # Canvas shape helpers
    # ---------------------------------------------------------------------

    def _draw_pill(
        self,
        x,
        y,
        w,
        h,
        text,
        fill,
        outline,
        text_fill,
        font,
    ):
        self._rounded_rect(
            self.canvas,
            x,
            y,
            x + w,
            y + h,
            h // 2,
            fill=fill,
            outline=outline,
            width=1,
        )

        self.canvas.create_text(
            x + w / 2,
            y + h / 2,
            text=text,
            fill=text_fill,
            font=font,
            anchor="center",
        )

    def _rounded_rect(self, canvas, x1, y1, x2, y2, r, fill, outline="", width=1):
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
            width=width,
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
            width=width,
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
            width=width,
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
            width=width,
        )

        canvas.create_rectangle(
            x1 + r,
            y1,
            x2 - r,
            y2,
            fill=fill,
            outline=outline,
            width=width,
        )
        canvas.create_rectangle(
            x1,
            y1 + r,
            x2,
            y2 - r,
            fill=fill,
            outline=outline,
            width=width,
        )

    # ---------------------------------------------------------------------
    # Config refresh
    # ---------------------------------------------------------------------

    def _read_config(self):
        return {
            "title": config.get(
                "welcome_page",
                "title",
                default="WELCOME",
            ),
            "subtitle": config.get(
                "welcome_page",
                "subtitle",
                default="Anonymous Health Screening",
            ),
            "tap_text": config.get(
                "welcome_page",
                "tap_text",
                default="TAP TO PROCEED",
            ),
        }

    def _start_config_refresh(self):
        try:
            latest = self._read_config()

            if latest != self._config_snapshot:
                self._config_snapshot = latest
                self._schedule_redraw()

        except Exception as e:
            print(f"[WELCOME] Config refresh failed: {e}", flush=True)

        self.after(1000, self._start_config_refresh)

    # ---------------------------------------------------------------------
    # Utility helpers
    # ---------------------------------------------------------------------

    def _asset_path(self, filename):
        roots = []

        try:
            roots.append(os.getcwd())
        except Exception:
            pass

        try:
            here = os.path.dirname(os.path.abspath(__file__))
            roots.append(here)
            roots.append(os.path.dirname(here))
            roots.append(os.path.dirname(os.path.dirname(here)))
        except Exception:
            pass

        for root in roots:
            candidate = os.path.join(root, "assets", filename)
            if os.path.exists(candidate):
                return candidate

        return os.path.join("assets", filename)

    def _hex_to_rgb(self, value):
        value = str(value).strip()

        if value.startswith("#"):
            value = value[1:]

        if len(value) == 3:
            value = "".join(ch * 2 for ch in value)

        try:
            return (
                int(value[0:2], 16),
                int(value[2:4], 16),
                int(value[4:6], 16),
            )
        except Exception:
            return (255, 255, 255)

    def _mix_rgb(self, a, b, t):
        t = max(0, min(1, t))

        return (
            int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t),
        )

    def go_to_login(self, event=None):
        self.controller.show_frame("QRLoginPage")