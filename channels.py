"""
Channel Manager - add, edit and delete YouTube channels in the FlexGet config.

Tabs:
  Add channel - paste a channel link, look up its ID, confirm, pick a list, save
  Channels    - every channel in the FlexGet config, A-Z, with edit / delete
  Waiting     - URLs in the lists that haven't downloaded yet; tick and delete

The FlexGet file is edited as text, so formatting, order and comments are kept.
A timestamped backup is saved before every change (last 20 kept).

Run:  pythonw channels.py      (or from the tray icon: "Manage channels")
"""
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    import yaml
    from flexget_channels import *          # noqa: F401,F403  shared logic
    from flexget_channels import (Channel, FlexgetConfig, CHANNEL_ID_RE, UA,
                                  load_settings, lookup_channel, suggest_task_name,
                                  validate, touch_list, HERE,
                                  read_waiting, remove_waiting)
except ImportError as _e:
    _msg = (f"Missing library: {_e.name}\n\n" +
            ("tkinter comes with Python. Re-run the Python installer, choose Modify, "
             "and tick 'tcl/tk and IDLE'." if _e.name in ("tkinter", "_tkinter")
             else "Run:  pip install pyyaml pillow"))
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, _msg, "Channel Manager error", 0x10)
    print(_msg, file=sys.stderr)
    sys.exit(1)


# ======================================================================
#  GUI
# ======================================================================
# ======================================================================
#  AER theme - soft neumorphic palette (matches the web interface)
# ======================================================================
BG, SHADE, LIGHT = "#E8EEF2", "#c3ccd4", "#ffffff"
ACCENT, INK, INK_SOFT, WARN = "#4db8e8", "#4a5a66", "#7d8b96", "#e0a24d"


def apply_theme(root):
    import tkinter.font as tkfont
    family = "Segoe UI" if os.name == "nt" else "TkDefaultFont"
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=family, size=10)
        except tk.TclError:
            pass
    root.configure(bg=BG)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=INK, fieldbackground=BG,
                bordercolor=SHADE, lightcolor=LIGHT, darkcolor=SHADE,
                troughcolor=BG, focuscolor=ACCENT, selectbackground=ACCENT,
                selectforeground="#ffffff", font=(family, 10))
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG, foreground=INK)
    s.configure("Muted.TLabel", foreground=INK_SOFT)
    s.configure("Warn.TLabel", foreground=WARN)
    s.configure("Title.TLabel", font=(family, 13, "bold"))
    s.configure("TLabelframe", background=BG, bordercolor=SHADE, lightcolor=LIGHT,
                darkcolor=SHADE, relief="groove", borderwidth=2)
    s.configure("TLabelframe.Label", background=BG, foreground=INK_SOFT,
                font=(family, 9, "bold"))
    # raised soft buttons
    s.configure("TButton", background=BG, foreground=INK, padding=(14, 7),
                bordercolor=SHADE, lightcolor=LIGHT, darkcolor=SHADE, relief="raised",
                borderwidth=2, font=(family, 10, "bold"))
    s.map("TButton",
          background=[("pressed", "#dde4e9"), ("active", "#eef3f6"), ("disabled", BG)],
          foreground=[("disabled", SHADE), ("active", ACCENT)],
          relief=[("pressed", "sunken")],
          lightcolor=[("pressed", SHADE)], darkcolor=[("pressed", LIGHT)])
    s.configure("Accent.TButton", foreground=ACCENT)
    # inset fields
    for w in ("TEntry", "TCombobox"):
        s.configure(w, fieldbackground="#eef3f6", background=BG, foreground=INK,
                    bordercolor=SHADE, lightcolor=SHADE, darkcolor=LIGHT, padding=6,
                    arrowcolor=INK_SOFT, insertcolor=INK)
        s.map(w, bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              fieldbackground=[("readonly", "#eef3f6")])
    root.option_add("*TCombobox*Listbox.background", BG)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    s.configure("TCheckbutton", background=BG, foreground=INK, indicatorbackground="#eef3f6",
                indicatorforeground=ACCENT, focuscolor=BG)
    s.map("TCheckbutton", background=[("active", BG)],
          indicatorbackground=[("selected", ACCENT)], indicatorforeground=[("selected", "#ffffff")])
    # notebook tabs as soft pills
    s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
    s.configure("TNotebook.Tab", background=BG, foreground=INK_SOFT, padding=(18, 8),
                borderwidth=0, font=(family, 10, "bold"))
    s.map("TNotebook.Tab", background=[("selected", "#dde4e9")],
          foreground=[("selected", ACCENT), ("active", INK)])
    # table
    s.configure("Treeview", background="#eef3f6", fieldbackground="#eef3f6", foreground=INK,
                rowheight=28, bordercolor=SHADE, lightcolor=SHADE, darkcolor=LIGHT)
    s.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#ffffff")])
    s.configure("Treeview.Heading", background=BG, foreground=INK_SOFT, relief="flat",
                padding=(8, 6), font=(family, 9, "bold"))
    s.map("Treeview.Heading", background=[("active", "#dde4e9")])
    s.configure("Vertical.TScrollbar", background=BG, troughcolor="#eef3f6",
                bordercolor=BG, arrowcolor=INK_SOFT, lightcolor=LIGHT, darkcolor=SHADE)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ytdl - Channel Manager")
        self.geometry("820x560")
        self.minsize(640, 440)
        apply_theme(self)

        try:
            flexget_path, lists_dir = load_settings()
        except Exception as e:
            messagebox.showerror("Config error", f"Couldn't read config.yaml:\n{e}")
            self.destroy()
            return
        try:
            self.cfg = FlexgetConfig(flexget_path, lists_dir)
        except FileNotFoundError:
            messagebox.showerror(
                "FlexGet config not found",
                f"Couldn't find:\n{flexget_path}\n\n"
                "Set paths: flexget_config in config.yaml to your FlexGet config.yml.")
            self.destroy()
            return

        self.found = None
        self.avatar_img = None

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.add_tab = ttk.Frame(nb, padding=12)
        self.list_tab = ttk.Frame(nb, padding=12)
        self.wait_tab = ttk.Frame(nb, padding=12)
        nb.add(self.add_tab, text="  Add channel  ")
        nb.add(self.list_tab, text="  Channels  ")
        nb.add(self.wait_tab, text="  Waiting  ")
        self.build_add_tab()
        self.build_list_tab()
        self.build_wait_tab()
        nb.bind("<<NotebookTabChanged>>",
                lambda _: self.refresh_waiting() if nb.select() == str(self.wait_tab) else None)

        self.status = tk.StringVar(value=f"FlexGet config: {self.cfg.path}")
        ttk.Label(self, textvariable=self.status, anchor="w",
                  style="Muted.TLabel").pack(fill="x", padx=10, pady=(0, 6))

    # ------------------------------------------------------------------
    #  Add tab
    # ------------------------------------------------------------------
    def build_add_tab(self):
        f = self.add_tab
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="1.  Paste a YouTube channel link").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Channel page, @handle, a video, or a UC... ID",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")
        self.url_var = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.url_var)
        e.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        e.bind("<Return>", lambda _: self.do_lookup())
        self.lookup_btn = ttk.Button(f, text="Look up", style="Accent.TButton", command=self.do_lookup)
        self.lookup_btn.grid(row=2, column=2, sticky="e", padx=(6, 0), pady=(4, 10))

        # --- result / confirm ---
        self.result = ttk.LabelFrame(f, text="2.  Is this the right channel?", padding=10)
        self.result.grid(row=3, column=0, columnspan=3, sticky="nsew")
        self.result.columnconfigure(1, weight=1)
        self.avatar_lbl = ttk.Label(self.result)
        self.avatar_lbl.grid(row=0, column=0, rowspan=4, sticky="nw", padx=(0, 12))
        self.r_name = ttk.Label(self.result, text="(nothing looked up yet)",
                                style="Title.TLabel")
        self.r_name.grid(row=0, column=1, sticky="w")
        self.r_id = ttk.Label(self.result, text="", style="Muted.TLabel")
        self.r_id.grid(row=1, column=1, sticky="w")
        self.r_warn = ttk.Label(self.result, text="", style="Warn.TLabel")
        self.r_warn.grid(row=2, column=1, sticky="w")
        self.r_videos = ttk.Label(self.result, text="", justify="left", style="Muted.TLabel")
        self.r_videos.grid(row=3, column=1, sticky="w", pady=(6, 0))
        self.open_btn = ttk.Button(self.result, text="Open on YouTube",
                                   command=self.open_found, state="disabled")
        self.open_btn.grid(row=0, column=2, sticky="ne")

        # --- save options ---
        opts = ttk.LabelFrame(f, text="3.  Choose a list and save", padding=10)
        opts.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="List:").grid(row=0, column=0, sticky="w")
        self.list_var = tk.StringVar()
        self.list_combo = ttk.Combobox(opts, textvariable=self.list_var,
                                       values=self.cfg.known_lists())
        self.list_combo.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(opts, text="pick one, or type a new name to create it",
                  style="Muted.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Label(opts, text="Task name:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.task_var = tk.StringVar()
        ttk.Entry(opts, textvariable=self.task_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Label(opts, text="name shown in FlexGet", style="Muted.TLabel").grid(
            row=1, column=2, sticky="w", pady=(6, 0))
        self.wav_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Download as WAV (audio only)",
                        variable=self.wav_var).grid(row=2, column=1, sticky="w",
                                                    padx=6, pady=(8, 0))
        self.save_btn = ttk.Button(opts, text="Yes - add this channel", style="Accent.TButton",
                                   command=self.do_add, state="disabled")
        self.save_btn.grid(row=3, column=1, sticky="e", pady=(10, 0))
        ttk.Button(opts, text="Clear", command=self.clear_add).grid(
            row=3, column=2, sticky="w", padx=6, pady=(10, 0))
        f.rowconfigure(3, weight=1)

    def do_lookup(self):
        text = self.url_var.get().strip()
        if not text:
            return
        self.lookup_btn.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.r_name.config(text="Looking up...")
        self.r_id.config(text="")
        self.r_warn.config(text="")
        self.r_videos.config(text="")
        self.avatar_lbl.config(image="")

        def work():
            try:
                info = lookup_channel(text)
                avatar = None
                if info.get("avatar"):
                    try:
                        req = urllib.request.Request(info["avatar"], headers={"User-Agent": UA})
                        avatar = urllib.request.urlopen(req, timeout=10).read()
                    except Exception:
                        pass
                self.after(0, self.show_found, info, avatar)
            except Exception as e:
                self.after(0, self.lookup_failed, str(e))
        threading.Thread(target=work, daemon=True).start()

    def lookup_failed(self, err):
        self.lookup_btn.config(state="normal")
        self.found = None
        self.r_name.config(text="Not found")
        self.r_id.config(text=err)

    def show_found(self, info, avatar):
        self.lookup_btn.config(state="normal")
        self.found = info
        name = info["name"] or "(name unknown)"
        self.r_name.config(text=name)
        self.r_id.config(text=f"{info['handle'] + '   ' if info['handle'] else ''}"
                              f"Channel ID: {info['id']}")
        warns = []
        dupes = self.cfg.find_id(info["id"])
        if dupes:
            warns.append("Already in FlexGet as: " +
                         ", ".join(f"{d.name} ({d.list_name})" for d in dupes))
        if not info.get("feed_ok"):
            warns.append("Couldn't read this channel's RSS feed - FlexGet may not see new videos.")
        self.r_warn.config(text="\n".join(warns))
        if info["videos"]:
            self.r_videos.config(text="Latest videos:\n" +
                                 "\n".join(f"  •  {v}" for v in info["videos"]))
        if avatar:
            try:
                from io import BytesIO
                from PIL import Image, ImageTk
                img = Image.open(BytesIO(avatar)).resize((88, 88))
                self.avatar_img = ImageTk.PhotoImage(img)
                self.avatar_lbl.config(image=self.avatar_img)
            except Exception:
                pass
        self.open_btn.config(state="normal")
        self.save_btn.config(state="normal")
        if not self.task_var.get():
            base = suggest_task_name(name if info["name"] else info["handle"].lstrip("@"))
            n, cand = 2, base
            while cand.lower() in self.cfg.names():
                cand, n = f"{base}{n}", n + 1
            self.task_var.set(cand)
        self.list_combo.focus_set()

    def open_found(self):
        if self.found:
            webbrowser.open(f"https://www.youtube.com/channel/{self.found['id']}")

    def do_add(self):
        if not self.found:
            return
        lst = self.list_var.get().strip()
        task = self.task_var.get().strip()
        err = validate(task, lst)
        if err:
            messagebox.showwarning("Check the details", err)
            return
        if self.cfg.find_id(self.found["id"]) and not messagebox.askyesno(
                "Already added",
                "This channel is already in your FlexGet config.\nAdd it again anyway?"):
            return
        new_list = lst.lower() not in {l.lower() for l in self.cfg.known_lists()}
        if new_list and not messagebox.askyesno(
                "New list", f"'{lst}' is a new list.\n\nCreate it?"):
            return
        try:
            self.cfg.add(task, self.found["id"], lst, self.wav_var.get())
            if new_list:
                touch_list(self.cfg.lists_dir, lst)
        except Exception as e:
            messagebox.showerror("Couldn't save", str(e))
            return
        fmt = "WAV" if self.wav_var.get() else "video"
        self.status.set(f"Added {task} → {lst} ({fmt})")
        messagebox.showinfo("Added", f"{self.found['name'] or task} was added to the {lst} list "
                                     f"as {fmt}.\n\n"
                                     "FlexGet will pick it up on its next run.")
        self.clear_add()
        self.refresh_lists()

    def clear_add(self):
        self.found = None
        self.url_var.set("")
        self.task_var.set("")
        self.wav_var.set(False)
        self.r_name.config(text="(nothing looked up yet)")
        for w in (self.r_id, self.r_warn, self.r_videos):
            w.config(text="")
        self.avatar_lbl.config(image="")
        self.open_btn.config(state="disabled")
        self.save_btn.config(state="disabled")

    # ------------------------------------------------------------------
    #  Channels tab
    # ------------------------------------------------------------------
    def build_list_tab(self):
        f = self.list_tab
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.fill_tree())
        ttk.Entry(top, textvariable=self.search_var, width=30).pack(side="left", padx=6)
        ttk.Label(top, text="List:").pack(side="left", padx=(12, 0))
        self.filter_var = tk.StringVar(value="All")
        self.filter_combo = ttk.Combobox(top, textvariable=self.filter_var,
                                         state="readonly", width=16)
        self.filter_combo.pack(side="left", padx=6)
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _: self.fill_tree())
        self.count_lbl = ttk.Label(top, style="Muted.TLabel")
        self.count_lbl.pack(side="right")

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, pady=8)
        cols = ("name", "list", "format", "id")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for c, label, w in (("name", "Channel", 240), ("list", "List", 130),
                            ("format", "Format", 70), ("id", "Channel ID", 240)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("warn", foreground=WARN)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _: self.do_edit())
        self.tree.bind("<Delete>", lambda _: self.do_delete())

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Edit...", command=self.do_edit).pack(side="left")
        ttk.Button(btns, text="Delete", command=self.do_delete).pack(side="left", padx=6)
        ttk.Button(btns, text="Open on YouTube", command=self.open_selected).pack(side="left")
        ttk.Button(btns, text="Reload", command=self.refresh_lists).pack(side="right")
        self.warn_lbl = ttk.Label(f, style="Warn.TLabel", wraplength=760, justify="left")
        self.warn_lbl.pack(fill="x", pady=(6, 0))
        self.refresh_lists()

    def refresh_lists(self):
        try:
            self.cfg.load()
        except Exception as e:
            messagebox.showerror("Couldn't read FlexGet config", str(e))
            return
        lists = self.cfg.known_lists()
        self.list_combo.config(values=lists)
        self.filter_combo.config(values=["All"] + lists)
        if self.filter_var.get() not in ["All"] + lists:
            self.filter_var.set("All")
        self.fill_tree()

    def channel_problems(self, c):
        lists_dir = self.cfg.lists_dir.as_posix().replace("\\", "/").lower()
        p = []
        if not c.channel_id:
            p.append("no channel ID")
        if c.list_path and Path(c.list_path).parent.as_posix().lower() != lists_dir:
            p.append("list is outside the lists folder, so ytdl won't download it")
        if c.channel_id and len(self.cfg.find_id(c.channel_id)) > 1:
            p.append("duplicate channel")
        return p

    def fill_tree(self):
        q = self.search_var.get().lower().strip()
        flt = self.filter_var.get()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        problems = []
        for c in sorted(self.cfg.channels, key=lambda c: c.name.lower()):
            if flt != "All" and c.list_name.lower() != flt.lower():
                continue
            if q and q not in c.name.lower() and q not in c.channel_id.lower() \
                    and q not in c.list_name.lower():
                continue
            probs = self.channel_problems(c)
            if probs:
                problems.append(f"{c.name}: {', '.join(probs)}")
            self.tree.insert("", "end", iid=c.name,
                             values=(c.name, c.list_name, "WAV" if c.wav else "Video",
                                     c.channel_id),
                             tags=("warn",) if probs else ())
            shown += 1
        self.count_lbl.config(text=f"{shown} of {len(self.cfg.channels)} channels")
        self.warn_lbl.config(text=("Needs attention (orange):  " + "   |   ".join(problems))
                             if problems else "")

    def selected(self):
        return [self.cfg_channel(i) for i in self.tree.selection() if self.cfg_channel(i)]

    def cfg_channel(self, name):
        return next((c for c in self.cfg.channels if c.name == name), None)

    def open_selected(self):
        for c in self.selected()[:5]:
            if c.channel_id:
                webbrowser.open(f"https://www.youtube.com/channel/{c.channel_id}")

    def do_delete(self):
        sel = self.selected()
        if not sel:
            return
        names = "\n".join(f"  •  {c.name}  ({c.list_name})" for c in sel[:15])
        more = f"\n  ...and {len(sel) - 15} more" if len(sel) > 15 else ""
        if not messagebox.askyesno("Delete channels",
                                   f"Remove {len(sel)} channel(s) from FlexGet?\n\n{names}{more}\n\n"
                                   "Videos already downloaded are not affected."):
            return
        try:
            self.cfg.delete({c.name for c in sel})
        except Exception as e:
            messagebox.showerror("Couldn't save", str(e))
            return
        self.status.set(f"Deleted {len(sel)} channel(s)")
        self.refresh_lists()

    def do_edit(self):
        sel = self.selected()
        if len(sel) != 1:
            if len(sel) > 1:
                messagebox.showinfo("Edit", "Select a single channel to edit.")
            return
        EditDialog(self, sel[0])


    # ------------------------------------------------------------------
    #  Waiting tab
    # ------------------------------------------------------------------
    CHECK_ON, CHECK_OFF = "\u2611", "\u2610"     # ☑ ☐

    def build_wait_tab(self):
        f = self.wait_tab
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="URLs waiting to download. Click a row to tick it, "
                            "then press Delete selected.").pack(side="left")
        self.wait_count = ttk.Label(top, style="Muted.TLabel")
        self.wait_count.pack(side="right")

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, pady=8)
        self.wait_tree = ttk.Treeview(mid, columns=("check", "list", "url", "format"),
                                      show="headings", selectmode="none")
        for c, label, w, stretch in (("check", "", 40, False), ("list", "List", 130, False),
                                     ("url", "Video link", 420, True),
                                     ("format", "Format", 70, False)):
            self.wait_tree.heading(c, text=label)
            self.wait_tree.column(c, width=w, anchor="center" if c == "check" else "w",
                                  stretch=stretch)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.wait_tree.yview)
        self.wait_tree.configure(yscrollcommand=sb.set)
        self.wait_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.wait_tree.bind("<Button-1>", self.wait_click)

        btns = ttk.Frame(f)
        btns.pack(fill="x")
        ttk.Button(btns, text="Delete selected", command=self.do_wait_delete).pack(side="left")
        ttk.Button(btns, text="Tick all", command=lambda: self.wait_tick_all(True)).pack(
            side="left", padx=6)
        ttk.Button(btns, text="Clear ticks", command=lambda: self.wait_tick_all(False)).pack(
            side="left")
        ttk.Button(btns, text="Reload", command=self.refresh_waiting).pack(side="right")

        self.wait_items = {}      # key -> (list_path, url, format)
        self.wait_checked = set() # keys that are ticked

    def refresh_waiting(self):
        self.wait_tree.delete(*self.wait_tree.get_children())
        self.wait_items.clear()
        self.wait_checked.clear()
        # Row ids are plain numbers; the path and URL live in wait_items.
        for i, (path, url, fmt) in enumerate(read_waiting(self.cfg.lists_dir)):
            key = str(i)
            self.wait_items[key] = (path, url, fmt)
            self.wait_tree.insert("", "end", iid=key, values=(
                self.CHECK_OFF, Path(path).stem, url, fmt.upper() if fmt else "List default"))
        n = len(self.wait_items)
        self.wait_count.config(text=f"{n} waiting")

    def wait_click(self, event):
        if self.wait_tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return None
        key = self.wait_tree.identify_row(event.y)
        if not key:
            return None
        self.wait_set_tick(key, key not in self.wait_checked)
        return "break"

    def wait_set_tick(self, key, on):
        if on:
            self.wait_checked.add(key)
        else:
            self.wait_checked.discard(key)
        self.wait_tree.set(key, "check", self.CHECK_ON if on else self.CHECK_OFF)

    def wait_tick_all(self, on):
        for key in self.wait_items:
            self.wait_set_tick(key, on)

    def do_wait_delete(self):
        keys = sorted(self.wait_checked, key=int)
        if not keys:
            messagebox.showinfo("Delete", "Tick the links you want to remove first.")
            return
        lines = []
        for k in keys[:15]:
            _, url, _ = self.wait_items[k]
            lines.append(f"  \u2022  {url}   ({Path(self.wait_items[k][0]).stem})")
        more = f"\n  ...and {len(keys) - 15} more" if len(keys) > 15 else ""
        if not messagebox.askyesno(
                "Delete waiting links",
                f"Remove {len(keys)} link(s) from the download lists?\n\n"
                + "\n".join(lines) + more + "\n\n"
                "These will NOT be downloaded. Videos already downloaded are not affected."):
            return
        pairs = [(self.wait_items[k][0], self.wait_items[k][1]) for k in keys]
        try:
            removed = remove_waiting(pairs)
        except OSError as e:
            messagebox.showerror("Couldn't save", str(e))
            self.refresh_waiting()
            return
        self.status.set(f"Removed {removed} line(s) from the lists")
        self.refresh_waiting()


class EditDialog(tk.Toplevel):
    def __init__(self, app: App, ch: Channel):
        super().__init__(app)
        self.app, self.ch = app, ch
        self.title(f"Edit {ch.name}")
        self.transient(app)
        self.resizable(False, False)
        f = ttk.Frame(self, padding=14)
        f.pack(fill="both", expand=True)

        self.name_var = tk.StringVar(value=ch.name)
        self.list_var = tk.StringVar(value=ch.list_name)
        self.id_var = tk.StringVar(value=ch.channel_id)

        ttk.Label(f, text="Task name:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.name_var, width=36).grid(row=0, column=1, pady=4)
        ttk.Label(f, text="List:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.list_var, width=34,
                     values=app.cfg.known_lists()).grid(row=1, column=1, pady=4)
        ttk.Label(f, text="Channel ID:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.id_var, width=36).grid(row=2, column=1, pady=4)
        self.wav_var = tk.BooleanVar(value=ch.wav)
        ttk.Checkbutton(f, text="Download as WAV (audio only)",
                        variable=self.wav_var).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(f, text=f"Currently saves to: {ch.list_path}", style="Muted.TLabel",
                  wraplength=360).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="Save", style="Accent.TButton", command=self.save).pack(side="right", padx=6)
        self.bind("<Return>", lambda _: self.save())
        self.bind("<Escape>", lambda _: self.destroy())
        self.grab_set()

    def save(self):
        name, lst, cid = (self.name_var.get().strip(), self.list_var.get().strip(),
                          self.id_var.get().strip())
        err = validate(name, lst)
        if not err and not CHANNEL_ID_RE.fullmatch(cid):
            err = "The channel ID should start with UC and be 24 characters long."
        if err:
            messagebox.showwarning("Check the details", err, parent=self)
            return
        new_list = lst.lower() not in {l.lower() for l in self.app.cfg.known_lists()}
        try:
            self.app.cfg.update(self.ch.name, name, cid, lst, self.wav_var.get())
            if new_list:
                touch_list(self.app.cfg.lists_dir, lst)
        except Exception as e:
            messagebox.showerror("Couldn't save", str(e), parent=self)
            return
        self.app.status.set(f"Saved {name}")
        self.app.refresh_lists()
        self.destroy()



def show_crash(err):
    crash = HERE / "channels_crash.txt"
    try:
        crash.write_text(err, encoding="utf-8")
    except Exception:
        pass
    print(err, file=sys.stderr)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, f"Channel Manager crashed:\n\n{err[-1500:]}\n\nSaved to {crash}",
            "Channel Manager error", 0x10)


if __name__ == "__main__":
    try:
        app = App()
        app.report_callback_exception = lambda *a: show_crash(
            "".join(__import__("traceback").format_exception(*a)))
        app.mainloop()
    except Exception:
        import traceback
        show_crash(traceback.format_exc())
        sys.exit(1)
