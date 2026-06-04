
import tkinter as tk
from tkinter import font as tkfont


def _parent_bg(master, default='#F5F2DE'):
    try:
        return master.cget('bg')
    except Exception:
        return default


def _normalize_color(color, master=None, default='#F5F2DE'):
    if color in (None, '', 'transparent'):
        return _parent_bg(master, default)
    if isinstance(color, tuple) and color:
        return color[0]
    return color

class CTkImage:
    def __init__(self, light_image=None, dark_image=None, size=None):
        self.light_image = light_image
        self.dark_image = dark_image if dark_image is not None else light_image
        self.size = size
        self._cache = {}

    def _select_source(self):
        return self.light_image or self.dark_image

    def get_tk_image(self):
        src = self._select_source()
        if src is None:
            return None

        cache_key = (id(src), self.size)

        if cache_key in self._cache:
            return self._cache[cache_key]

        if ImageTk is None:
            return src

        try:
            if Image is not None and isinstance(src, Image.Image):
                prepared = _resize_pil_image(src, self.size)
                tk_img = ImageTk.PhotoImage(prepared)
            else:
                tk_img = src
        except Exception:
            tk_img = src

        self._cache[cache_key] = tk_img
        return tk_img

    def configure(self, light_image=None, dark_image=None, size=None):
        if light_image is not None:
            self.light_image = light_image
        if dark_image is not None:
            self.dark_image = dark_image
        if size is not None:
            self.size = size
        self._cache.clear()

class CTkFont(tkfont.Font):
    def __init__(self, family='Arial', size=12, weight='normal', **kwargs):
        super().__init__(family=family, size=size, weight=weight, **kwargs)


class CTk(tk.Tk):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.configure(bg='#F5F2DE')

    def configure(self, cnf=None, **kw):
        if 'fg_color' in kw:
            kw['bg'] = kw.pop('fg_color')
        return super().configure(cnf or {}, **kw)

    config = configure


class _BaseMixin:
    def _apply_common(self, master=None, kwargs=None):
        kwargs = dict(kwargs or {})
        if 'fg_color' in kwargs:
            kwargs['bg'] = _normalize_color(kwargs.pop('fg_color'), master)
        if 'text_color' in kwargs:
            kwargs['fg'] = kwargs.pop('text_color')
        if 'hover_color' in kwargs:
            kwargs.pop('hover_color')
        if 'corner_radius' in kwargs:
            kwargs.pop('corner_radius')
        if 'border_spacing' in kwargs:
            kwargs.pop('border_spacing')
        if 'textvariable' in kwargs:
            pass
        if 'border_color' in kwargs:
            bc = kwargs.pop('border_color')
            kwargs['highlightbackground'] = bc
            kwargs['highlightcolor'] = bc
        if 'border_width' in kwargs:
            kwargs['highlightthickness'] = kwargs.pop('border_width')
        if 'font' in kwargs and isinstance(kwargs['font'], tuple):
            kwargs['font'] = kwargs['font']
        return kwargs

    def configure(self, cnf=None, **kw):
        kw = self._apply_common(self.master, kw)
        return super().configure(cnf or {}, **kw)

    config = configure


class CTkFrame(_BaseMixin, tk.Frame):
    def __init__(self, master=None, **kwargs):
        kwargs = self._apply_common(master, kwargs)
        kwargs.setdefault('bd', 0)
        super().__init__(master, **kwargs)


class CTkLabel(_BaseMixin, tk.Label):
    def __init__(self, master=None, **kwargs):
        kwargs = self._apply_common(master, kwargs)
        kwargs.setdefault('bg', _parent_bg(master))
        super().__init__(master, **kwargs)


class CTkButton(_BaseMixin, tk.Button):
    def __init__(self, master=None, **kwargs):
        command = kwargs.get('command')
        kwargs = self._apply_common(master, kwargs)
        bg = kwargs.get('bg', '#C46A2A')
        fg = kwargs.get('fg', '#FFFFFF')
        kwargs.setdefault('activebackground', bg)
        kwargs.setdefault('activeforeground', fg)
        kwargs.setdefault('relief', 'flat')
        kwargs.setdefault('cursor', 'hand2' if command else 'arrow')
        kwargs.setdefault('bd', 0)
        kwargs.setdefault('disabledforeground', '#DDDDDD')
        super().__init__(master, **kwargs)


def set_appearance_mode(mode):
    return None
