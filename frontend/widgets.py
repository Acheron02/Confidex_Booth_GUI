from frontend import tk_compat as ctk
from frontend import theme
from frontend.components import RoundedCard, OutlineTile, PillButton
from PIL import Image, ImageTk


def card_body(card):
    return getattr(card, 'content', card)


class AppShell(ctk.CTkFrame):
    def __init__(self, master, title_right: str | None = None, website_text: str | None = None):
        super().__init__(master, fg_color=theme.CREAM)

        self.header = ctk.CTkFrame(self, height=theme.HEADER_H, fg_color=theme.BLACK, border_width=0)
        self.header.pack(fill='x', side='top')
        self.header.pack_propagate(False)

        self.header_inner = ctk.CTkFrame(self.header, fg_color='transparent')
        self.header_inner.pack(fill='both', expand=True, padx=26)

        img = Image.open("assets/logo1.png")
        img.thumbnail((400, 400))

        self.logo_img = ImageTk.PhotoImage(img)

        self.logo = ctk.CTkLabel(
            self.header_inner,
            image=self.logo_img,
            text=""
        )
        self.logo.pack(side='left') 
        self.header_right = ctk.CTkLabel(
            self.header_inner,
            text=title_right or '',
            font=theme.font(22, 'bold'),
            text_color=theme.WHITE
        )
        self.header_right.pack(side='right')

        self.footer = ctk.CTkFrame(self, height=theme.FOOTER_H, fg_color=theme.BLACK, border_width=0)
        self.footer.pack(fill='x', side='bottom')
        self.footer.pack_propagate(False)

        self.footer_label = ctk.CTkLabel(
            self.footer,
            text=website_text or theme.WEBSITE_TEXT,
            font=theme.font(16, 'bold'),
            text_color=theme.WHITE
        )
        self.footer_label.pack(side='left', padx=20, pady=18)

        self.body = ctk.CTkFrame(self, fg_color=theme.CREAM, border_width=0)
        self.body.pack(fill='both', expand=True)

    def set_header_right(self, value: str = ''):
        self.header_right.configure(text=value or '')