"""Desktop editor for the shared named-model catalog."""

import os

from simple_ar.integrations.model_settings import read_settings, save_profile, settings_path


def open_model_settings(parent, on_saved) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    path = settings_path()
    try:
        original, catalog = read_settings(path)
    except (OSError, ValueError) as exc:
        messagebox.showerror("模型设置", str(exc), parent=parent)
        return
    window = tk.Toplevel(parent)
    window.title("设置 · 模型连接")
    window.transient(parent)
    window.minsize(620, 350)
    panel = ttk.Frame(window, padding=18)
    panel.pack(fill="both", expand=True)
    panel.columnconfigure(1, weight=1)
    ttk.Label(panel, text="模型连接", font=("Segoe UI", 15, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(panel, text=str(path), wraplength=570).grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(4, 14))
    profiles = list(catalog.profiles) if catalog else []
    chooser = ttk.Combobox(panel, state="readonly", values=["新建连接", *profiles])
    chooser.grid(row=2, column=1, sticky="ew")
    ttk.Label(panel, text="连接").grid(row=2, column=0, sticky="w", padx=(0, 18))
    fields = {}
    for row, (key, label) in enumerate((("name", "连接名称"), ("endpoint", "Base URL"),
            ("model", "模型 ID"), ("reference", "凭据环境变量"), ("key", "API Key")), start=3):
        ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(panel, show="•" if key == "key" else "")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        fields[key] = entry
    use_default = tk.BooleanVar()
    status = tk.StringVar()
    ttk.Checkbutton(panel, text="用作新任务的默认文本连接", variable=use_default).grid(
        row=8, column=1, sticky="w")
    ttk.Label(panel, textvariable=status, wraplength=465).grid(row=9, column=1, sticky="w", pady=8)

    def select(_event=None):
        index = chooser.current()
        name = profiles[index - 1] if index > 0 else ""
        profile = catalog.profiles[name] if name else None
        values = dict(name=name, endpoint=profile.base_url if profile else "",
                      model=profile.model if profile else "", reference=profile.api_key_env if profile else "")
        for field, entry in fields.items():
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.insert(0, values.get(field, ""))
        if profile:
            fields["name"].configure(state="readonly")
        if os.name != "nt":
            fields["key"].configure(state="disabled")
        use_default.set(bool(profile and (catalog.routes.get("text") or catalog.routes.get("default")) == name))
        if profile and os.environ.get(profile.api_key_env, "").strip():
            status.set("当前凭据由环境提供。")
        elif profile and profile.has_credential():
            status.set("已配置凭据。留空则沿用已有 Key。")
        elif os.name == "nt":
            status.set("输入 API Key 可保存到 Windows 凭据管理器，或填写现有凭据环境变量。")
        else:
            status.set("请在环境中设置凭据变量。")

    def save():
        values = {name: widget.get() for name, widget in fields.items()}
        if chooser.current() == 0 and values["name"].strip() in profiles:
            status.set("此名称已存在，请选择该连接进行编辑。")
            return
        try:
            save_profile(path, original, **values, default=use_default.get())
        except (OSError, ValueError, RuntimeError) as exc:
            status.set(str(exc))
            return
        window.destroy()
        on_saved()

    ttk.Button(panel, text="保存连接", command=save).grid(row=10, column=1, sticky="e")
    chooser.bind("<<ComboboxSelected>>", select)
    chooser.current(0)
    select()
