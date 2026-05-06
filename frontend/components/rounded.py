import tkinter as tk
from tkinter import font as tkfont


def _parent_bg(master, default='#F5F2DE'):
    try:
        return master.cget('bg')
    except Exception:
        return default


def round_rect_points(x1, y1, x2, y2, r):
    return [
        x1 + r, y1,
        x2 - r, y1,
        x2, y1,
        x2, y1 + r,
        x2, y2 - r,
        x2, y2,
        x2 - r, y2,
        x1 + r, y2,
        x1, y2,
        x1, y2 - r,
        x1, y1 + r,
        x1, y1,
    ]


class RoundedContainer(tk.Frame):
    def __init__(
        self,
        master,
        width=None,
        height=None,
        fg_color='#FFFFFF',
        border_color='#1E1E1E',
        border_width=2,
        radius=26,
        pad=10,
        auto_size=False,
        **kwargs,
    ):
        bg = _parent_bg(master)
        super().__init__(
            master,
            bg=bg,
            bd=0,
            highlightthickness=0,
            **kwargs
        )

        self._explicit_width = width
        self._explicit_height = height
        self._fill = fg_color
        self._border = border_color
        self._border_width = border_width
        self._radius = radius
        self._pad = pad
        self._bg = bg
        self._auto_size = auto_size

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)

        self.content = tk.Frame(self, bg=fg_color, bd=0, highlightthickness=0)
        self.content.place(x=0, y=0)

        if width is not None:
            tk.Frame.configure(self, width=width)
        if height is not None:
            tk.Frame.configure(self, height=height)

        self.content.bind('<Configure>', self._sync_layout, add='+')
        self.bind('<Configure>', self._sync_layout, add='+')
        self.after_idle(self._sync_layout)

    def _sync_layout(self, event=None):
        self.update_idletasks()

        current_w = self.winfo_width()
        current_h = self.winfo_height()

        if self._auto_size:
            content_req_w = self.content.winfo_reqwidth()
            content_req_h = self.content.winfo_reqheight()
            total_extra = (self._pad * 2) + (self._border_width * 2)

            target_w = self._explicit_width if self._explicit_width is not None else content_req_w + total_extra
            target_h = self._explicit_height if self._explicit_height is not None else content_req_h + total_extra

            if current_w != target_w or current_h != target_h:
                tk.Frame.configure(self, width=target_w, height=target_h)
                current_w = target_w
                current_h = target_h
        else:
            if self._explicit_width is not None and current_w <= 1:
                current_w = self._explicit_width
                tk.Frame.configure(self, width=current_w)

            if self._explicit_height is not None and current_h <= 1:
                current_h = self._explicit_height
                tk.Frame.configure(self, height=current_h)

        w = max(2, current_w)
        h = max(2, current_h)
        inset = self._pad + self._border_width

        self.content.place(
            x=inset,
            y=inset,
            width=max(1, w - 2 * inset),
            height=max(1, h - 2 * inset)
        )

        self._redraw()

    def _redraw(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        r = min(self._radius, w // 2, h // 2)

        self.canvas.delete('all')

        if self._border_width > 0:
            self._draw_shape(0, 0, w, h, self._border, r)
            inset = self._border_width
        else:
            inset = 0

        self._draw_shape(
            inset,
            inset,
            w - inset,
            h - inset,
            self._fill,
            max(0, r - inset)
        )

        self.content.configure(bg=self._fill)
        tk.Frame.configure(self, bg=self._bg)
        tk.Canvas.configure(self.canvas, bg=self._bg)

    def _draw_shape(self, x1, y1, x2, y2, fill, r):
        if x2 <= x1 or y2 <= y1:
            return

        self.canvas.create_polygon(
            round_rect_points(x1, y1, x2, y2, r),
            smooth=True,
            splinesteps=24,
            fill=fill,
            outline=fill
        )

    def configure(self, cnf=None, **kw):
        if 'fg_color' in kw:
            self._fill = kw.pop('fg_color')
        if 'border_color' in kw:
            self._border = kw.pop('border_color')
        if 'border_width' in kw:
            self._border_width = kw.pop('border_width')
        if 'corner_radius' in kw:
            self._radius = kw.pop('corner_radius')
        if 'radius' in kw:
            self._radius = kw.pop('radius')
        if 'pad' in kw:
            self._pad = kw.pop('pad')
        if 'bg' in kw:
            self._bg = kw.pop('bg')
        if 'width' in kw:
            self._explicit_width = kw['width']
        if 'height' in kw:
            self._explicit_height = kw['height']
        if 'auto_size' in kw:
            self._auto_size = kw.pop('auto_size')

        if 'width' in kw:
            tk.Frame.configure(self, width=kw.pop('width'))
        if 'height' in kw:
            tk.Frame.configure(self, height=kw.pop('height'))

        result = tk.Frame.configure(self, cnf or {}, **kw)
        self.after_idle(self._sync_layout)
        return result

    config = configure

    def pack_propagate(self, flag=True):
        tk.Frame.pack_propagate(self, flag)

    def grid_propagate(self, flag=True):
        tk.Frame.grid_propagate(self, flag)


class RoundedCard(RoundedContainer):
    def __init__(self, master, **kwargs):
        kwargs.setdefault('fg_color', '#FFFFFF')
        kwargs.setdefault('border_color', '#1E1E1E')
        kwargs.setdefault('border_width', 2)
        kwargs.setdefault('radius', 28)
        kwargs.setdefault('pad', 14)
        kwargs.setdefault('auto_size', False)
        super().__init__(master, **kwargs)


class OutlineTile(RoundedContainer):
    def __init__(self, master, **kwargs):
        kwargs.setdefault('fg_color', '#FFFFFF')
        kwargs.setdefault('border_color', '#1E1E1E')
        kwargs.setdefault('border_width', 2)
        kwargs.setdefault('radius', 22)
        kwargs.setdefault('pad', 10)
        kwargs.setdefault('auto_size', False)
        super().__init__(master, **kwargs)


class PillButton(tk.Canvas):
    def __init__(
        self,
        master,
        text,
        command=None,
        width=None,
        height=56,
        fg_color='#C46A2A',
        text_color='#FFFFFF',
        disabled_color='#C9A58D',
        font=('Arial', 18, 'bold'),
        radius=None,
        state='normal',
        padx=26,
        **kwargs
    ):
        bg = _parent_bg(master)
        super().__init__(
            master,
            bg=bg,
            highlightthickness=0,
            bd=0,
            cursor='hand2',
            **kwargs
        )

        self._outer_bg = bg
        self._fill = fg_color
        self._text_color = text_color
        self._disabled_color = disabled_color
        self._font = font
        self._command = command
        self._text = text
        self._state = state
        self._fixed_width = width
        self._height = height
        self._padx = padx
        self._radius = radius or height // 2

        self.bind('<Configure>', self._redraw)
        self.bind('<Button-1>', self._on_click)
        self.after_idle(self._sync_size)

    def _measure_text_width(self):
        f = tkfont.Font(font=self._font)
        return f.measure(self._text)

    def _sync_size(self):
        width = self._fixed_width
        if width is None:
            width = self._measure_text_width() + self._padx * 2

        width = max(width, self._height)
        tk.Canvas.configure(self, width=width, height=self._height)
        self.after_idle(self._redraw)

    def _redraw(self, event=None):
        w = max(2, self.winfo_width())
        h = max(2, self.winfo_height())
        r = min(self._radius, w // 2, h // 2)
        fill = self._fill if self._state != 'disabled' else self._disabled_color

        self.delete('all')
        tk.Canvas.configure(self, bg=self._outer_bg)

        self.create_polygon(
            round_rect_points(0, 0, w, h, r),
            smooth=True,
            splinesteps=24,
            fill=fill,
            outline=fill
        )
        self.create_text(
            w / 2,
            h / 2,
            text=self._text,
            fill=self._text_color,
            font=self._font
        )

    def _on_click(self, event=None):
        if self._state == 'disabled':
            return
        if callable(self._command):
            self._command()

    def configure(self, cnf=None, **kw):
        resize_needed = False

        if 'text' in kw:
            self._text = kw.pop('text')
            resize_needed = True
        if 'command' in kw:
            self._command = kw.pop('command')
        if 'state' in kw:
            self._state = kw.pop('state')
            tk.Canvas.configure(
                self,
                cursor='arrow' if self._state == 'disabled' else 'hand2'
            )
        if 'fg_color' in kw:
            self._fill = kw.pop('fg_color')
        if 'text_color' in kw:
            self._text_color = kw.pop('text_color')
        if 'font' in kw:
            self._font = kw.pop('font')
            resize_needed = True
        if 'radius' in kw:
            self._radius = kw.pop('radius')
        if 'width' in kw:
            self._fixed_width = kw.pop('width')
            resize_needed = True
        if 'height' in kw:
            self._height = kw.pop('height')
            resize_needed = True

        result = tk.Canvas.configure(self, cnf or {}, **kw)

        if resize_needed:
            self.after_idle(self._sync_size)
        else:
            self.after_idle(self._redraw)

        return result

    config = configure