#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Планировщик DDP: группы дисков и LUN-ы, размазанные по дискам согласно RAID."""

from __future__ import annotations

import html
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# Двоичные единицы (ТиБ/ГиБ), в интерфейсе подписываются как ТБ/ГБ.
GB = 2**30
TB = 2**40

LUN_PALETTE = (
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#d97706",
    "#7c3aed",
    "#0891b2",
    "#ea580c",
    "#db2777",
    "#0d9488",
    "#65a30d",
)

RAID_TYPES = ("0", "1", "5", "6", "10")


def tb_to_bytes(tb: float) -> int:
    """ТБ (двоичные, 2**40) → байты."""
    return int(tb * TB)


def format_bytes(n: int) -> str:
    """Человекочитаемый объём в байтах с разделителями тысяч."""
    return f"{n:,}".replace(",", " ")


def format_tb(bytes_val: int) -> str:
    """Байты → ТБ (÷ 2**40)."""
    return f"{bytes_val / TB:.3f}"


def format_gb(bytes_val: int) -> str:
    """Байты → ГБ (÷ 2**30)."""
    return f"{bytes_val / GB:.0f}"


def format_gb_tb(bytes_val: int) -> str:
    """ГБ и ТБ в скобках — как у бегунка LUN: «56000 ГБ  (54.688 ТБ)»."""
    return f"{format_gb(bytes_val)} ГБ  ({format_tb(bytes_val)} ТБ)"


def parse_int(var: tk.StringVar, default: int = 0) -> int:
    try:
        return int(str(var.get()).strip())
    except (TypeError, ValueError):
        return default


def parse_float(var: tk.StringVar, default: float = 0.0) -> float:
    try:
        return float(str(var.get()).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def min_width(raid_type: str) -> int:
    rt = str(raid_type).strip().lower()
    if rt == "0":
        return 2
    if rt == "1":
        return 2
    if rt == "5":
        return 3
    if rt == "6":
        return 4
    if rt == "10":
        return 4
    return 2


def suggest_width(raid_type: str) -> int:
    rt = str(raid_type).strip().lower()
    if rt == "0":
        return 2
    if rt == "1":
        return 2
    if rt == "5":
        return 4
    if rt == "6":
        return 6
    if rt == "10":
        return 4
    return 2


def normalize_width(raid_type: str, width: int) -> int:
    """Минимальная ширина; RAID-10 — чётное число дисков."""
    rt = str(raid_type).strip().lower()
    w = max(min_width(rt), width)
    if rt == "10" and w % 2:
        w += 1
    if rt == "1":
        w = 2
    return w


def data_disks(raid_type: str, width: int) -> int:
    rt = str(raid_type).strip().lower()
    w = max(0, width)
    if rt == "0":
        return w
    if rt in ("1", "10"):
        return w // 2
    if rt == "5":
        return max(0, w - 1)
    if rt == "6":
        return max(0, w - 2)
    return w


def parity_disks(raid_type: str, width: int) -> int:
    return max(0, width - data_disks(raid_type, width))


def extent_bytes(usable: int, raid_type: str, width: int) -> int:
    n = data_disks(raid_type, width)
    if n <= 0 or usable <= 0:
        return 0
    return usable // n


def usable_from_extent(extent: int, raid_type: str, width: int) -> int:
    return extent * data_disks(raid_type, width)


def parity_kind(raid_type: str, index_in_stripe: int, width: int) -> str:
    """Метка страйпа: '' данные, 'P' чётность/зеркало."""
    rt = str(raid_type).strip().lower()
    if rt == "0":
        return ""
    if rt in ("1", "10"):
        return "P" if index_in_stripe % 2 == 1 else ""
    if rt == "5":
        return "P" if index_in_stripe >= width - 1 else ""
    if rt == "6":
        return "P" if index_in_stripe >= width - 2 else ""
    return ""


def first_fit_disks(free: list[int], extent: int, width: int) -> list[int] | None:
    if extent <= 0 or width <= 0:
        return None
    picked: list[int] = []
    for i, f in enumerate(free):
        if f >= extent:
            picked.append(i)
            if len(picked) == width:
                return picked
    return None


def max_extent(free: list[int], width: int) -> int:
    if width <= 0 or width > len(free):
        return 0
    return sorted(free, reverse=True)[width - 1]


def stipple_for(kind: str) -> str:
    if kind == "P":
        return "gray50"
    return ""


@dataclass
class Segment:
    lun_index: int
    name: str
    color: str
    size: int
    parity_kind: str = ""


@dataclass
class LunSpec:
    name: str
    color: str
    raid_type: str
    width: int
    usable: int


def place_luns(disk_count: int, disk_bytes: int, luns: list[LunSpec]) -> tuple[list[list[Segment]], list[int]]:
    """Разложить LUN-ы по дискам по порядку (first-fit). Свободное место — в хвосте."""
    free = [disk_bytes] * disk_count
    segments: list[list[Segment]] = [[] for _ in range(disk_count)]
    for i, lun in enumerate(luns):
        ext = extent_bytes(lun.usable, lun.raid_type, lun.width)
        if ext <= 0:
            continue
        chosen = first_fit_disks(free, ext, lun.width)
        if chosen is None:
            continue
        for j, d in enumerate(chosen):
            segments[d].append(
                Segment(
                    lun_index=i,
                    name=lun.name,
                    color=lun.color,
                    size=ext,
                    parity_kind=parity_kind(lun.raid_type, j, lun.width),
                )
            )
            free[d] -= ext
    return segments, free


class LunRow:
    """Одна строка LUN: тип RAID, ширина, бегунок размера."""

    def __init__(
        self,
        parent: ttk.Frame,
        index: int,
        on_change: Callable[[], None],
        on_remove: Callable[["LunRow"], None],
    ) -> None:
        self.on_change = on_change
        self.on_remove = on_remove
        self._updating = False
        self.color = LUN_PALETTE[index % len(LUN_PALETTE)]
        self._max_gb = 0.0

        self.frame = ttk.Frame(parent, padding=(2, 2))
        self.frame.grid(row=index, column=0, sticky="ew", pady=2)
        parent.columnconfigure(0, weight=1)

        col = 0
        self.lbl_name = ttk.Label(self.frame, text=f"LUN{index + 1}")
        self.lbl_name.grid(row=0, column=col, sticky="w", padx=(0, 6))
        col += 1

        self.swatch = tk.Canvas(self.frame, width=16, height=16, highlightthickness=1, highlightbackground="#444")
        self.swatch.grid(row=0, column=col, padx=(0, 8), sticky="w")
        self.swatch.create_rectangle(0, 0, 16, 16, fill=self.color, outline="")
        col += 1

        ttk.Label(self.frame, text="RAID:").grid(row=0, column=col, sticky="w")
        col += 1
        self.raid_var = tk.StringVar(value="0")
        self.raid_combo = ttk.Combobox(
            self.frame,
            textvariable=self.raid_var,
            values=RAID_TYPES,
            width=4,
            state="readonly",
        )
        self.raid_combo.grid(row=0, column=col, padx=(4, 8), sticky="w")
        col += 1
        self.raid_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_raid_change())

        ttk.Label(self.frame, text="дисков:").grid(row=0, column=col, sticky="w")
        col += 1
        self.width_var = tk.StringVar(value="2")
        self.width_spin = ttk.Spinbox(
            self.frame,
            from_=2,
            to=999,
            width=4,
            textvariable=self.width_var,
            command=self._on_width_change,
        )
        self.width_spin.grid(row=0, column=col, padx=(4, 8), sticky="w")
        col += 1
        self.width_var.trace_add("write", lambda *_: self._on_width_change())

        self.size_lbl = ttk.Label(self.frame, text="0 ГБ")
        self.size_lbl.grid(row=0, column=col, sticky="w", padx=(0, 10))
        col += 1

        try:
            free_bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        except tk.TclError:
            free_bg = "SystemButtonFace"
        self.free_lbl = tk.Label(
            self.frame,
            text="ещё 0 ТБ",
            fg="#15803d",
            bg=free_bg,
            font=("Segoe UI", 9),
        )
        self.free_lbl.grid(row=0, column=col, sticky="w", padx=(0, 8))
        col += 1

        self.warn_lbl = ttk.Label(self.frame, text="", foreground="darkorange")
        self.warn_lbl.grid(row=0, column=col, sticky="w", padx=(0, 4))
        col += 1

        self.frame.columnconfigure(col, weight=1)
        col += 1

        ttk.Button(self.frame, text="−", width=3, command=lambda: self.on_remove(self)).grid(
            row=0, column=col, padx=(8, 0), sticky="e"
        )

        self.size_var = tk.DoubleVar(value=0.0)
        self.scale = tk.Scale(
            self.frame,
            from_=0,
            to=1,
            resolution=1,
            orient=tk.HORIZONTAL,
            variable=self.size_var,
            showvalue=False,
            length=400,
            width=14,
            sliderlength=28,
            troughcolor="#94a3b8",
            activebackground="#2563eb",
            highlightthickness=0,
            borderwidth=1,
            relief=tk.FLAT,
            command=lambda *_: self._on_slider(),
        )
        self.scale.grid(row=1, column=0, columnspan=col + 1, sticky="ew", padx=(0, 8), pady=(0, 4))

    def set_index(self, index: int) -> None:
        self.color = LUN_PALETTE[index % len(LUN_PALETTE)]
        self.swatch.delete("all")
        self.swatch.create_rectangle(0, 0, 16, 16, fill=self.color, outline="")
        self.lbl_name.configure(text=f"LUN{index + 1}")
        self.frame.grid(row=index, column=0, sticky="ew", pady=2)

    def name(self) -> str:
        return str(self.lbl_name.cget("text"))

    def raid_type(self) -> str:
        return str(self.raid_var.get()).strip()

    def width(self) -> int:
        return normalize_width(self.raid_type(), max(0, parse_int(self.width_var, 0)))

    def usable_bytes(self) -> int:
        return int(self.size_var.get() * GB)

    def spec(self) -> LunSpec:
        return LunSpec(
            name=self.name(),
            color=self.color,
            raid_type=self.raid_type(),
            width=self.width(),
            usable=self.usable_bytes(),
        )

    def set_max_gb(self, max_gb: float) -> None:
        """Потолок бегунка: сколько ещё можно выкроить при текущей раскладке."""
        max_gb = max(0.0, float(max_gb))
        self._max_gb = max_gb
        to_val = max(1.0, max_gb)
        cur = float(self.size_var.get())
        self._updating = True
        try:
            self.scale.configure(to=to_val)
            if cur > max_gb:
                self.size_var.set(max_gb)
        finally:
            self._updating = False
        self._refresh_size_label()

    def _refresh_size_label(self) -> None:
        gb = float(self.size_var.get())
        usable = int(gb * GB)
        w = self.width()
        rt = self.raid_type()
        ext = extent_bytes(usable, rt, w)
        n_data = data_disks(rt, w)
        self.size_lbl.configure(
            text=f"{format_gb_tb(usable)}  ×{n_data} по {format_tb(ext)} ТБ"
        )
        remain = max(0, int(self._max_gb * GB) - usable)
        self.free_lbl.configure(text=f"ещё {format_tb(remain)} ТБ")

    def set_warning(self, text: str) -> None:
        self.warn_lbl.configure(text=text)

    def _on_raid_change(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            w = suggest_width(self.raid_type())
            self.width_var.set(str(w))
        finally:
            self._updating = False
        self._notify()

    def _on_width_change(self) -> None:
        if self._updating:
            return
        rt = self.raid_type()
        raw = parse_int(self.width_var, min_width(rt))
        norm = normalize_width(rt, raw)
        if str(norm) != str(self.width_var.get()).strip():
            self._updating = True
            try:
                self.width_var.set(str(norm))
            finally:
                self._updating = False
        self._notify()

    def _on_slider(self) -> None:
        if self._updating:
            return
        if self._max_gb > 0 and float(self.size_var.get()) > self._max_gb:
            self._updating = True
            try:
                self.size_var.set(self._max_gb)
            finally:
                self._updating = False
        self._refresh_size_label()
        self._notify()

    def _notify(self) -> None:
        if not self._updating:
            self.on_change()

    def destroy(self) -> None:
        self.frame.destroy()


class DdpTab(ttk.Frame):
    """Вкладка группы DDP: число дисков, список LUN, карта дисков."""

    def __init__(self, master: ttk.Notebook, app: "RaidPlannerApp", title: str) -> None:
        super().__init__(master, padding=8)
        self.app = app
        self.title = title
        self.luns: list[LunRow] = []
        self._last_segments: list[list[Segment]] = []
        self._last_free: list[int] = []
        self._last_usable = 0
        self._last_parity = 0
        self._drawing = False
        self.disk_count_var = tk.StringVar(value="5")
        self.disk_count_var.trace_add("write", lambda *_: self.app.refresh())

        header = ttk.Frame(self)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(header, text="Дисков в группе:").pack(side="left")
        ttk.Spinbox(
            header,
            from_=0,
            to=9999,
            width=6,
            textvariable=self.disk_count_var,
        ).pack(side="left", padx=(6, 16))
        self.group_info_lbl = ttk.Label(header, text="")
        self.group_info_lbl.pack(side="left")

        # Карта дисков снизу: pack первым, чтобы LUN-ы не вытесняли её за край окна.
        disk_wrap = ttk.LabelFrame(self, text="Диски группы (нумерация по порядку)", padding=6)
        disk_wrap.pack(side="bottom", fill="both", expand=True)
        legend = ttk.Label(
            disk_wrap,
            text="Сплошной цвет — данные LUN. Штриховка и «P» — чётность/зеркало того же LUN. Серое — свободно.",
        )
        legend.pack(anchor="w", pady=(0, 4))

        canvas_row = ttk.Frame(disk_wrap)
        canvas_row.pack(fill="both", expand=True)
        self.disk_canvas = tk.Canvas(
            canvas_row, highlightthickness=0, background="#f8fafc", height=200
        )
        scrollbar = ttk.Scrollbar(canvas_row, orient="vertical", command=self.disk_canvas.yview)
        self.disk_canvas.configure(yscrollcommand=scrollbar.set)
        self.disk_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.disk_canvas.bind("<Configure>", lambda *_: self.redraw_disks(use_cache=True))
        self.disk_canvas.bind(
            "<MouseWheel>",
            lambda e: self.disk_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        lun_wrap = ttk.LabelFrame(self, text="LUN-ы", padding=6)
        lun_wrap.pack(side="top", fill="both", expand=True, pady=(0, 8))
        toolbar = ttk.Frame(lun_wrap)
        toolbar.pack(fill="x", pady=(0, 4))
        ttk.Button(toolbar, text="+", width=3, command=self.add_lun).pack(side="left")
        ttk.Label(toolbar, text="Добавить LUN").pack(side="left", padx=(6, 0))

        lun_scroll = ttk.Frame(lun_wrap)
        lun_scroll.pack(fill="both", expand=True)
        self.lun_canvas = tk.Canvas(
            lun_scroll, highlightthickness=0, background="#f8fafc", height=120
        )
        lun_scroll_bar = ttk.Scrollbar(
            lun_scroll, orient="vertical", command=self.lun_canvas.yview
        )
        self.lun_canvas.configure(yscrollcommand=lun_scroll_bar.set)
        self.lun_canvas.pack(side="left", fill="both", expand=True)
        lun_scroll_bar.pack(side="right", fill="y")

        self.luns_frame = ttk.Frame(self.lun_canvas)
        self._lun_window = self.lun_canvas.create_window(
            (0, 0), window=self.luns_frame, anchor="nw"
        )
        self.luns_frame.bind("<Configure>", lambda *_: self._sync_lun_scroll())
        self.lun_canvas.bind("<Configure>", self._on_lun_canvas_configure)
        self._bind_lun_mousewheel(lun_wrap)
        lun_wrap.bind("<Enter>", self._lun_wheel_on)
        lun_wrap.bind("<Leave>", self._lun_wheel_off)

    def _sync_lun_scroll(self) -> None:
        bbox = self.lun_canvas.bbox("all")
        self.lun_canvas.configure(scrollregion=bbox if bbox else (0, 0, 0, 0))

    def _on_lun_canvas_configure(self, event: tk.Event) -> None:
        self.lun_canvas.itemconfigure(self._lun_window, width=max(1, event.width))

    def _on_lun_mousewheel(self, event: tk.Event) -> str:
        self.lun_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _lun_wheel_on(self, _event: tk.Event | None = None) -> None:
        self.lun_canvas.bind_all("<MouseWheel>", self._on_lun_mousewheel)

    def _lun_wheel_off(self, _event: tk.Event | None = None) -> None:
        self.lun_canvas.unbind_all("<MouseWheel>")

    def _bind_lun_mousewheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_lun_mousewheel)
        for child in widget.winfo_children():
            self._bind_lun_mousewheel(child)

    def disk_count(self) -> int:
        return max(0, parse_int(self.disk_count_var, 0))

    def add_lun(self) -> None:
        row = LunRow(
            parent=self.luns_frame,
            index=len(self.luns),
            on_change=self.app.refresh,
            on_remove=self.remove_lun,
        )
        self.luns.append(row)
        self._bind_lun_mousewheel(row.frame)
        self._sync_lun_scroll()
        self.app.refresh()

    def remove_lun(self, row: LunRow) -> None:
        if row in self.luns:
            self.luns.remove(row)
            row.destroy()
            for i, r in enumerate(self.luns):
                r.set_index(i)
            self._sync_lun_scroll()
            self.app.refresh()

    def apply_limits_and_place(self, disk_bytes: int) -> tuple[list[list[Segment]], list[int], int, int]:
        """Обновить потолки бегунков, затем разложить LUN-ы. Возвращает сегменты, free, usable, parity."""
        n = self.disk_count()
        free = [disk_bytes] * n
        placed: list[LunSpec] = []
        total_usable = 0
        total_parity = 0

        for row in self.luns:
            w = row.width()
            rt = row.raid_type()
            warn = ""
            if n < w:
                warn = f"нужно ≥{w} дисков в группе"
                row.set_max_gb(0)
                row.set_warning(warn)
                placed.append(row.spec())
                continue

            e_max = max_extent(free, w)
            u_max = usable_from_extent(e_max, rt, w)
            row.set_max_gb(u_max / GB)

            spec = row.spec()
            ext = extent_bytes(spec.usable, rt, w)
            chosen = first_fit_disks(free, ext, w) if ext > 0 else None
            if spec.usable > 0 and chosen is None:
                warn = "нельзя выкроить такой размер"
            elif spec.usable > 0 and u_max > 0 and spec.usable >= u_max:
                warn = "потолок по свободным страйпам"
            row.set_warning(warn)

            if chosen is not None and ext > 0:
                for d in chosen:
                    free[d] -= ext
                total_usable += ext * data_disks(rt, w)
                total_parity += ext * parity_disks(rt, w)

            placed.append(spec)

        segments, free_after = place_luns(n, disk_bytes, placed)
        self._last_usable = total_usable
        self._last_parity = total_parity
        return segments, free_after, total_usable, total_parity

    def redraw_disks(
        self,
        segments: list[list[Segment]] | None = None,
        free: list[int] | None = None,
        use_cache: bool = False,
    ) -> None:
        if self._drawing:
            return
        n = self.disk_count()
        disk_bytes = self.app.disk_bytes()
        if use_cache:
            segments = self._last_segments
            free = self._last_free
        if segments is None:
            segments = [[] for _ in range(n)]
        if free is None:
            free = [disk_bytes] * n
        self._last_segments = segments
        self._last_free = free

        self._drawing = True
        canvas = self.disk_canvas
        canvas.delete("all")

        width = max(200, int(canvas.winfo_width()) - 8)
        label_w = 36
        used_w = 130
        bar_x = label_w + 8
        bar_w = max(80, width - bar_x - used_w - 12)
        row_h = 28
        pad_y = 6
        bar_h = 20

        if n == 0 or disk_bytes <= 0:
            canvas.create_text(12, 16, anchor="w", text="Укажите число дисков в группе и объём диска в пуле.")
            canvas.configure(scrollregion=(0, 0, width, 40))
            self._drawing = False
            return

        for i in range(n):
            y = pad_y + i * row_h
            canvas.create_text(label_w, y + bar_h / 2, text=f"#{i + 1}", anchor="e", font=("Segoe UI", 10, "bold"))
            x0, y0 = bar_x, y
            x1, y1 = bar_x + bar_w, y + bar_h
            canvas.create_rectangle(x0, y0, x1, y1, fill="#e5e7eb", outline="#9ca3af")
            x = x0
            for seg in segments[i] if i < len(segments) else []:
                w = bar_w * seg.size / disk_bytes
                if w < 1:
                    w = 1
                canvas.create_rectangle(
                    x,
                    y0,
                    x + w,
                    y1,
                    fill=seg.color,
                    outline="#1f2937",
                    stipple=stipple_for(seg.parity_kind),
                )
                if w >= 22:
                    label = seg.parity_kind if seg.parity_kind else seg.name.replace("LUN", "L")
                    canvas.create_text(
                        x + w / 2,
                        y0 + bar_h / 2,
                        text=label,
                        fill="white",
                        font=("Segoe UI", 8, "bold"),
                    )
                x += w
            used = disk_bytes - (free[i] if i < len(free) else 0)
            canvas.create_text(
                x1 + 8,
                y0 + bar_h / 2,
                text=f"{format_tb(used)} / {format_tb(disk_bytes)} ТБ",
                anchor="w",
                font=("Segoe UI", 8),
            )

        canvas.configure(scrollregion=(0, 0, width, pad_y + n * row_h + 8))
        self._drawing = False

    def set_group_info(self, used: int, usable: int, parity: int) -> None:
        n = self.disk_count()
        total = n * self.app.disk_bytes()
        remain = max(0, total - used)
        self.group_info_lbl.configure(
            text=(
                f"занято сырого: {format_tb(used)} ТБ из {format_tb(total)} ТБ   |   "
                f"осталось: {format_tb(remain)} ТБ   |   "
                f"полезная нагрузка: {format_tb(usable)} ТБ   |   чётность: {format_tb(parity)} ТБ"
            )
        )


def _html_disk_bar(segments: list[Segment], free: int, disk_bytes: int) -> str:
    if disk_bytes <= 0:
        return ""
    parts: list[str] = []
    for seg in segments:
        pct = 100.0 * seg.size / disk_bytes
        kind_cls = f" parity-{seg.parity_kind.lower()}" if seg.parity_kind else ""
        label = html.escape(seg.parity_kind or seg.name)
        parts.append(
            f'<div class="seg{kind_cls}" style="width:{pct:.4f}%;background-color:{seg.color}" '
            f'title="{label}">{label}</div>'
        )
    if free > 0:
        pct = 100.0 * free / disk_bytes
        parts.append(f'<div class="seg free" style="width:{pct:.4f}%;" title="свободно"></div>')
    return "".join(parts)


def build_html_report(app: "RaidPlannerApp") -> str:
    """Собрать HTML-отчёт без сторонних библиотек. Из браузера можно сохранить в PDF."""
    count = app.pool_disk_count()
    tb = max(0.0, parse_float(app.disk_tb_var, 0.0))
    disk_bytes = app.disk_bytes()
    pool_bytes = count * disk_bytes
    used_disks = sum(t.disk_count() for t in app.tabs)
    remaining_disks = count - used_disks
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    total_usable = sum(t._last_usable for t in app.tabs)
    total_parity = sum(t._last_parity for t in app.tabs)

    sections: list[str] = []
    for tab in app.tabs:
        n = tab.disk_count()
        segs = tab._last_segments
        free = tab._last_free
        used_raw = n * disk_bytes - sum(free) if free else 0
        lun_rows = []
        for row in tab.luns:
            spec = row.spec()
            ext = extent_bytes(spec.usable, spec.raid_type, spec.width)
            lun_rows.append(
                "<tr>"
                f"<td><span class='swatch' style='background:{spec.color}'></span> {html.escape(spec.name)}</td>"
                f"<td>RAID-{html.escape(spec.raid_type)}</td>"
                f"<td>{spec.width}</td>"
                f"<td>{format_gb_tb(spec.usable)}</td>"
                f"<td>{format_gb_tb(ext)}</td>"
                f"<td>{data_disks(spec.raid_type, spec.width)} / {parity_disks(spec.raid_type, spec.width)}</td>"
                "</tr>"
            )
        disk_rows = []
        for i in range(n):
            disk_segs = segs[i] if i < len(segs) else []
            disk_free = free[i] if i < len(free) else disk_bytes
            used = disk_bytes - disk_free
            disk_rows.append(
                "<div class='disk'>"
                f"<div class='dnum'>#{i + 1}</div>"
                f"<div class='bar'>{_html_disk_bar(disk_segs, disk_free, disk_bytes)}</div>"
                f"<div class='dused'>{format_gb(used)} / {format_gb(disk_bytes)} ГБ"
                f"  ({format_tb(used)} / {format_tb(disk_bytes)} ТБ)</div>"
                "</div>"
            )
        sections.append(
            f"<section><h2>{html.escape(tab.title)}</h2>"
            f"<p>Дисков: {n}. Занято сырого: {format_gb_tb(used_raw)}. "
            f"Полезная нагрузка: {format_gb_tb(tab._last_usable)}. Чётность: {format_gb_tb(tab._last_parity)}.</p>"
            "<table><thead><tr><th>LUN</th><th>RAID</th><th>Дисков</th>"
            "<th>Полезная нагрузка</th><th>Страйп</th><th>Данные / чётность</th></tr></thead><tbody>"
            + ("".join(lun_rows) if lun_rows else "<tr><td colspan='6'>Нет LUN-ов</td></tr>")
            + "</tbody></table>"
            "<div class='disks'>" + "".join(disk_rows) + "</div></section>"
        )

    body = "\n".join(sections) if sections else "<p>Нет групп.</p>"
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Отчёт DDP / LUN</title>
<style>
  body {{ font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #111; }}
  h1 {{ margin: 0 0 8px; }}
  .meta {{ color: #555; margin-bottom: 24px; }}
  section {{ margin: 28px 0; page-break-inside: avoid; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0 16px; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; }}
  th {{ background: #f1f5f9; }}
  .swatch {{ display: inline-block; width: 12px; height: 12px; border: 1px solid #333; vertical-align: middle; }}
  .disk {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; }}
  .dnum {{ width: 36px; font-weight: 700; }}
  .dused {{ min-width: 220px; font-size: 12px; color: #334155; white-space: nowrap; }}
  .bar {{ flex: 1; display: flex; height: 22px; background: #e5e7eb; border: 1px solid #9ca3af; overflow: hidden; }}
  .seg {{ height: 100%; box-sizing: border-box; color: #fff; font-size: 11px; font-weight: 700;
         display: flex; align-items: center; justify-content: center; overflow: hidden; white-space: nowrap; }}
  .seg.free {{ background: #e5e7eb; }}
  .parity-p {{
    background-image: repeating-linear-gradient(
      -45deg,
      rgba(255,255,255,.55) 0,
      rgba(255,255,255,.55) 3px,
      transparent 3px,
      transparent 7px
    );
  }}
  .hint {{ margin-top: 32px; color: #64748b; font-size: 13px; }}
  @media print {{
    body {{ margin: 12px; }}
    .hint {{ display: none; }}
    * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style>
</head>
<body>
<h1>Отчёт DDP / LUN</h1>
<p class="meta">{html.escape(stamp)}</p>
<p>Пул: {count} × {tb:g} ТБ = {format_gb_tb(pool_bytes)} ({format_bytes(pool_bytes)} Б).
В группах: {used_disks} диск(ов), свободно: {remaining_disks}.
Суммарно полезная нагрузка: {format_gb_tb(total_usable)}, чётность: {format_gb_tb(total_parity)}.</p>
{body}
<p class="hint">Чтобы получить PDF: в браузере Печать → «Сохранить как PDF».</p>
</body>
</html>
"""


class RaidPlannerApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.master = master
        self.pack(fill="both", expand=True)
        self.tabs: list[DdpTab] = []
        self._refreshing = False

        self.disk_count_var = tk.StringVar(value="20")
        self.disk_tb_var = tk.StringVar(value="14.0")

        self._build_pool_section()
        self._build_groups_section()
        self._build_status()

        for var in (self.disk_count_var, self.disk_tb_var):
            var.trace_add("write", lambda *_: self.refresh())

        self.add_group()
        self.refresh()

    def disk_bytes(self) -> int:
        tb = max(0.0, parse_float(self.disk_tb_var, 0.0))
        return tb_to_bytes(tb)

    def pool_disk_count(self) -> int:
        return max(0, parse_int(self.disk_count_var, 0))

    def _build_pool_section(self) -> None:
        pool = ttk.LabelFrame(self, text="Исходный пул", padding=10)
        pool.pack(fill="x", pady=(0, 10))

        row1 = ttk.Frame(pool)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Исходное количество дисков:").grid(row=0, column=0, sticky="w")
        ttk.Entry(row1, textvariable=self.disk_count_var, width=10).grid(
            row=0, column=1, padx=(6, 20), sticky="w"
        )
        ttk.Label(row1, text="Объём одного диска (ТБ):").grid(row=0, column=2, sticky="w")
        ttk.Entry(row1, textvariable=self.disk_tb_var, width=10).grid(
            row=0, column=3, padx=(6, 0), sticky="w"
        )

        row2 = ttk.Frame(pool)
        row2.pack(fill="x", pady=(8, 2))
        self.pool_bytes_lbl = ttk.Label(row2, text="Доступный объём: —")
        self.pool_bytes_lbl.pack(anchor="w")
        self.pool_tb_lbl = ttk.Label(row2, text="Доступный объём (ТБ): —")
        self.pool_tb_lbl.pack(anchor="w")

        row3 = ttk.Frame(pool)
        row3.pack(fill="x", pady=(8, 2))
        self.remain_disks_lbl = ttk.Label(row3, text="Осталось дисков: —")
        self.remain_disks_lbl.pack(anchor="w")
        self.remain_vol_lbl = ttk.Label(row3, text="Осталось объёма: —")
        self.remain_vol_lbl.pack(anchor="w")

    def _build_groups_section(self) -> None:
        box = tk.LabelFrame(self, text="Группы DDP", font=("Segoe UI", 10, "bold"), padx=8, pady=8)
        box.pack(fill="both", expand=True)

        toolbar = ttk.Frame(box)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="+", width=3, command=self.add_group).pack(side="left")
        ttk.Label(toolbar, text="Добавить группу").pack(side="left", padx=(6, 12))
        ttk.Button(toolbar, text="−", width=3, command=self.remove_current_group).pack(side="left")
        ttk.Label(toolbar, text="Удалить текущую группу").pack(side="left", padx=(6, 12))
        ttk.Button(toolbar, text="Отчёт…", command=self.export_report).pack(side="left")
        ttk.Label(toolbar, text="HTML, из браузера можно сохранить в PDF").pack(side="left", padx=(6, 0))

        totals = ttk.Frame(box)
        totals.pack(fill="x", pady=(0, 6))
        self.total_usable_lbl = ttk.Label(totals, text="Суммарно полезная нагрузка (под LUN): —")
        self.total_usable_lbl.pack(anchor="w")
        self.total_parity_lbl = ttk.Label(totals, text="Суммарно под чётность: —")
        self.total_parity_lbl.pack(anchor="w")

        try:
            style = ttk.Style()
            style.configure("TNotebook.Tab", font=("Segoe UI", 9, "bold"))
        except tk.TclError:
            pass
        self.notebook = ttk.Notebook(box)
        self.notebook.pack(fill="both", expand=True)

    def _build_status(self) -> None:
        self.warn_lbl = ttk.Label(self, text="", foreground="red")
        self.warn_lbl.pack(fill="x", pady=(8, 0))

    def add_group(self) -> None:
        title = f"DDP{len(self.tabs) + 1}"
        tab = DdpTab(self.notebook, self, title)
        self.tabs.append(tab)
        self.notebook.add(tab, text=title)
        self.notebook.select(tab)
        self.refresh()

    def remove_current_group(self) -> None:
        if not self.tabs:
            return
        current = self.notebook.select()
        if not current:
            return
        widget = self.nametowidget(current)
        if widget not in self.tabs:
            return
        self.tabs.remove(widget)
        self.notebook.forget(widget)
        widget.destroy()
        for i, tab in enumerate(self.tabs):
            tab.title = f"DDP{i + 1}"
            self.notebook.tab(tab, text=tab.title)
        self.refresh()

    def export_report(self) -> None:
        self.refresh()
        path = filedialog.asksaveasfilename(
            parent=self.master,
            title="Сохранить отчёт",
            defaultextension=".html",
            filetypes=(("HTML", "*.html"), ("Все файлы", "*.*")),
            initialfile="ddp_report.html",
        )
        if not path:
            return
        try:
            Path(path).write_text(build_html_report(self), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Отчёт", f"Не удалось сохранить файл:\n{exc}")
            return
        webbrowser.open(Path(path).resolve().as_uri())

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh_body()
        finally:
            self._refreshing = False

    def _refresh_body(self) -> None:
        count = self.pool_disk_count()
        tb = max(0.0, parse_float(self.disk_tb_var, 0.0))
        disk_bytes = tb_to_bytes(tb)
        pool_bytes = count * disk_bytes

        self.pool_bytes_lbl.configure(text=f"Доступный объём: {format_bytes(pool_bytes)} Б")
        self.pool_tb_lbl.configure(
            text=f"Доступный объём (ТБ): {format_tb(pool_bytes)} ТБ  "
            f"(дисков {count} × {tb:g} ТБ)"
        )

        used_disks = sum(t.disk_count() for t in self.tabs)
        remaining_disks = count - used_disks
        remaining_bytes = remaining_disks * disk_bytes
        remain_fg = "red" if remaining_disks < 0 else ""
        self.remain_disks_lbl.configure(
            text=f"Осталось дисков (не в группах): {remaining_disks}",
            foreground=remain_fg,
        )
        self.remain_vol_lbl.configure(
            text=(
                f"Осталось объёма: {format_tb(max(0, remaining_bytes))} ТБ  "
                f"({format_bytes(max(0, remaining_bytes))} Б)"
                if remaining_disks >= 0
                else f"Осталось объёма: перерасход на {abs(remaining_disks)} диск(ов)"
            ),
            foreground=remain_fg,
        )

        warnings: list[str] = []
        if remaining_disks < 0:
            warnings.append(
                f"⚠ В группах дисков ({used_disks}) больше, чем в пуле ({count})!"
            )

        total_usable = 0
        total_parity = 0
        for tab in self.tabs:
            segments, free, usable, parity = tab.apply_limits_and_place(disk_bytes)
            used_raw = tab.disk_count() * disk_bytes - sum(free)
            tab.set_group_info(used_raw, usable, parity)
            tab.redraw_disks(segments, free)
            total_usable += usable
            total_parity += parity

        self.warn_lbl.configure(text="  |  ".join(warnings) if warnings else "")
        self.total_usable_lbl.configure(
            text=(
                f"Суммарно полезная нагрузка (под LUN): {format_tb(total_usable)} ТБ  "
                f"({format_bytes(total_usable)} Б)"
            )
        )
        self.total_parity_lbl.configure(
            text=(
                f"Суммарно под чётность: {format_tb(total_parity)} ТБ  "
                f"({format_bytes(total_parity)} Б)"
            )
        )


def main() -> None:
    root = tk.Tk()
    root.title("DDP / LUN — планировщик")
    root.geometry("1280x780")
    root.minsize(960, 560)

    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass

    RaidPlannerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
