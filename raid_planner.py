#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RAID / пул — планировщик: интерактивный расчёт групп RAID по исходному пулу дисков."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable


def marketing_tb_to_bytes(tb: float) -> int:
    """Маркетинговые ТБ → байты: TB * 10**12."""
    return int(tb * 10**12)


def format_bytes(n: int) -> str:
    """Человекочитаемый объём в байтах с разделителями тысяч."""
    return f"{n:,}".replace(",", " ")


def format_tb(bytes_val: int) -> str:
    """Байты → маркетинговые ТБ (÷ 10**12)."""
    return f"{bytes_val / 10**12:.3f}"


def suggest_parity(raid_type: str, data: int) -> int:
    """Рекомендуемое число дисков под чётность при смене типа RAID."""
    rt = str(raid_type).strip()
    if rt == "0":
        return 0
    if rt == "5":
        return 1
    if rt == "6":
        return 2
    if rt in ("1", "10"):
        # зеркало: под чётность столько же, сколько под данные
        return max(1, data) if data > 0 else 2
    return 0


class RaidRow:
    """Одна строка RAID-группы: тип, данные, чётность, итого = data+parity."""

    def __init__(
        self,
        parent: ttk.Frame,
        on_change: Callable[[], None],
        on_remove: Callable[["RaidRow"], None],
        row_index: int,
    ) -> None:
        self.on_change = on_change
        self.on_remove = on_remove
        self._updating = False

        self.frame = ttk.Frame(parent, padding=(4, 2))
        self.frame.grid(row=row_index, column=0, sticky="ew", pady=2)
        parent.columnconfigure(0, weight=1)

        col = 0
        self.lbl_num = ttk.Label(self.frame, text=f"#{row_index + 1}", width=4)
        self.lbl_num.grid(row=0, column=col, padx=(0, 6))
        col += 1

        # 1. RAID
        ttk.Label(self.frame, text="RAID:").grid(row=0, column=col, sticky="e")
        col += 1
        self.raid_var = tk.StringVar(value="6")
        self.raid_combo = ttk.Combobox(
            self.frame,
            textvariable=self.raid_var,
            values=("0", "1", "5", "6", "10"),
            width=5,
            state="readonly",
        )
        self.raid_combo.grid(row=0, column=col, padx=(4, 10))
        col += 1
        self.raid_combo.bind("<<ComboboxSelected>>", lambda *_: self._on_raid_type_change())

        # 2. диски под данные
        ttk.Label(self.frame, text="диски под данные:").grid(row=0, column=col, sticky="e")
        col += 1
        self.data_var = tk.StringVar(value="4")
        self.data_spin = ttk.Spinbox(
            self.frame,
            from_=0,
            to=9999,
            width=5,
            textvariable=self.data_var,
            command=self._on_data_change,
        )
        self.data_spin.grid(row=0, column=col, padx=(4, 10))
        col += 1
        self.data_var.trace_add("write", lambda *_: self._on_data_change())

        # 3. под чётность
        ttk.Label(self.frame, text="под чётность:").grid(row=0, column=col, sticky="e")
        col += 1
        self.parity_var = tk.StringVar(value="2")
        self.parity_spin = ttk.Spinbox(
            self.frame,
            from_=0,
            to=9999,
            width=5,
            textvariable=self.parity_var,
            command=self._on_parity_change,
        )
        self.parity_spin.grid(row=0, column=col, padx=(4, 10))
        col += 1
        self.parity_var.trace_add("write", lambda *_: self._on_parity_change())

        # 4. итого дисков (только отображение)
        ttk.Label(self.frame, text="итого дисков:").grid(row=0, column=col, sticky="e")
        col += 1
        self.total_var = tk.StringVar(value="6")
        self.total_entry = ttk.Entry(
            self.frame, textvariable=self.total_var, width=5, state="readonly"
        )
        self.total_entry.grid(row=0, column=col, padx=(4, 10))
        col += 1

        # 5. объёмы
        self.usable_lbl = ttk.Label(self.frame, text="полезно: —", width=32)
        self.usable_lbl.grid(row=0, column=col, padx=(4, 6), sticky="w")
        col += 1

        self.parity_vol_lbl = ttk.Label(self.frame, text="чётность: —", width=32)
        self.parity_vol_lbl.grid(row=0, column=col, padx=(4, 6), sticky="w")
        col += 1

        self.warn_row_lbl = ttk.Label(self.frame, text="", foreground="darkorange", width=18)
        self.warn_row_lbl.grid(row=0, column=col, padx=(2, 4), sticky="w")
        col += 1

        self.btn_remove = ttk.Button(
            self.frame, text="−", width=3, command=lambda: self.on_remove(self)
        )
        self.btn_remove.grid(row=0, column=col, padx=(8, 0))

        self._sync_total()

    def set_index(self, index: int) -> None:
        self.lbl_num.configure(text=f"#{index + 1}")
        self.frame.grid(row=index, column=0, sticky="ew", pady=2)

    def _parse_int(self, var: tk.StringVar, default: int = 0) -> int:
        try:
            return int(str(var.get()).strip())
        except (TypeError, ValueError):
            return default

    def data_disks(self) -> int:
        return max(0, self._parse_int(self.data_var, 0))

    def parity_disks(self) -> int:
        return max(0, self._parse_int(self.parity_var, 0))

    def total_disks(self) -> int:
        return self.data_disks() + self.parity_disks()

    def raid_type(self) -> str:
        return str(self.raid_var.get()).strip()

    def soft_warning(self) -> str:
        """Мягкая проверка конфигурации строки."""
        data = self.data_disks()
        parity = self.parity_disks()
        total = data + parity
        rt = self.raid_type()

        if data < 1:
            return "нужен ≥1 диск данных"
        if total < 1:
            return "итого = 0"
        if rt == "5" and parity != 1:
            return "RAID5: обычно чётность=1"
        if rt == "6" and parity != 2:
            return "RAID6: обычно чётность=2"
        if rt == "0" and parity != 0:
            return "RAID0: обычно без чётности"
        if rt in ("1", "10") and parity != data:
            return "RAID1/10: обычно data=parity"
        if parity >= total and total > 0:
            return "чётность ≥ итого"
        return ""

    def _sync_total(self) -> None:
        self.total_var.set(str(self.total_disks()))

    def _on_raid_type_change(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            data = self.data_disks()
            if data < 1:
                data = 4 if self.raid_type() == "6" else 2
                self.data_var.set(str(data))
            p = suggest_parity(self.raid_type(), data)
            # для 1/10: если data ещё «старое» от RAID6 — стартер 2+2
            if self.raid_type() in ("1", "10") and data != p:
                # держим зеркало: parity = data (уже из suggest)
                pass
            self.parity_var.set(str(p))
            self._sync_total()
        finally:
            self._updating = False
        self._notify()

    def _on_data_change(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            # для зеркала при смене data подсказать parity = data
            if self.raid_type() in ("1", "10"):
                self.parity_var.set(str(max(0, self.data_disks())))
            self._sync_total()
        finally:
            self._updating = False
        self._notify()

    def _on_parity_change(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self._sync_total()
        finally:
            self._updating = False
        self._notify()

    def _notify(self) -> None:
        if not self._updating:
            self.on_change()

    def update_volumes(self, disk_bytes: int) -> tuple[int, int]:
        """Обновить подписи объёмов. Возвращает (usable_bytes, parity_bytes)."""
        data = self.data_disks()
        parity = self.parity_disks()
        self._sync_total()

        warn = self.soft_warning()
        self.warn_row_lbl.configure(text=warn)

        if data < 1:
            self.usable_lbl.configure(text="полезно: —", foreground="red")
            self.parity_vol_lbl.configure(text="чётность: —", foreground="red")
            return 0, 0

        usable = data * disk_bytes
        parity_space = parity * disk_bytes
        fg = "darkorange" if warn else ""
        self.usable_lbl.configure(
            text=f"полезно: {format_tb(usable)} ТБ  ({format_bytes(usable)} Б)",
            foreground=fg,
        )
        self.parity_vol_lbl.configure(
            text=f"чётность: {format_tb(parity_space)} ТБ  ({format_bytes(parity_space)} Б)",
            foreground=fg,
        )
        return usable, parity_space

    def destroy(self) -> None:
        self.frame.destroy()


class RaidPlannerApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=12)
        self.master = master
        self.pack(fill="both", expand=True)

        self.rows: list[RaidRow] = []

        self.disk_count_var = tk.StringVar(value="20")
        self.disk_tb_var = tk.StringVar(value="14.0")

        self._build_pool_section()
        self._build_raid_section()
        self._build_status()

        for var in (self.disk_count_var, self.disk_tb_var):
            var.trace_add("write", lambda *_: self.refresh())

        self.refresh()

    def _build_pool_section(self) -> None:
        pool = ttk.LabelFrame(self, text="Исходный пул", padding=10)
        pool.pack(fill="x", pady=(0, 10))

        row1 = ttk.Frame(pool)
        row1.pack(fill="x", pady=2)

        ttk.Label(row1, text="Исходное количество дисков:").grid(row=0, column=0, sticky="w")
        ttk.Entry(row1, textvariable=self.disk_count_var, width=10).grid(
            row=0, column=1, padx=(6, 20), sticky="w"
        )

        ttk.Label(row1, text="Объём одного диска (маркетинговые ТБ):").grid(
            row=0, column=2, sticky="w"
        )
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

    def _build_raid_section(self) -> None:
        raid = ttk.LabelFrame(self, text="Группы RAID", padding=10)
        raid.pack(fill="both", expand=True)

        toolbar = ttk.Frame(raid)
        toolbar.pack(fill="x", pady=(0, 6))

        ttk.Button(toolbar, text="+", width=3, command=self.add_row).pack(side="left")
        ttk.Label(toolbar, text="Добавить RAID-группу").pack(side="left", padx=(6, 0))

        canvas_wrap = ttk.Frame(raid)
        canvas_wrap.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_wrap, highlightthickness=0, height=220)
        scrollbar = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.canvas.yview)
        self.rows_frame = ttk.Frame(self.canvas)

        self.rows_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self._rows_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        def _on_canvas_configure(event: tk.Event) -> None:
            self.canvas.itemconfigure(self._rows_window, width=event.width)

        self.canvas.bind("<Configure>", _on_canvas_configure)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.canvas.bind_all(
            "<MouseWheel>",
            lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

    def _build_status(self) -> None:
        self.warn_lbl = ttk.Label(self, text="", foreground="red")
        self.warn_lbl.pack(fill="x", pady=(8, 0))

        totals = ttk.Frame(self)
        totals.pack(fill="x", pady=(4, 0))
        self.total_usable_lbl = ttk.Label(totals, text="Суммарно полезно (под LUN): —")
        self.total_usable_lbl.pack(anchor="w")
        self.total_parity_lbl = ttk.Label(totals, text="Суммарно под чётность: —")
        self.total_parity_lbl.pack(anchor="w")

    def add_row(self) -> None:
        row = RaidRow(
            parent=self.rows_frame,
            on_change=self.refresh,
            on_remove=self.remove_row,
            row_index=len(self.rows),
        )
        self.rows.append(row)
        self.refresh()

    def remove_row(self, row: RaidRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.destroy()
            for i, r in enumerate(self.rows):
                r.set_index(i)
            self.refresh()

    def _parse_pool(self) -> tuple[int, float, int]:
        try:
            count = int(str(self.disk_count_var.get()).strip())
        except (TypeError, ValueError):
            count = 0
        try:
            tb = float(str(self.disk_tb_var.get()).strip().replace(",", "."))
        except (TypeError, ValueError):
            tb = 0.0
        count = max(0, count)
        tb = max(0.0, tb)
        disk_bytes = marketing_tb_to_bytes(tb)
        return count, tb, disk_bytes

    def refresh(self) -> None:
        count, tb, disk_bytes = self._parse_pool()
        pool_bytes = count * disk_bytes

        self.pool_bytes_lbl.configure(
            text=f"Доступный объём: {format_bytes(pool_bytes)} Б"
        )
        self.pool_tb_lbl.configure(
            text=f"Доступный объём (маркетинговые ТБ): {format_tb(pool_bytes)} ТБ  "
            f"(дисков {count} × {tb:g} ТБ)"
        )

        used_disks = sum(r.total_disks() for r in self.rows)
        remaining_disks = count - used_disks
        remaining_bytes = remaining_disks * disk_bytes

        remain_fg = "red" if remaining_disks < 0 else ""
        self.remain_disks_lbl.configure(
            text=f"Осталось дисков (сырые, не в RAID): {remaining_disks}",
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
                f"⚠ Использовано дисков ({used_disks}) больше, чем в пуле ({count})!"
            )

        total_usable = 0
        total_parity = 0
        for r in self.rows:
            u, p = r.update_volumes(disk_bytes)
            total_usable += u
            total_parity += p
            w = r.soft_warning()
            if w:
                warnings.append(f"#{self.rows.index(r) + 1}: {w}")

        self.warn_lbl.configure(text="  |  ".join(warnings) if warnings else "")

        self.total_usable_lbl.configure(
            text=(
                f"Суммарно полезно (под LUN): {format_tb(total_usable)} ТБ  "
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
    root.title("RAID / пул — планировщик")
    root.geometry("1280x560")
    root.minsize(900, 420)

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
