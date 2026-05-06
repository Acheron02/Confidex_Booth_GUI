
from frontend import tk_compat as ctk

BLACK = "#000000"
CREAM = "#F5F2DE"
ORANGE = "#C46A2A"
WHITE = "#FFFFFF"
GRAY = "#696969"
TEXT = "#111111"
MUTED = "#555555"
SUCCESS = "#237B4B"
ERROR = "#B3261E"
INFO = "#2457A5"
BORDER = "#1E1E1E"
CARD = WHITE

HEADER_H = 88
FOOTER_H = 72

WEBSITE_TEXT = "confidex.local"


def font(size: int, weight: str | None = None, family: str = "Arial"):
    return ctk.CTkFont(family=family, size=size, weight=weight or "normal")


def heavy(size: int, family: str = "Arial"):
    return ctk.CTkFont(family=family, size=size, weight="bold")
