"""Shared typography and controls for the desktop client."""

import os


PAGE = "#181622"
SIDEBAR = "#211e2d"
SURFACE = "#282438"
FIELD = "#1d1a2a"
BORDER = "#423b51"
INK = "#f3edf8"
MUTED = "#bdb2cb"
ACCENT = "#f3b899"
ON_ACCENT = "#271d26"
FONT = "Microsoft YaHei UI" if os.name == "nt" else "TkDefaultFont"


def initialize_display() -> None:
    """Set DPI handling before Tk creates its first window."""
    if os.name == "nt":
        import ctypes as ct

        set_awareness = getattr(ct.windll.user32, "SetProcessDpiAwarenessContext", None)
        if set_awareness is not None:
            set_awareness(ct.c_void_p(-4))


def set_window_chrome(window) -> None:
    if os.name == "nt":
        import ctypes as ct
        from ctypes import wintypes as wt

        window.update_idletasks()
        user32 = ct.windll.user32
        user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user32.GetAncestor.restype = wt.HWND
        handle = user32.GetAncestor(window.winfo_id(), 2)  # Tk client to root window.
        color = wt.DWORD(int(PAGE[5:7] + PAGE[3:5] + PAGE[1:3], 16))
        if handle:
            ct.windll.dwmapi.DwmSetWindowAttribute(wt.HWND(handle), 35, ct.byref(color), ct.sizeof(color))
            text = wt.DWORD(0x00F8EDF3)
            ct.windll.dwmapi.DwmSetWindowAttribute(wt.HWND(handle), 36, ct.byref(text), ct.sizeof(text))


def configure_styles(root) -> None:
    from tkinter import ttk

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("SAR.TCombobox", padding=7, font=(FONT, 10), foreground=INK,
                    background=SURFACE, fieldbackground=FIELD, arrowcolor=MUTED,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.map("SAR.TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", INK)], selectbackground=[("readonly", FIELD)],
              selectforeground=[("readonly", INK)])
    style.configure("SAR.TEntry", padding=8, foreground=INK, fieldbackground=FIELD,
                    insertcolor=ACCENT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    style.map("SAR.TEntry", fieldbackground=[("readonly", SURFACE), ("disabled", SIDEBAR)],
              bordercolor=[("focus", ACCENT)], foreground=[("disabled", MUTED)])
    style.configure("SAR.TCheckbutton", background=SURFACE, foreground=MUTED, padding=6,
                    indicatorbackground=FIELD, indicatorforeground=ON_ACCENT)
    style.map("SAR.TCheckbutton", background=[("active", SURFACE)],
              indicatorbackground=[("selected", ACCENT)])
    style.configure("SAR.Vertical.TScrollbar", arrowsize=0, width=8, background=BORDER,
                    troughcolor=PAGE, bordercolor=PAGE, lightcolor=PAGE, darkcolor=PAGE)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", BORDER)
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))


def label(parent, text="", *, bg=PAGE, color=INK, size=10, bold=False, **kwargs):
    import tkinter as tk

    return tk.Label(parent, text=text, bg=bg, fg=color, anchor="w",
                    font=(FONT, size, "bold" if bold else "normal"), **kwargs)


def button(parent, text, command, *, bg=SURFACE, fg=INK, anchor="center"):
    import tkinter as tk

    hover = "#ffd4bf" if bg == ACCENT else BORDER
    widget = tk.Button(parent, text=text, command=command, bg=bg, fg=fg, relief="flat",
                       activebackground=hover, activeforeground=fg, bd=0, padx=12, pady=7,
                       highlightthickness=1, highlightbackground=bg, highlightcolor=ACCENT,
                       cursor="hand2", anchor=anchor, font=(FONT, 10))
    widget.bind("<Enter>", lambda _: widget.configure(bg=hover) if widget["state"] == "normal" else None)
    widget.bind("<Leave>", lambda _: widget.configure(bg=bg))
    return widget
