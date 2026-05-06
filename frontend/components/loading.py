from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import AppShell, RoundedCard, card_body


class LoadingPage(ctk.CTkFrame):
    def __init__(self, master, controller):
        super().__init__(master, fg_color=theme.CREAM)
        self.controller = controller
        self.next_page = None
        self.next_kwargs = {}
        self._dot_job = None
        self._nav_job = None
        self._dot_count = 0
        self._navigated = False

        self.shell = AppShell(self, title_right='Please wait')
        self.shell.pack(fill='both', expand=True)

        wrap = ctk.CTkFrame(self.shell.body, fg_color='transparent')
        wrap.pack(expand=True, fill='both')

        wrap.grid_columnconfigure(0, weight=1)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_rowconfigure(1, weight=0)
        wrap.grid_rowconfigure(2, weight=1)

        self.card = RoundedCard(wrap, auto_size=True, pad=24)
        self.card.grid(row=1, column=0)

        body = card_body(self.card)
        body.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            body,
            text='LOADING',
            font=theme.heavy(30),
            text_color=theme.BLACK,
            fg_color=theme.WHITE
        )
        self.title_label.grid(row=0, column=0, padx=40, pady=(18, 12))

        self.message_label = ctk.CTkLabel(
            body,
            text='Preparing next page',
            font=theme.font(22, 'bold'),
            text_color=theme.MUTED,
            fg_color=theme.WHITE,
            justify='center',
            wraplength=560
        )
        self.message_label.grid(row=1, column=0, padx=40, pady=(0, 8))

        self.dots_label = ctk.CTkLabel(
            body,
            text='',
            font=theme.heavy(28),
            text_color=theme.ORANGE,
            fg_color=theme.WHITE
        )
        self.dots_label.grid(row=2, column=0, padx=40, pady=(0, 18))

    def update_data(self, message='Preparing next page', next_page=None, next_kwargs=None, delay=900, **kwargs):
        self.reset_fields()

        self.message_label.configure(text=message)
        self.next_page = next_page
        self.next_kwargs = next_kwargs or {}
        self._dot_count = 0
        self._navigated = False

        self._start_dots()
        self._nav_job = self.after(delay, self._go_next)

    def _start_dots(self):
        self._stop_dots()
        self._animate_dots()

    def _stop_dots(self):
        if self._dot_job is not None:
            try:
                self.after_cancel(self._dot_job)
            except Exception:
                pass
            self._dot_job = None

    def _stop_navigation_job(self):
        if self._nav_job is not None:
            try:
                self.after_cancel(self._nav_job)
            except Exception:
                pass
            self._nav_job = None

    def _animate_dots(self):
        if self._navigated:
            return

        self._dot_count = (self._dot_count + 1) % 4
        self.dots_label.configure(text='.' * self._dot_count)
        self._dot_job = self.after(350, self._animate_dots)

    def _go_next(self):
        if self._navigated:
            return

        self._navigated = True
        self._stop_dots()
        self._stop_navigation_job()

        if self.next_page:
            self.controller.show_frame(self.next_page, **self.next_kwargs)

    def reset_fields(self, **kwargs):
        self._navigated = True
        self._stop_dots()
        self._stop_navigation_job()
        self.dots_label.configure(text='')
        self.message_label.configure(text='Preparing next page')
        self.next_page = None
        self.next_kwargs = {}