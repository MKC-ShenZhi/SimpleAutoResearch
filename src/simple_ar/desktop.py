"""Conversation workspace for local SimpleAutoResearch tasks."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from uuid import uuid4

from simple_ar.ui import Workspace, public_data, public_text
from simple_ar.desktop_theme import (
    ACCENT as accent, BORDER as border, FIELD as field, FONT, INK as ink, MUTED as muted,
    ON_ACCENT, PAGE as page, SIDEBAR as sidebar, SURFACE as surface,
    button, configure_styles, initialize_display, label, set_window_chrome,
)


def run_desktop(root, workspace: Workspace) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root.title("SimpleAutoResearch · 研究工作台")
    width = min(1260, root.winfo_screenwidth() - 70)
    height = min(810, root.winfo_screenheight() - 100)
    root.geometry(f"{width}x{height}")
    root.minsize(800, 520)
    root.configure(bg=page)
    configure_styles(root)
    set_window_chrome(root)

    pool = ThreadPoolExecutor(max_workers=1)
    jobs = {}
    active = None
    task_view = {}
    rows = []
    artifacts = []
    materials = []
    connections = []
    drafts = {}
    preferred = None
    delivery_stamp = None

    def request(key, load, show):
        previous = jobs.get(key)
        if previous is not None:
            if key == "action":
                return
            previous.cancel()
        future = jobs[key] = pool.submit(load)

        def poll():
            if jobs.get(key) is not future:
                return
            if not future.done():
                root.after(50, poll)
                return
            jobs.pop(key)
            try:
                value = future.result()
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                notice.set(public_text(str(exc)))
            else:
                show(value)

        root.after(50, poll)

    def close():
        with workspace.lock:
            running = any(thread.is_alive() for thread, _ in workspace.workers.values())
        if running or "action" in jobs:
            messagebox.showinfo("任务正在运行", "请先暂停并等待当前操作完成，再关闭工作台。", parent=root)
            return
        jobs.clear()
        pool.shutdown(wait=False, cancel_futures=True)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)

    def list_view(parent, color, height=12):
        return tk.Listbox(parent, bg=color, fg=ink, selectbackground=border, selectforeground=ink,
                          relief="flat", bd=0, selectborderwidth=5, highlightthickness=0,
                          activestyle="none", exportselection=False, font=(FONT, 10), height=height)

    def text_view(parent, *, color=page, height=12, editable=False):
        text = tk.Text(parent, bg=color, fg=ink, insertbackground=accent, wrap="word",
                       relief="flat", bd=0, highlightthickness=0, padx=12, pady=8,
                       font=(FONT, 11), height=height, undo=editable)
        if not editable:
            text.configure(state="disabled")
        return text

    nav = tk.Frame(root, bg=sidebar, width=234, padx=14, pady=20)
    nav.pack(side="left", fill="y")
    nav.pack_propagate(False)
    label(nav, "SimpleAutoResearch", bg=sidebar, size=12, bold=True).pack(fill="x")
    label(nav, str(workspace.project.name or workspace.project), bg=sidebar,
          color=muted).pack(fill="x", pady=(3, 20))
    button(nav, "＋  新建研究", lambda: select_task(None), fg=accent, anchor="w").pack(fill="x")
    nav_header = tk.Frame(nav, bg=sidebar)
    nav_header.pack(fill="x", pady=(18, 5))
    label(nav_header, "研究会话", bg=sidebar, color=muted, bold=True).pack(side="left")
    button(nav_header, "刷新", lambda: refresh_list(), bg=sidebar, fg=muted).pack(side="right")
    tasks = list_view(nav, sidebar)
    tasks.pack(fill="both", expand=True)
    tk.Frame(nav, height=1, bg=border).pack(fill="x", pady=(12, 7))
    button(nav, "设置 · 模型连接", lambda: open_settings(), bg=sidebar, anchor="w").pack(fill="x")

    work = tk.Frame(root, bg=page, padx=24, pady=15)
    work.pack(side="left", fill="both", expand=True)
    top = tk.Frame(work, bg=page)
    top.pack(fill="x")
    title = tk.StringVar(value="新建研究")
    status = tk.StringVar()
    label(top, size=15, bold=True, textvariable=title).pack(side="left", fill="x", expand=True)
    output_button = button(top, "成果", lambda: toggle_outputs())
    output_button.pack(side="right")
    pause = button(top, "暂停", lambda: action("stop"))
    resume = button(top, "继续", lambda: action("resume"))
    approve = button(top, "查看待审批操作", lambda: approve_current(), fg=accent)
    status_line = label(work, color=muted, textvariable=status)
    status_line.pack(fill="x", pady=(3, 8))
    tk.Frame(work, height=1, bg=border).pack(fill="x", pady=(0, 10))

    center = tk.Frame(work, bg=page)
    center.pack(fill="both", expand=True)
    evidence = tk.Frame(center, bg=sidebar, width=310, padx=12, pady=12)
    evidence.pack_propagate(False)
    label(evidence, "交付成果", bg=sidebar, size=12, bold=True).pack(fill="x")
    artifact_status = tk.StringVar()
    label(evidence, bg=sidebar, color=muted, textvariable=artifact_status,
          wraplength=278, justify="left").pack(fill="x", pady=6)
    files = list_view(evidence, sidebar, height=6)
    files.pack(fill="x")
    preview = text_view(evidence, color=surface)
    preview.pack(fill="both", expand=True, pady=(8, 0))
    thread_panel = tk.Frame(center, bg=page)
    thread_panel.pack(fill="both", expand=True)
    thread = text_view(thread_panel)
    scroll = ttk.Scrollbar(thread_panel, command=thread.yview, style="SAR.Vertical.TScrollbar")
    thread.configure(yscrollcommand=scroll.set)
    thread.pack(side="left", fill="both", expand=True)
    thread.tag_configure("role", foreground=accent, font=(FONT, 10, "bold"), spacing1=15, spacing3=4)
    thread.tag_configure("user", background=surface, lmargin1=12, lmargin2=12, rmargin=10,
                         spacing1=8, spacing2=2, spacing3=8)
    thread.tag_configure("muted", foreground=muted, spacing1=3, spacing3=3)
    welcome = tk.Frame(thread_panel, bg=page)
    label(welcome, "开始一项研究", size=25, bold=True, anchor="center").pack(fill="x")
    label(welcome, "写下研究问题，或添加论文、数据与代码。", color=muted,
          size=11, anchor="center").pack(fill="x", pady=(10, 0))

    composer_lane = tk.Frame(work, bg=page)
    composer_lane.pack(fill="x", pady=(10, 0))
    composer = tk.Frame(composer_lane, bg=surface, padx=10, pady=8,
                        highlightthickness=1, highlightbackground=border)
    composer.pack(fill="x")

    def fit_composer(event):
        composer.pack_configure(padx=max(0, (event.width - 770) // 2))

    composer_lane.bind("<Configure>", fit_composer)
    composer_title = tk.StringVar(value="研究目标")
    label(composer, bg=surface, color=muted, textvariable=composer_title).pack(fill="x", padx=12)
    prompt = text_view(composer, color=surface, height=2, editable=True)
    prompt.pack(fill="x")
    controls = tk.Frame(composer, bg=surface)
    controls.pack(fill="x")
    attach = button(controls, "＋ 材料", lambda: attach_files())
    model = ttk.Combobox(controls, state="readonly", width=20, style="SAR.TCombobox")
    execution = tk.BooleanVar()
    network = tk.BooleanVar()

    def option(text, value):
        return ttk.Checkbutton(controls, text=text, variable=value, style="SAR.TCheckbutton")

    permissions = [option("执行", execution), option("联网", network)]
    send_button = button(controls, "开始", lambda: send(), bg=accent, fg=ON_ACCENT)
    send_button.pack(side="right")
    model.pack(side="right", padx=(5, 10))
    material_label = tk.StringVar()
    label(composer, bg=surface, color=muted, textvariable=material_label).pack(fill="x", padx=12)
    notice = tk.StringVar()
    label(work, color=muted, textvariable=notice, wraplength=800, justify="left").pack(fill="x", pady=(8, 0))

    def render_text(widget, pieces, *, tail=False):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in pieces:
            widget.insert("end", text, tag)
        widget.configure(state="disabled")
        if tail:
            widget.see("end")

    outputs = {}

    def render_thread():
        if active is None:
            render_text(thread, [])
            scroll.pack_forget()
            welcome.place(relx=.5, rely=.43, anchor="center")
            return
        welcome.place_forget()
        pieces = []
        for message in task_view.get("requests", []):
            pieces.extend([("你\n", "role"), (message + "\n", "user")])
        if task_view.get("activity"):
            pieces.append(("工作记录\n", "role"))
            for row in reversed(task_view["activity"]):
                detail = row.get("summary") or row.get("error") or row.get("arguments", {}).get("path") or ""
                pieces.append((f"{row.get('tool', '操作')}   ·   {row.get('status', '')}"
                               + (f"   {str(detail)[:160]}" if detail else "") + "\n", "muted"))
        if outputs.get("summary"):
            heading = "交付摘要" if outputs["current"] else "上次交付"
            pieces.extend([(heading + "\n", "role"), (outputs["summary"] + "\n", "")])
        at_bottom = thread.yview()[1] > .94
        position = thread.yview()[0]
        render_text(thread, pieces, tail=at_bottom)
        if thread.yview() != (0.0, 1.0):
            scroll.pack(side="right", fill="y", before=thread)
        else:
            scroll.pack_forget()
        if not at_bottom:
            thread.yview_moveto(position)

    def show_task(view):
        if task_view == view:
            return
        task_view.clear()
        task_view.update(view)
        model.set((view.get("model") or "").removeprefix("profile:"))
        manifest = view["manifest"]
        title.set(manifest.get("topic", "研究会话")[:58])
        reason = (manifest.get("status_reason") or "").partition("; token=")[0]
        status.set(manifest["status"] + ("  ·  " + reason[:150] if reason else ""))
        for widget in (pause, resume, approve):
            widget.pack_forget()
        if view.get("worker_live"):
            pause.pack(side="right", padx=(0, 6))
            pause.configure(state="disabled" if view.get("stop_requested") else "normal")
        elif view.get("approval"):
            approve.pack(side="right", padx=(0, 6))
        elif manifest["status"] not in {"completed"}:
            resume.pack(side="right", padx=(0, 6))
        render_thread()

    def show_outputs(delivery):
        if outputs == delivery:
            return
        outputs.clear()
        outputs.update(delivery)
        artifacts[:] = delivery["files"]
        files.delete(0, "end")
        for item in artifacts:
            files.insert("end", item["path"])
        artifact_status.set("当前交付" if delivery["current"] else "历史交付")
        versions = [f"第 {row['number']} 版 · {row['status']} · {row['file_count']} 项"
                    for row in delivery["versions"]]
        render_text(preview, [("\n".join(versions + delivery.get("unresolved", [])), "muted")])
        render_thread()

    def refresh_task():
        name = active
        if name is None:
            return

        def loaded(view):
            nonlocal delivery_stamp
            if active != name:
                return
            show_task(view)
            manifest = view["manifest"]
            stamp = (name, manifest.get("updated_at"), manifest["status"])
            if ("outputs" in jobs or delivery_stamp == stamp
                    or not (evidence.winfo_manager() or manifest["status"] in {"completed", "partial"})):
                return

            def accept(result):
                nonlocal delivery_stamp
                if active == name:
                    delivery_stamp = stamp
                    show_outputs(result)
            request("outputs", lambda: public_data(workspace.delivery(name)), accept)
        request("snapshot", lambda: public_data(workspace.snapshot(name)), loaded)

    def toggle_outputs():
        if evidence.winfo_manager():
            evidence.pack_forget()
        elif active is not None:
            evidence.pack(side="right", fill="y", padx=(14, 0), before=thread_panel)
            refresh_task()

    def select_task(name):
        nonlocal active, delivery_stamp
        drafts[active] = prompt.get("1.0", "end-1c")
        active = name
        delivery_stamp = None
        outputs.clear()
        task_view.clear()
        files.delete(0, "end")
        artifacts.clear()
        artifact_status.set("")
        render_text(preview, [])
        prompt.delete("1.0", "end")
        prompt.insert("1.0", drafts.get(name, ""))
        material_label.set("")
        for widget in (pause, resume, approve, attach, *permissions):
            widget.pack_forget()
        if name is None:
            tasks.selection_clear(0, "end")
            evidence.pack_forget()
            title.set("新建研究")
            status.set("")
            output_button.configure(state="disabled")
            composer_title.set("研究目标")
            send_button.configure(text="开始")
            attach.pack(side="left")
            select_default_model()
            for widget in permissions:
                widget.pack(side="left")
            material_label.set("、".join(path.name for path in materials))
        else:
            output_button.configure(state="normal")
            title.set("正在读取会话")
            model.set("")
            composer_title.set("给当前会话补充要求")
            send_button.configure(text="发送")
            refresh_task()
        render_thread()

    def refresh_list():
        def show(new_rows):
            nonlocal preferred
            rows[:] = new_rows
            tasks.delete(0, "end")
            for row in rows:
                tasks.insert("end", public_text(row["topic"] or row["task"]))
            target = preferred or active
            for index, row in enumerate(rows):
                if row["task"] == target:
                    tasks.selection_set(index)
                    if preferred:
                        preferred = None
                        select_task(target)
                    break
        request("listing", workspace.listing, show)

    def task_selected(_event=None):
        selection = tasks.curselection()
        if selection and active != rows[selection[0]]["task"]:
            select_task(rows[selection[0]]["task"])

    def attach_files():
        selected = filedialog.askopenfilenames(parent=root, initialdir=workspace.project)
        if selected:
            materials[:] = list(map(Path, selected))
            material_label.set("、".join(path.name for path in materials))

    def send():
        message = prompt.get("1.0", "end-1c").strip()
        if not message or "action" in jobs:
            return
        name = active
        if name is not None:
            action("feedback", feedback=message, submission_id=uuid4().hex)
            return
        selection = model.current()
        if selection < 0:
            notice.set("请选择已配置的模型连接。")
            return
        item = connections[selection]
        payload = dict(goal=message, model=item["selector"], binding_id=item["binding_id"],
                       materials=list(map(str, materials)), execution=execution.get(), network=network.get())

        def confirm(config):
            limits = config["limits"]
            recipient = config["recipients"].get("task", {})
            sources = "\n".join(map(str, config["sources"].values()))
            detail = (f"模型  {config['model']}\n连接  {recipient.get('base_url', '')}\n"
                      f"执行  {'允许提出申请' if config['access']['allow_execution'] else '关闭'}\n"
                      f"联网  {'开启' if config['access']['allow_network'] else '关闭'}\n"
                      f"调用上限  {limits['llm_requests']}\nToken 上限  {limits['total_tokens']}\n"
                      f"材料\n{sources or '无'}")
            if not messagebox.askyesno("确认研究设置", public_text(detail), parent=root):
                return

            def start():
                created = workspace.mutate({**payload, "op": "create", "confirmed": True,
                                            "preview_id": config["preview_id"]})
                try:
                    result = workspace.mutate({"op": "resume", "task": created["task"]})
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                    result = {"message": str(exc)}
                return created["task"], result

            def opened(result):
                nonlocal preferred
                preferred, data = result
                if active is None:
                    prompt.delete("1.0", "end")
                    materials.clear()
                notice.set(public_text(data["message"]))
                refresh_list()

            request("action", start, opened)

        request("action", lambda: workspace.preview_creation(payload), confirm)

    def action(op, **fields):
        name = active
        if name is None or "action" in jobs:
            return

        def done(result):
            notice.set(public_text(result.get("message", "")))
            if active == name:
                if op == "feedback":
                    prompt.delete("1.0", "end")
                refresh_task()
        request("action", lambda: workspace.mutate(dict(op=op, task=name, **fields)), done)

    def approve_current():
        proposal = task_view.get("approval")
        if proposal is None:
            return
        operation = proposal["call"]["function"]
        receipt = proposal.get("execution") or operation.get("arguments")
        summary = json.dumps(receipt, ensure_ascii=False, indent=2) if isinstance(receipt, dict) else str(receipt)
        summary = public_text(summary)
        if messagebox.askyesno("批准本次操作", f"{operation['name']}\n\n{summary}", parent=root):
            action("approve", token=proposal["token"])

    def select_file(_event=None):
        selection = files.curselection()
        if active is None or not selection:
            return
        name, index = active, selection[0]
        row = artifacts[index]
        extension = Path(row["path"]).suffix.lower()

        def load():
            if extension == ".png":
                _, data = workspace.download(name, row["mount"], row["path"])
                return data
            path = workspace.file(name, row["mount"], row["path"])
            with path.open("rb") as stream:
                content = stream.read(65537)
            return public_text(content[:65536].decode("utf-8", errors="replace")) + (
                "\n\n仅显示文件开头 64 KiB。" if len(content) > 65536 else "")

        def show(value):
            if active != name or files.curselection() != (index,) or row not in artifacts:
                return
            if isinstance(value, str):
                render_text(preview, [(value, "")])
                return
            try:
                image = tk.PhotoImage(data=base64.b64encode(value))
            except tk.TclError:
                render_text(preview, [("这张图片无法预览。", "muted")])
                return
            factor = max(1, (image.width() + 255) // 256, (image.height() + 399) // 400)
            if factor > 1:
                image = image.subsample(factor)
            preview.image = image
            render_text(preview, [])
            preview.configure(state="normal")
            preview.image_create("end", image=image)
            preview.configure(state="disabled")

        if extension in {".md", ".txt", ".csv", ".json", ".log", ".py", ".patch", ".tex", ".png"}:
            request("preview", load, show)
        else:
            render_text(preview, [(f"{row['path']}\n此格式暂无内嵌预览。", "muted")])

    def select_default_model():
        if connections:
            index = next((i for i, row in enumerate(connections) if row["default"]), 0)
            model.current(index)
        else:
            model.set("配置模型")

    def show_connections(profiles):
        selected = model.get()
        connections[:] = profiles
        model.configure(values=[row["selector"].removeprefix("profile:") for row in profiles])
        if active is None and selected not in model.cget("values"):
            select_default_model()

    def open_settings():
        from simple_ar.desktop_settings import open_model_settings
        open_model_settings(root, lambda: request("profiles", workspace.profiles, show_connections))

    def select_connection(_event=None):
        index = model.current()
        if active is None or index < 0:
            return
        profile = connections[index]
        saved = task_view.get("model") or ""
        model.set(saved.removeprefix("profile:"))
        if not saved or task_view.get("worker_live") or task_view.get("pending") or "action" in jobs:
            notice.set("请等当前会话到达可切换的操作边界。")
            return
        recipient = profile["recipients"].get("task") or {}
        if (profile["selector"] == saved
                and recipient == task_view.get("saved_connections", {}).get("task")):
            return
        detail = (f"后续工作将使用 {profile['selector']}\n"
                  f"{recipient.get('model', '')}\n{recipient.get('base_url', '')}")
        if messagebox.askyesno("切换当前会话的模型", public_text(detail), parent=root):
            action("switch", model=profile["selector"], binding_id=profile["binding_id"],
                   confirmed=True, switch_id=uuid4().hex, reason="用户从桌面切换会话模型")

    def tick():
        if "snapshot" not in jobs:
            refresh_task()
        root.after(1800, tick)

    def keyboard_send(_event):
        send()
        return "break"

    tasks.bind("<<ListboxSelect>>", task_selected)
    files.bind("<<ListboxSelect>>", select_file)
    model.bind("<<ComboboxSelected>>", select_connection)
    prompt.bind("<Control-Return>", keyboard_send)
    select_task(None)
    refresh_list()
    request("profiles", workspace.profiles, show_connections)
    root.after(1800, tick)
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True, help="Folder containing saved tasks")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--read-root", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        import tkinter as tk
    except ImportError:
        parser.exit(1, "Desktop requires a Python installation with Tk support.\n")
    if not args.task_root.is_dir():
        parser.error("--task-root must be an existing task folder")
    workspace = Workspace(args.project_root, args.read_root, args.task_root)
    try:
        initialize_display()
        root = tk.Tk()
    except tk.TclError as exc:
        parser.exit(1, f"Cannot open the desktop window: {exc}\n")
    run_desktop(root, workspace)


if __name__ == "__main__":
    main()
