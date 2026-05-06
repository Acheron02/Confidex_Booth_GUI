from frontend import tk_compat as ctk
from frontend import theme
from frontend.widgets import PillButton


class CashNumericKeyboard(ctk.CTkFrame):
    def __init__(
        self,
        master,
        on_key,
        on_clear,
        on_submit=None,
        **kwargs
    ):
        super().__init__(master, fg_color=theme.WHITE, **kwargs)

        self.on_key = on_key
        self.on_clear = on_clear
        self.on_submit = on_submit

        for col in range(3):
            self.grid_columnconfigure(col, weight=1, uniform="keys")

        for row in range(4):
            self.grid_rowconfigure(row, weight=1, uniform="keys")

        pad_x = 12
        pad_y = 12

        self._make_btn("1", lambda: self.on_key("1")).grid(row=0, column=0, padx=pad_x, pady=pad_y, sticky="nsew")
        self._make_btn("2", lambda: self.on_key("2")).grid(row=0, column=1, padx=pad_x, pady=pad_y, sticky="nsew")
        self._make_btn("3", lambda: self.on_key("3")).grid(row=0, column=2, padx=pad_x, pady=pad_y, sticky="nsew")

        self._make_btn("4", lambda: self.on_key("4")).grid(row=1, column=0, padx=pad_x, pady=pad_y, sticky="nsew")
        self._make_btn("5", lambda: self.on_key("5")).grid(row=1, column=1, padx=pad_x, pady=pad_y, sticky="nsew")
        self._make_btn("6", lambda: self.on_key("6")).grid(row=1, column=2, padx=pad_x, pady=pad_y, sticky="nsew")

        self._make_btn("7", lambda: self.on_key("7")).grid(row=2, column=0, padx=pad_x, pady=pad_y, sticky="nsew")
        self._make_btn("8", lambda: self.on_key("8")).grid(row=2, column=1, padx=pad_x, pady=pad_y, sticky="nsew")
        self._make_btn("9", lambda: self.on_key("9")).grid(row=2, column=2, padx=pad_x, pady=pad_y, sticky="nsew")

        PillButton(
            self,
            text="CLEAR",
            command=self.on_clear,
            height=72,
            fg_color=theme.ORANGE,
            text_color=theme.WHITE,
            font=theme.font(18, "bold"),
        ).grid(row=3, column=0, padx=pad_x, pady=pad_y, sticky="nsew")

        self._make_btn("0", lambda: self.on_key("0")).grid(row=3, column=1, padx=pad_x, pady=pad_y, sticky="nsew")

        if self.on_submit:
            PillButton(
                self,
                text="CONTINUE",
                command=self.on_submit,
                height=72,
                fg_color=theme.SUCCESS,
                text_color=theme.WHITE,
                font=theme.font(18, "bold"),
            ).grid(row=3, column=2, padx=pad_x, pady=pad_y, sticky="nsew")
        else:
            spacer = ctk.CTkFrame(self, fg_color=theme.WHITE)
            spacer.grid(row=3, column=2, padx=pad_x, pady=pad_y, sticky="nsew")

    def _make_btn(self, text, command):
        return PillButton(
            self,
            text=text,
            command=command,
            height=72,
            fg_color=theme.CREAM,
            text_color=theme.BLACK,
            font=theme.heavy(28),
        )