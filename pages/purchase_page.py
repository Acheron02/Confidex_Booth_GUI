from frontend import tk_compat as ctk
from pynput import keyboard
import uuid
import threading
import requests
import json
import tkinter as tk
import os

from PIL import Image, ImageTk, ImageOps, ImageChops

from frontend import theme
from frontend.widgets import AppShell, RoundedCard, PillButton, OutlineTile, card_body
from backend.util import api_client
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


# ---------------------------------------------------------------------
# Product tile
# ---------------------------------------------------------------------

class ProductTile(OutlineTile):
    TILE_HEIGHT = 350
    PILL_HEIGHT = 44

    def __init__(self, master, product, select_callback):
        super().__init__(
            master,
            pad=14,
            auto_size=False,
            height=self.TILE_HEIGHT,
        )

        self.product = product
        self.select_callback = select_callback
        self.selected = False
        self.available = bool(product.get("available", True))

        self.product_image_ref = None
        self.original_image = None

        self.grid_propagate(False)

        body = self.content
        safe_configure(body, fg_color=WHITE)

        body.grid_columnconfigure(0, weight=1)

        body.grid_rowconfigure(0, weight=0)
        body.grid_rowconfigure(1, weight=0)
        body.grid_rowconfigure(2, weight=1)
        body.grid_rowconfigure(3, weight=0)
        body.grid_rowconfigure(4, weight=0)

        self.type_label = ctk.CTkLabel(
            body,
            text=self._product_type(),
            font=app_font(20, "bold"),
            text_color=BLACK if self.available else MUTED,
            fg_color=WHITE,
            wraplength=260,
            justify="center",
            anchor="center"
        )
        self.type_label.grid(row=0, column=0, sticky="ew", padx=10, pady=(4, 2))

        self.name_label = ctk.CTkLabel(
            body,
            text=self._product_name(),
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            wraplength=260,
            justify="center",
            anchor="center"
        )
        self.name_label.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.image_panel = ctk.CTkFrame(
            body,
            fg_color="#FFF4E8",
            corner_radius=20
        )
        self.image_panel.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 12))
        self.image_panel.grid_propagate(False)

        self.image_label = ctk.CTkLabel(
            self.image_panel,
            text="",
            fg_color="#FFF4E8"
        )
        self.image_label.pack(fill="both", expand=True, padx=6, pady=6)

        self.meta_frame = ctk.CTkFrame(
            body,
            fg_color=WHITE
        )
        self.meta_frame.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.meta_frame.grid_columnconfigure(0, weight=1, uniform="tile-meta")
        self.meta_frame.grid_columnconfigure(1, weight=1, uniform="tile-meta")

        self.price_pill = ctk.CTkFrame(
            self.meta_frame,
            fg_color="#FFF1E7",
            corner_radius=16,
            height=self.PILL_HEIGHT
        )
        self.price_pill.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.price_pill.grid_propagate(False)

        self.price_label = ctk.CTkLabel(
            self.price_pill,
            text=f"₱{self._product_price()}",
            font=app_font(16, "bold"),
            text_color=ORANGE,
            fg_color="transparent",
            anchor="center",
            justify="center"
        )
        self.price_label.place(relx=0.5, rely=0.5, anchor="center")

        self.stock_pill = ctk.CTkFrame(
            self.meta_frame,
            fg_color="#F7F7F7",
            corner_radius=16,
            height=self.PILL_HEIGHT
        )
        self.stock_pill.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.stock_pill.grid_propagate(False)

        self.stock_label = ctk.CTkLabel(
            self.stock_pill,
            text=self._stock_text(),
            font=app_font(13, "bold"),
            text_color=MUTED if self.available else ERROR,
            fg_color="transparent",
            anchor="center",
            justify="center",
            wraplength=150
        )
        self.stock_label.place(relx=0.5, rely=0.5, anchor="center")

        self.tap_hint = ctk.CTkLabel(
            body,
            text="Tap to select" if self.available else "Unavailable",
            font=app_font(12, "bold"),
            text_color=ORANGE if self.available else MUTED,
            fg_color=WHITE,
            anchor="center"
        )
        self.tap_hint.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 4))

        self.bind("<Configure>", self._on_resize, add="+")
        body.bind("<Configure>", self._on_resize, add="+")
        self.image_panel.bind("<Configure>", self._on_resize, add="+")

        self._load_product_image()
        self._apply_style()
        self._bind_clicks()

    def _product_name(self):
        return str(
            self.product.get("name")
            or self.product.get("product_name")
            or self.product.get("type")
            or "Testing Kit"
        )

    def _product_type(self):
        return str(self.product.get("type") or self.product.get("name") or "Testing Kit")

    def _product_price(self):
        return self.product.get("price", 0)

    def _stock_text(self):
        stock = int(self.product.get("stock", 0))

        if not self.available:
            return config.get("purchase_page", "out_of_stock_text", default="OUT OF STOCK")

        return f"{config.get('purchase_page', 'stock_label_prefix', default='Stock')}: {stock}"

    def _product_search_text(self):
        return (
            str(self.product.get("type", "")) + " " +
            str(self.product.get("name", "")) + " " +
            str(self.product.get("product_name", "")) + " " +
            str(self.product.get("product_id", ""))
        ).lower()

    def _resolve_product_image_path(self):
        text = self._product_search_text()

        if "dengue" in text:
            filename = "Dengue_image.png"
        elif "hiv" in text:
            filename = "HIV_image.png"
        else:
            return None

        roots = []

        try:
            roots.append(os.getcwd())
        except Exception:
            pass

        try:
            here = os.path.dirname(os.path.abspath(__file__))
            roots.extend([
                here,
                os.path.dirname(here),
                os.path.dirname(os.path.dirname(here)),
            ])
        except Exception:
            pass

        candidates = []

        for root in roots:
            candidates.extend([
                os.path.join(root, "assets", "product_images", filename),
                os.path.join(root, "assets", filename),
                os.path.join(root, filename),
            ])

        for path in candidates:
            if os.path.exists(path):
                return path

        return os.path.join("assets", "product_images", filename)

    def _trim_image_whitespace(self, img):
        try:
            img = img.convert("RGBA")

            alpha = img.getchannel("A")
            alpha_bbox = alpha.getbbox()

            if alpha_bbox:
                if alpha_bbox != (0, 0, img.width, img.height):
                    img = img.crop(alpha_bbox)

            rgb = img.convert("RGB")
            bg_color = rgb.getpixel((0, 0))
            bg = Image.new("RGB", rgb.size, bg_color)
            diff = ImageChops.difference(rgb, bg)
            diff = diff.convert("L")

            mask = diff.point(lambda p: 255 if p > 18 else 0)
            bbox = mask.getbbox()

            if bbox:
                pad = 10
                left = max(0, bbox[0] - pad)
                top = max(0, bbox[1] - pad)
                right = min(img.width, bbox[2] + pad)
                bottom = min(img.height, bbox[3] + pad)

                if right > left and bottom > top:
                    img = img.crop((left, top, right, bottom))

            return img

        except Exception:
            return img

    def _load_product_image(self):
        image_path = self._resolve_product_image_path()

        if not image_path or not os.path.exists(image_path):
            self.original_image = None
            self.image_label.configure(
                image=None,
                text="KIT",
                font=app_font(28, "bold"),
                text_color=ORANGE
            )
            return

        try:
            img = Image.open(image_path).convert("RGBA")
            img = self._trim_image_whitespace(img)

            self.original_image = img
            self._render_product_image()

        except Exception as e:
            print(f"[PRODUCT TILE] Image load failed: {e}", flush=True)
            self.original_image = None
            self.image_label.configure(
                image=None,
                text="KIT",
                font=app_font(28, "bold"),
                text_color=ORANGE
            )

    def _render_product_image(self):
        if self.original_image is None:
            return

        try:
            panel_w = max(220, self.image_panel.winfo_width() - 12)
            panel_h = max(130, self.image_panel.winfo_height() - 12)

            img = self.original_image.copy()
            img = ImageOps.contain(img, (panel_w, panel_h), Image.LANCZOS)

            self.product_image_ref = ImageTk.PhotoImage(img)
            self.image_label.configure(
                image=self.product_image_ref,
                text=""
            )

        except Exception as e:
            print(f"[PRODUCT TILE] Image render failed: {e}", flush=True)

    def _bind_clicks(self):
        widgets = [
            self,
            getattr(self, "canvas", None),
            self.content,
            self.type_label,
            self.name_label,
            self.image_panel,
            self.image_label,
            self.meta_frame,
            self.price_pill,
            self.price_label,
            self.stock_pill,
            self.stock_label,
            self.tap_hint,
        ]

        for widget in widgets:
            if widget is None:
                continue

            try:
                widget.bind("<Button-1>", self._on_click, add="+")
            except Exception:
                pass

    def _on_click(self, event=None):
        if self.available:
            self.select_callback(self)

    def _on_resize(self, event=None):
        try:
            width = max(190, self.content.winfo_width() - 30)
            self.type_label.configure(wraplength=width)
            self.name_label.configure(wraplength=width)
            self.stock_label.configure(wraplength=150)
            self._render_product_image()
        except Exception:
            pass

    def _apply_style(self):
        if self.selected:
            card_bg = ORANGE
            image_bg = "#FBE2CE"
            title_color = WHITE
            subtitle_color = "#FFF2E8"
            price_bg = WHITE
            price_color = ORANGE
            stock_bg = WHITE
            stock_color = ORANGE
            hint_color = WHITE
        elif not self.available:
            card_bg = "#F7F7F7"
            image_bg = "#EFEFEF"
            title_color = MUTED
            subtitle_color = MUTED
            price_bg = "#EFEFEF"
            price_color = MUTED
            stock_bg = "#FFECEC"
            stock_color = ERROR
            hint_color = MUTED
        else:
            card_bg = WHITE
            image_bg = "#FFF4E8"
            title_color = BLACK
            subtitle_color = MUTED
            price_bg = "#FFF1E7"
            price_color = ORANGE
            stock_bg = "#F7F7F7"
            stock_color = MUTED
            hint_color = ORANGE

        safe_configure(self, fg_color=card_bg)
        safe_configure(self.content, fg_color=card_bg)
        safe_configure(self.meta_frame, fg_color=card_bg)

        safe_configure(self.type_label, fg_color=card_bg)
        safe_configure(self.name_label, fg_color=card_bg)
        safe_configure(self.tap_hint, fg_color=card_bg)

        safe_configure(self.image_panel, fg_color=image_bg)
        safe_configure(self.image_label, fg_color=image_bg)

        self.type_label.configure(text_color=title_color)
        self.name_label.configure(text_color=subtitle_color)
        self.tap_hint.configure(text_color=hint_color)

        self.price_pill.configure(fg_color=price_bg)
        self.price_label.configure(text_color=price_color)

        self.stock_pill.configure(fg_color=stock_bg)
        self.stock_label.configure(text_color=stock_color)

    def set_selected(self, selected: bool):
        if selected and not self.available:
            return

        self.selected = bool(selected)
        self._apply_style()

    def update_product(self, product):
        self.product = product
        self.available = bool(product.get("available", True))

        if not self.available:
            self.selected = False

        self.type_label.configure(text=self._product_type())
        self.name_label.configure(text=self._product_name())
        self.price_label.configure(text=f"₱{self._product_price()}")
        self.stock_label.configure(text=self._stock_text())
        self.tap_hint.configure(text="Tap to select" if self.available else "Unavailable")

        self._load_product_image()
        self._apply_style()


# ---------------------------------------------------------------------
# Purchase page
# ---------------------------------------------------------------------

class PurchasePage(ctk.CTkFrame):
    REFRESH_MS = 1500

    def __init__(self, master, controller, user_data=None):
        super().__init__(master, fg_color=CREAM)

        self.controller = controller
        self.user_data = user_data or {}
        self.username = self.user_data.get("username", "User")
        self.userID = self.user_data.get("userID")

        self.selected_tile = None
        self.selected_product = None
        self.discount = None
        self.transaction_id = None

        self.qr_buffer = ""
        self.scan_disabled = False
        self.listener = None

        self.validation_in_progress = False
        self.last_scanned_token = None
        self.discount_applied_token = None
        self._config_refresh_job = None
        self._last_products_signature = None
        self._empty_tiles_label = None

        self.shell = AppShell(self, title_right=f"Welcome, {self.username}!")
        self.shell.pack(fill="both", expand=True)

        self.logout_btn = PillButton(
            self.shell.header_inner,
            text=config.get("purchase_page", "logout_button_text", default="Logout"),
            width=132,
            height=44,
            command=self.logout,
            font=app_font(15, "bold")
        )
        self.logout_btn.pack(side="right", padx=24)

        self.page = ctk.CTkFrame(
            self.shell.body,
            fg_color=CREAM
        )
        self.page.pack(fill="both", expand=True)

        self.page.grid_columnconfigure(0, weight=1)
        self.page.grid_rowconfigure(1, weight=1)

        self.header_area = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.header_area.grid(row=0, column=0, sticky="ew", padx=28, pady=(16, 6))
        self.header_area.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.header_area,
            text=config.get("purchase_page", "title", default="SELECT A PRODUCT"),
            font=app_heavy(34),
            text_color=BLACK,
            anchor="w",
            justify="left"
        )
        self.title_label.grid(row=0, column=0, sticky="w")

        self.subtitle_label = ctk.CTkLabel(
            self.header_area,
            text="Choose your testing kit, review the receipt, then continue to payment.",
            font=app_font(15, "normal"),
            text_color=MUTED,
            anchor="w",
            justify="left",
            wraplength=760
        )
        self.subtitle_label.grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.content = ctk.CTkFrame(
            self.page,
            fg_color=CREAM
        )
        self.content.grid(row=1, column=0, sticky="nsew", padx=28, pady=(8, 20))

        self.content.grid_columnconfigure(0, weight=12, uniform="purchase-col")
        self.content.grid_columnconfigure(1, weight=10, uniform="purchase-col")
        self.content.grid_rowconfigure(0, weight=1)

        self.left_card = RoundedCard(
            self.content,
            fg_color=WHITE,
            radius=34,
            auto_size=False
        )
        self.left_card.grid(row=0, column=0, sticky="nsew", padx=(0, 14), pady=0)

        self.right_card = RoundedCard(
            self.content,
            fg_color=WHITE,
            radius=34,
            auto_size=False
        )
        self.right_card.grid(row=0, column=1, sticky="nsew", padx=(14, 0), pady=0)

        left_body = card_body(self.left_card)
        right_body = card_body(self.right_card)

        left_body.grid_columnconfigure(0, weight=1)
        left_body.grid_rowconfigure(0, weight=0)
        left_body.grid_rowconfigure(1, weight=1)

        self.left_header = ctk.CTkFrame(
            left_body,
            fg_color=WHITE
        )
        self.left_header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 10))
        self.left_header.grid_columnconfigure(0, weight=1)

        self.left_title = ctk.CTkLabel(
            self.left_header,
            text=config.get("purchase_page", "section_title", default="Testing Kits"),
            font=app_heavy(27),
            text_color=BLACK,
            fg_color=WHITE,
            anchor="w",
            justify="left"
        )
        self.left_title.grid(row=0, column=0, sticky="w")

        self.left_hint = ctk.CTkLabel(
            self.left_header,
            text="Tap a kit to preview the order receipt.",
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="w",
            justify="left"
        )
        self.left_hint.grid(row=1, column=0, sticky="w", pady=(3, 0))

        self.tiles_frame = ctk.CTkFrame(
            left_body,
            fg_color=WHITE
        )
        self.tiles_frame.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 20))

        right_body.grid_columnconfigure(0, weight=1)
        right_body.grid_rowconfigure(0, weight=0)
        right_body.grid_rowconfigure(1, weight=1)
        right_body.grid_rowconfigure(2, weight=0)
        right_body.grid_rowconfigure(3, weight=0)

        self.right_header = ctk.CTkFrame(
            right_body,
            fg_color=WHITE
        )
        self.right_header.grid(row=0, column=0, sticky="ew", padx=24, pady=(22, 8))
        self.right_header.grid_columnconfigure(0, weight=1)

        self.right_title = ctk.CTkLabel(
            self.right_header,
            text=config.get("purchase_page", "order_title", default="Order Details"),
            font=app_heavy(27),
            text_color=BLACK,
            fg_color=WHITE,
            anchor="w",
            justify="left"
        )
        self.right_title.grid(row=0, column=0, sticky="w")

        self.right_hint = ctk.CTkLabel(
            self.right_header,
            text="Receipt preview is generated after selecting a kit.",
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color=WHITE,
            anchor="w",
            justify="left",
            wraplength=520
        )
        self.right_hint.grid(row=1, column=0, sticky="w", pady=(3, 0))

        self.order_box = ctk.CTkFrame(
            right_body,
            fg_color="#FFFDF8",
            border_color="#EBD8C6",
            border_width=2,
            corner_radius=24
        )
        self.order_box.grid(row=1, column=0, sticky="nsew", padx=24, pady=(6, 10))
        self.order_box.grid_columnconfigure(0, weight=1)
        self.order_box.grid_rowconfigure(0, weight=1)

        self.order_content = ctk.CTkFrame(
            self.order_box,
            fg_color="#FFFDF8"
        )
        self.order_content.grid(row=0, column=0, sticky="nsew", padx=22, pady=18)

        self.status_label = ctk.CTkLabel(
            right_body,
            text="",
            font=app_font(15, "bold"),
            text_color=ERROR,
            wraplength=540,
            justify="center",
            anchor="center",
            fg_color=WHITE
        )
        self.status_label.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 8))

        self.button_panel = ctk.CTkFrame(
            right_body,
            fg_color=WHITE
        )
        self.button_panel.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 22))
        self.button_panel.grid_columnconfigure(0, weight=1)
        self.button_panel.grid_columnconfigure(1, weight=1)

        self.scan_btn = PillButton(
            self.button_panel,
            text=config.get("purchase_page", "discount_button_text", default="Apply Discount"),
            width=230,
            height=64,
            command=self.start_scan,
            state="disabled",
            font=app_font(17, "bold")
        )
        self.scan_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.pay_btn = PillButton(
            self.button_panel,
            text=config.get("purchase_page", "pay_button_text", default="Continue to Pay"),
            width=230,
            height=64,
            command=self.go_to_payment,
            state="disabled",
            font=app_font(17, "bold")
        )
        self.pay_btn.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        self.tiles = []
        self.products = []

        self.reload_products(force=True)
        self._start_config_refresh()

    # ---------------------------------------------------------------------
    # Product loading
    # ---------------------------------------------------------------------

    def _build_products_from_config(self):
        products = []

        for product in config.get_enabled_products():
            product_id = product.get("product_id")

            if not product_id:
                continue

            stock = config.get_product_stock(product_id)
            merged = product.copy()
            merged["stock"] = stock
            merged["available"] = stock > 0 and bool(product.get("enabled", True))
            products.append(merged)

        return products

    def _make_products_signature(self, products):
        return json.dumps(products, sort_keys=True)

    def _clear_tiles_frame(self):
        for child in self.tiles_frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        self.tiles = []
        self._empty_tiles_label = None

        for col in range(4):
            self.tiles_frame.grid_columnconfigure(col, weight=0, uniform="")
        for row in range(10):
            self.tiles_frame.grid_rowconfigure(row, weight=0)

    def _get_column_count(self, item_count: int) -> int:
        if item_count <= 1:
            return 1

        return 2

    def reload_products(self, force=False):
        new_products = self._build_products_from_config()
        new_signature = self._make_products_signature(new_products)

        if not force and new_signature == self._last_products_signature:
            return

        self._last_products_signature = new_signature
        self.products = new_products

        self._clear_tiles_frame()

        if not self.products:
            self.tiles_frame.grid_columnconfigure(0, weight=1)
            self.tiles_frame.grid_rowconfigure(0, weight=1)

            self._empty_tiles_label = ctk.CTkLabel(
                self.tiles_frame,
                text="No products available",
                font=app_font(18, "bold"),
                text_color=MUTED,
                fg_color=WHITE
            )
            self._empty_tiles_label.grid(row=0, column=0, pady=24, sticky="n")
            self.tiles.append(self._empty_tiles_label)
            self.update_order_summary()
            return

        column_count = self._get_column_count(len(self.products))

        for col in range(column_count):
            self.tiles_frame.grid_columnconfigure(col, weight=1, uniform="product-col")

        row_count = (len(self.products) + column_count - 1) // column_count

        for row in range(row_count):
            self.tiles_frame.grid_rowconfigure(row, weight=1)

        for index, product in enumerate(self.products):
            row = index // column_count
            col = index % column_count

            tile = ProductTile(self.tiles_frame, product, self.on_tile_selected)
            tile.grid(row=row, column=col, sticky="nsew", padx=10, pady=10)
            self.tiles.append(tile)

        if self.selected_product:
            selected_id = self.selected_product.get("product_id")
            selected_transaction_id = (
                self.selected_product.get("transaction_id")
                or self.selected_product.get("transactionID")
                or self.selected_product.get("selection_id")
                or self.transaction_id
            )
            still_exists = None

            for product in self.products:
                if product.get("product_id") == selected_id and product.get("available"):
                    still_exists = product.copy()
                    break

            if still_exists:
                self.selected_product = still_exists

                if selected_transaction_id:
                    self.transaction_id = str(selected_transaction_id)
                    self.selected_product["selection_id"] = self.transaction_id
                    self.selected_product["transaction_id"] = self.transaction_id
                    self.selected_product["transactionID"] = self.transaction_id

                self.selected_tile = None

                for tile in self.tiles:
                    if isinstance(tile, ProductTile) and tile.product.get("product_id") == selected_id:
                        self.selected_tile = tile
                        tile.set_selected(True)
                        break
            else:
                self.selected_tile = None
                self.selected_product = None
                self.discount = None
                self.transaction_id = None
                self.scan_disabled = False

        self.update_order_summary()

    # ---------------------------------------------------------------------
    # Receipt UI
    # ---------------------------------------------------------------------

    def _clear_order_content(self):
        for child in self.order_content.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

    def _make_receipt_divider(self, parent, pady=9):
        line = ctk.CTkFrame(
            parent,
            fg_color="#E8D8CA",
            height=2,
            corner_radius=999
        )
        line.pack(fill="x", padx=2, pady=pady)
        return line

    def _make_receipt_row(self, parent, label, value, value_color=None, large=False):
        row = ctk.CTkFrame(
            parent,
            fg_color="#FFFDF8"
        )
        row.pack(fill="x", pady=5)

        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=1)

        label_widget = ctk.CTkLabel(
            row,
            text=label,
            font=app_font(14 if not large else 15, "bold"),
            text_color=MUTED,
            fg_color="#FFFDF8",
            anchor="w",
            justify="left",
            wraplength=210
        )
        label_widget.grid(row=0, column=0, sticky="w")

        value_widget = ctk.CTkLabel(
            row,
            text=value,
            font=app_font(17 if not large else 25, "bold"),
            text_color=value_color or BLACK,
            fg_color="#FFFDF8",
            anchor="e",
            justify="right",
            wraplength=270
        )
        value_widget.grid(row=0, column=1, sticky="e")

        return row

    def _build_empty_receipt(self):
        receipt = ctk.CTkFrame(
            self.order_content,
            fg_color="#FFFDF8"
        )
        receipt.pack(fill="both", expand=True)

        receipt.grid_columnconfigure(0, weight=1)
        receipt.grid_rowconfigure(0, weight=1)

        center = ctk.CTkFrame(
            receipt,
            fg_color="#FFFDF8"
        )
        center.grid(row=0, column=0, sticky="nsew")
        center.grid_columnconfigure(0, weight=1)
        center.grid_rowconfigure(0, weight=1)
        center.grid_rowconfigure(1, weight=0)
        center.grid_rowconfigure(2, weight=0)
        center.grid_rowconfigure(3, weight=1)

        icon = tk.Canvas(
            center,
            width=96,
            height=96,
            bg="#FFFDF8",
            highlightthickness=0,
            bd=0
        )
        icon.grid(row=1, column=0, pady=(0, 14))
        self._draw_receipt_icon(icon)

        empty_title = ctk.CTkLabel(
            center,
            text=config.get(
                "purchase_page",
                "no_item_selected_text",
                default="Select a product to view details"
            ),
            font=app_font(21, "bold"),
            text_color=BLACK,
            fg_color="#FFFDF8",
            justify="center",
            wraplength=420
        )
        empty_title.grid(row=2, column=0, sticky="ew", pady=(0, 6))

        empty_sub = ctk.CTkLabel(
            center,
            text="Your order receipt will be generated here.",
            font=app_font(14, "normal"),
            text_color=MUTED,
            fg_color="#FFFDF8",
            justify="center",
            wraplength=420
        )
        empty_sub.grid(row=3, column=0, sticky="n", pady=(0, 0))

    def _draw_receipt_icon(self, canvas):
        c = canvas
        c.delete("all")

        bg = "#FFF0E3"
        dark = "#2D2620"
        line = "#EBD8C6"

        c.create_oval(3, 3, 93, 93, fill=bg, outline=bg)
        self._canvas_round_rect(c, 29, 20, 67, 76, 8, WHITE, line, 2)

        for i, y in enumerate([32, 43, 54]):
            c.create_line(37, y, 59, y, fill=dark if i == 0 else line, width=2)

        c.create_line(37, 66, 59, 66, fill=ORANGE, width=3)

    def _canvas_round_rect(self, canvas, x1, y1, x2, y2, r, fill, outline="", width=1):
        if not outline:
            outline = fill

        x1 = int(x1)
        y1 = int(y1)
        x2 = int(x2)
        y2 = int(y2)
        r = int(max(1, min(r, abs(x2 - x1) // 2, abs(y2 - y1) // 2)))

        canvas.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, fill=fill, outline=outline, width=width)
        canvas.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, fill=fill, outline=outline, width=width)
        canvas.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, fill=fill, outline=outline, width=width)
        canvas.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, fill=fill, outline=outline, width=width)
        canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=outline, width=width)
        canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline=outline, width=width)

    def update_order_summary(self):
        self._clear_order_content()

        if not self.selected_product:
            self._build_empty_receipt()
            self.pay_btn.configure(state="disabled")
            self.scan_btn.configure(state="disabled")
            return

        product_name = str(
            self.selected_product.get("name")
            or self.selected_product.get("product_name")
            or self.selected_product.get("type")
            or "Testing Kit"
        )
        product_type = str(self.selected_product.get("type") or product_name)
        price = float(self.selected_product.get("price", 0))
        discount_value = float(self.discount or 0)
        discount_text = "-" if discount_value <= 0 else f"{discount_value:.0f}%"
        total = price * (1 - discount_value / 100.0)

        item_label = config.get("purchase_page", "summary_item_label", default="Item")
        type_label = config.get("purchase_page", "summary_type_label", default="Type")
        discount_label = config.get("purchase_page", "summary_discount_label", default="Discount")
        total_label = config.get("purchase_page", "summary_total_label", default="Total Price")

        receipt = ctk.CTkFrame(
            self.order_content,
            fg_color="#FFFDF8"
        )
        receipt.pack(fill="both", expand=True)

        transaction_id = str(
            self.selected_product.get("transaction_id")
            or self.selected_product.get("transactionID")
            or self.selected_product.get("selection_id")
            or ""
        ).upper()

        if not transaction_id:
            transaction_id = "PENDING"

        top = ctk.CTkFrame(
            receipt,
            fg_color="#FFFDF8"
        )
        top.pack(fill="x", pady=(0, 4))

        brand = ctk.CTkLabel(
            top,
            text="CONFIDEX",
            font=app_font(19, "bold"),
            text_color=BLACK,
            fg_color="#FFFDF8",
            anchor="center"
        )
        brand.pack(anchor="center")

        receipt_title = ctk.CTkLabel(
            top,
            text="ORDER RECEIPT",
            font=app_font(12, "bold"),
            text_color=ORANGE,
            fg_color="#FFFDF8",
            anchor="center"
        )
        receipt_title.pack(anchor="center", pady=(2, 0))

        transaction_id_label = ctk.CTkLabel(
            top,
            text=f"Transaction ID: {transaction_id}",
            font=app_font(11, "normal"),
            text_color=MUTED,
            fg_color="#FFFDF8",
            anchor="center"
        )
        transaction_id_label.pack(anchor="center", pady=(2, 0))

        self._make_receipt_divider(receipt, pady=9)

        self._make_receipt_row(receipt, item_label, product_name)
        self._make_receipt_row(receipt, type_label, product_type)
        self._make_receipt_row(receipt, "Quantity", "1 pc")

        self._make_receipt_divider(receipt, pady=9)

        self._make_receipt_row(receipt, "Unit Price", f"₱{price:.2f}")

        discount_color = SUCCESS if discount_value > 0 else MUTED
        self._make_receipt_row(
            receipt,
            discount_label,
            discount_text,
            value_color=discount_color
        )

        self._make_receipt_divider(receipt, pady=9)

        self._make_receipt_row(
            receipt,
            total_label,
            f"₱{total:.2f}",
            value_color=ORANGE,
            large=True
        )

        footer = ctk.CTkLabel(
            receipt,
            text="Review your order before continuing to payment.",
            font=app_font(12, "normal"),
            text_color=MUTED,
            fg_color="#FFFDF8",
            justify="center",
            wraplength=420
        )
        footer.pack(fill="x", pady=(8, 0))

        is_available = bool(self.selected_product.get("available", False))

        self.pay_btn.configure(state="normal" if is_available else "disabled")
        self.scan_btn.configure(
            state="normal" if is_available and not self.scan_disabled else "disabled"
        )

    # ---------------------------------------------------------------------
    # Config refresh
    # ---------------------------------------------------------------------

    def _refresh_text_only(self):
        self.title_label.configure(
            text=config.get("purchase_page", "title", default="SELECT A PRODUCT")
        )
        self.left_title.configure(
            text=config.get("purchase_page", "section_title", default="Testing Kits")
        )
        self.right_title.configure(
            text=config.get("purchase_page", "order_title", default="Order Details")
        )
        self.logout_btn.configure(
            text=config.get("purchase_page", "logout_button_text", default="Logout")
        )
        self.scan_btn.configure(
            text=config.get("purchase_page", "discount_button_text", default="Apply Discount")
        )
        self.pay_btn.configure(
            text=config.get("purchase_page", "pay_button_text", default="Continue to Pay")
        )

    def _refresh_from_config(self):
        try:
            self._refresh_text_only()
            self.reload_products(force=False)
        except Exception as e:
            print(f"[PURCHASE] Config refresh failed: {e}", flush=True)

    def _start_config_refresh(self):
        self._refresh_from_config()
        self._config_refresh_job = self.after(self.REFRESH_MS, self._start_config_refresh)

    # ---------------------------------------------------------------------
    # Selection and latest product checking
    # ---------------------------------------------------------------------

    def _resolve_latest_selected_product(self):
        if not self.selected_product:
            return None

        selected_id = (
            self.selected_product.get("product_id")
            or self.selected_product.get("productID")
            or self.selected_product.get("id")
        )

        if not selected_id:
            return None

        latest_product = config.get_product_by_id(selected_id)

        if not latest_product:
            return None

        stock = config.get_product_stock(selected_id)
        merged = latest_product.copy()
        merged["stock"] = stock
        merged["available"] = stock > 0 and bool(latest_product.get("enabled", True))
        return merged

    def _new_transaction_id(self):
        return f"TXN-{uuid.uuid4().hex[:12].upper()}"

    def _ensure_transaction_id(self):
        if not self.transaction_id:
            self.transaction_id = self._new_transaction_id()

        if self.selected_product is not None:
            self.selected_product["selection_id"] = self.transaction_id
            self.selected_product["transaction_id"] = self.transaction_id
            self.selected_product["transactionID"] = self.transaction_id

        if self.user_data is not None:
            self.user_data["transaction_id"] = self.transaction_id
            self.user_data["latest_transaction_id"] = self.transaction_id

        return self.transaction_id

    def update_data(self, user_data=None, **kwargs):
        if user_data:
            self.user_data = user_data
            self.username = user_data.get("username", "User")
            self.userID = user_data.get("userID")
            self.shell.set_header_right(f"Welcome, {self.username}!")

        self.reload_products(force=True)

    def logout(self):
        if self.listener:
            self.listener.stop()
            self.listener = None

        self.reset_fields()
        self.user_data = {}
        self.username = "User"
        self.userID = None
        self.shell.set_header_right("Welcome, User!")

        if hasattr(self.controller, "current_user"):
            self.controller.current_user = None

        if hasattr(self.controller, "frames"):
            qr_page = self.controller.frames.get("QRLoginPage")

            if qr_page and hasattr(qr_page, "reset_fields"):
                qr_page.reset_fields()

        self.controller.show_frame("WelcomePage")

    def on_tile_selected(self, tile):
        if not tile.available:
            self.status_label.configure(
                text=config.get("purchase_page", "out_of_stock_text", default="OUT OF STOCK"),
                text_color=ERROR
            )
            return

        if self.selected_tile:
            self.selected_tile.set_selected(False)

        tile.set_selected(True)
        self.selected_tile = tile
        self.selected_product = tile.product.copy()
        self.transaction_id = self._new_transaction_id()
        self.selected_product["selection_id"] = self.transaction_id
        self.selected_product["transaction_id"] = self.transaction_id
        self.selected_product["transactionID"] = self.transaction_id

        self.discount = None
        self.scan_disabled = False
        self.validation_in_progress = False
        self.qr_buffer = ""
        self.last_scanned_token = None
        self.discount_applied_token = None

        if self.listener:
            self.listener.stop()
            self.listener = None

        self.status_label.configure(text="", text_color=ERROR)
        self.update_order_summary()

    # ---------------------------------------------------------------------
    # Discount QR scanning
    # ---------------------------------------------------------------------

    def start_scan(self):
        if self.scan_disabled or not self.selected_product or self.validation_in_progress:
            return

        latest = self._resolve_latest_selected_product()

        if latest is None or not latest.get("available", False):
            self.selected_product = None
            self.selected_tile = None
            self.discount = None
            self.scan_disabled = False
            self.reload_products(force=True)
            self.status_label.configure(
                text=config.get("purchase_page", "out_of_stock_text", default="OUT OF STOCK"),
                text_color=ERROR
            )
            return

        transaction_id = self._ensure_transaction_id()

        latest["selection_id"] = transaction_id
        latest["transaction_id"] = transaction_id
        latest["transactionID"] = transaction_id

        self.selected_product = latest
        self.update_order_summary()

        self.qr_buffer = ""
        self.last_scanned_token = None

        if self.listener:
            self.listener.stop()
            self.listener = None

        self.listener = keyboard.Listener(on_press=self.on_key_press)
        self.listener.start()

        self.status_label.configure(
            text=config.get(
                "purchase_page",
                "discount_ready_text",
                default="Ready to scan discount QR."
            ),
            text_color=INFO
        )
        print("[PURCHASE] Discount QR scan started", flush=True)

    def on_key_press(self, key):
        if self.validation_in_progress or self.scan_disabled:
            return

        try:
            if hasattr(key, "char") and key.char:
                self.qr_buffer += key.char
        except AttributeError:
            pass

        if key == keyboard.Key.enter:
            scanned = self.qr_buffer.strip()
            self.qr_buffer = ""

            print(f"[PURCHASE] Raw scanned QR token: {repr(scanned)}", flush=True)

            if not scanned:
                self.after(
                    0,
                    lambda: self.status_label.configure(
                        text=config.get(
                            "purchase_page",
                            "empty_qr_text",
                            default="Empty QR scan detected."
                        ),
                        text_color=ERROR
                    )
                )
                return

            if self.discount_applied_token == scanned:
                print(f"[PURCHASE] Ignoring already applied token: {repr(scanned)}", flush=True)
                self.after(
                    0,
                    lambda: self.status_label.configure(
                        text=config.get(
                            "purchase_page",
                            "discount_success_text",
                            default="QR token valid. Discount applied."
                        ),
                        text_color=SUCCESS
                    )
                )
                self.after(0, self.update_order_summary)
                return

            if self.last_scanned_token == scanned:
                print(f"[PURCHASE] Ignoring duplicate token: {repr(scanned)}", flush=True)
                return

            self.last_scanned_token = scanned
            self.validation_in_progress = True

            if self.listener:
                self.listener.stop()
                self.listener = None

            threading.Thread(
                target=self._validate_and_apply_discount,
                args=(scanned,),
                daemon=True
            ).start()

    def _apply_discount_success(self, scanned):
        self.discount = float(config.get("purchase_page", "discount_percent", default=10))
        self.scan_disabled = True
        self.discount_applied_token = scanned

        self.status_label.configure(
            text=config.get(
                "purchase_page",
                "discount_success_text",
                default="QR token valid. Discount applied."
            ),
            text_color=SUCCESS
        )
        self.update_order_summary()

    def _validate_and_apply_discount(self, scanned):
        try:
            print(f"[PURCHASE] Validating token for userID={repr(self.userID)} token={repr(scanned)}", flush=True)

            response = api_client.validate_discount_token(self.userID, scanned)

            content_type = response.headers.get("content-type", "")
            data = response.json() if content_type.startswith("application/json") else {}

            print(f"[PURCHASE] Validate response status={response.status_code}", flush=True)
            print(f"[PURCHASE] Validate response data={data}", flush=True)

            valid = response.ok and data.get("valid", False)

            if valid:
                self.after(0, lambda s=scanned: self._apply_discount_success(s))
                return

            msg = data.get("error") or config.get(
                "purchase_page",
                "invalid_discount_text",
                default="Invalid or expired QR token."
            )

            if msg == "Token already used." and self.discount_applied_token == scanned:
                print(f"[PURCHASE] Ignoring already-used response after successful apply: {repr(scanned)}", flush=True)
                self.after(0, lambda s=scanned: self._apply_discount_success(s))
                return

            self.after(
                0,
                lambda m=msg: self.status_label.configure(
                    text=m,
                    text_color=ERROR
                )
            )

        except requests.RequestException as e:
            self.after(
                0,
                lambda err=str(e): self.status_label.configure(
                    text=f"{config.get('purchase_page', 'discount_error_prefix', default='QR validation failed:')} {err}",
                    text_color=ERROR
                )
            )
        finally:
            self.validation_in_progress = False

    # ---------------------------------------------------------------------
    # Payment
    # ---------------------------------------------------------------------

    def go_to_payment(self):
        if not self.selected_product or not self.user_data:
            return

        latest = self._resolve_latest_selected_product()

        if latest is None or not latest.get("available", False):
            self.selected_product = None
            self.selected_tile = None
            self.discount = None
            self.scan_disabled = False
            self.reload_products(force=True)
            self.status_label.configure(
                text=config.get("purchase_page", "out_of_stock_text", default="OUT OF STOCK"),
                text_color=ERROR
            )
            return

        transaction_id = self._ensure_transaction_id()

        latest["selection_id"] = transaction_id
        latest["transaction_id"] = transaction_id
        latest["transactionID"] = transaction_id

        self.selected_product = latest
        self.update_order_summary()

        payload_product = self.selected_product.copy()

        self.controller.show_loading_then(
            config.get(
                "purchase_page",
                "payment_loading_text",
                default="Preparing payment options"
            ),
            "PaymentMethodPage",
            delay=1000,
            user_data=self.user_data,
            product=payload_product,
            selected_product=payload_product,
            selected_item=payload_product,
            discount=self.discount or 0,
            transaction_id=transaction_id
        )

    def reset_fields(self, **kwargs):
        if self.listener:
            self.listener.stop()
            self.listener = None

        self.selected_tile = None
        self.selected_product = None
        self.discount = None
        self.transaction_id = None
        self.qr_buffer = ""
        self.scan_disabled = False
        self.validation_in_progress = False
        self.last_scanned_token = None
        self.discount_applied_token = None

        for tile in getattr(self, "tiles", []):
            if isinstance(tile, ProductTile):
                tile.set_selected(False)

        self._clear_order_content()
        self._build_empty_receipt()

        self.status_label.configure(text="", text_color=ERROR)
        self.pay_btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled")
        self.reload_products(force=True)