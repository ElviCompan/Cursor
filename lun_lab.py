#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Лаборатория LUN/DDP: генерация lvcreate/lvextend и проверка страйпа по SSH.

Dry-run по умолчанию. Для SSH:  pip install paramiko
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import paramiko
except ImportError:
    paramiko = None  # type: ignore[assignment]


GB = 2**30
TB = 2**40
ROOT = Path(__file__).resolve().parent
DISKS_DIR = ROOT / "5.Disks"
if not DISKS_DIR.is_dir():
    DISKS_DIR = ROOT / "5.DIsks"

# Образец с лабораторной машины (VG fr).
SAMPLE_LVCREATE = (
    "sudo lvcreate -y --type raid0 --regionsize=1G --maxrecoveryrate 1000 "
    "-i 2 -L 107374080.0b -n ddp01 fr "
    "/dev/mapper/36000c299a5b525b789a367008beec9b6 "
    "/dev/mapper/36000c29753c87b6686d81308d2746b3d 2>&1"
)
SAMPLE_LVEXTEND = (
    "sudo lvextend -y -L 109576192b fr/ddp01 "
    "/dev/mapper/36000c29753c87b6686d81308d2746b3d "
    "/dev/mapper/36000c299a5b525b789a367008beec9b6 2>&1"
)

# Целевая машина — лабораторная VM (VG fr), не железо ENGINE-0.
LAB_VMWARE_DISKS = (
    "/dev/mapper/36000c299a5b525b789a367008beec9b6",
    "/dev/mapper/36000c29753c87b6686d81308d2746b3d",
)

NAA_MAPPER_RE = re.compile(r"^/dev/mapper/[36][0-9a-fA-F]+$")
NAA_NAME_RE = re.compile(r"^[36][0-9a-fA-F]+$")
PVS_PV_RE = re.compile(r"(/dev/mapper/[36][0-9a-fA-F]+)")

RAID_TYPES = ("raid0", "raid1", "raid5", "raid6", "raid10")
SIZE_UNITS = (("ГБ", GB), ("МБ", 2**20), ("ТБ", TB), ("Б", 1))

VERIFY_COMMANDS = (
    "sudo lvs -a -o lv_name,vg_name,lv_size,segtype,stripes,stripesize,regionsize,devices,copy_percent",
    "sudo pvs -o pv_name,vg_name,pv_size,pv_free,pv_pe_count,pv_pe_alloc_count",
    "sudo vgs -o vg_name,pv_count,lv_count,vg_size,vg_free",
    "sudo lsblk -o NAME,SIZE,TYPE,FSTYPE,PKNAME,WWN",
    "sudo ls -la /dev/mapper",
    "sudo lvdisplay -m",
    "sudo dmsetup ls --tree",
    "sudo dmsetup table",
)

FETCH_MAPPER_CMD = (
    "ls -1 /dev/mapper 2>/dev/null | "
    "grep -E '^[36][0-9a-fA-F]+$' | "
    "sed 's|^|/dev/mapper/|'"
)


def format_bytes(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def format_gb(bytes_val: int) -> str:
    return f"{bytes_val / GB:.3f}"


def format_tb(bytes_val: int) -> str:
    return f"{bytes_val / TB:.3f}"


def format_gb_tb(bytes_val: int) -> str:
    return f"{format_gb(bytes_val)} ГБ  ({format_tb(bytes_val)} ТБ)"


def parse_int(text: str, default: int = 0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def parse_float(text: str, default: float = 0.0) -> float:
    try:
        return float(str(text).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def unit_multiplier(label: str) -> int:
    for name, mul in SIZE_UNITS:
        if name == label:
            return mul
    return GB


def size_to_bytes(value_text: str, unit_label: str) -> int:
    return int(parse_float(value_text, 0.0) * unit_multiplier(unit_label))


def lun_lv_name(index: int) -> str:
    """Имя LV в стиле продакшена: DataStore1_SSD, DataStore2_SSD, …"""
    return f"DataStore{index}_SSD"


def load_placeholder_disks() -> list[str]:
    """Диски лабораторной VM из образца lvcreate (36000c29…), не ENGINE-0."""
    return list(LAB_VMWARE_DISKS)


def load_iron_dump_disks() -> list[str]:
    """PV из архива 5.Disks (ENGINE-0). Только справка, не дефолт лаборатории."""
    pvs_path = DISKS_DIR / "pvs"
    found: list[str] = []
    if pvs_path.is_file():
        try:
            text = pvs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for match in PVS_PV_RE.finditer(text):
            path = match.group(1)
            if path not in found:
                found.append(path)
    return found


def parse_disk_list(text: str) -> list[str]:
    disks: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("/dev/mapper/"):
            name = line
        elif NAA_NAME_RE.match(line):
            name = f"/dev/mapper/{line}"
        else:
            name = line
        if name not in disks:
            disks.append(name)
    return disks


def sudo_prefix(use_sudo: bool) -> str:
    return "sudo " if use_sudo else ""


def min_disks_for_raid(raid_type: str) -> int:
    rt = raid_type.strip().lower()
    return {"raid0": 2, "raid1": 2, "raid5": 3, "raid6": 4, "raid10": 4}.get(rt, 2)


@dataclass
class LunSpec:
    name: str
    size_bytes: int
    raid_type: str
    stripe_count: int
    disks: list[str]
    extend_bytes: int = 0
    reverse_extend_pvs: bool = True


@dataclass
class LabPlan:
    vg_name: str
    luns: list[LunSpec]
    disks_all: list[str]
    use_sudo: bool = True
    regionsize: str = "1G"
    maxrecoveryrate: str = "1000"
    stripesize: str = ""
    make_pv_vg: bool = False
    vg_exists: bool = False


def assign_disks_to_luns(
    disks: list[str],
    lun_count: int,
    stripe_count: int,
    share_all: bool,
) -> list[list[str]]:
    if lun_count <= 0:
        return []
    if share_all:
        chosen = disks[:stripe_count] if stripe_count else list(disks)
        return [list(chosen) for _ in range(lun_count)]
    groups: list[list[str]] = []
    if not disks or stripe_count <= 0:
        return [[] for _ in range(lun_count)]
    for i in range(lun_count):
        start = (i * stripe_count) % len(disks)
        group = []
        for k in range(stripe_count):
            group.append(disks[(start + k) % len(disks)])
        groups.append(group)
    return groups


def build_plan(
    vg_name: str,
    disks: list[str],
    lun_count: int,
    size_bytes: int,
    raid_type: str,
    stripe_count: int,
    share_all: bool,
    extend_bytes: int,
    reverse_extend: bool,
    use_sudo: bool,
    regionsize: str,
    maxrecoveryrate: str,
    stripesize: str,
    make_pv_vg: bool,
    vg_exists: bool,
) -> LabPlan:
    groups = assign_disks_to_luns(disks, lun_count, stripe_count, share_all)
    luns = [
        LunSpec(
            name=lun_lv_name(i + 1),
            size_bytes=size_bytes,
            raid_type=raid_type,
            stripe_count=stripe_count,
            disks=groups[i] if i < len(groups) else [],
            extend_bytes=extend_bytes,
            reverse_extend_pvs=reverse_extend,
        )
        for i in range(lun_count)
    ]
    return LabPlan(
        vg_name=vg_name.strip() or "fr",
        luns=luns,
        disks_all=list(disks),
        use_sudo=use_sudo,
        regionsize=regionsize.strip() or "1G",
        maxrecoveryrate=maxrecoveryrate.strip() or "1000",
        stripesize=stripesize.strip(),
        make_pv_vg=make_pv_vg,
        vg_exists=vg_exists,
    )


def lvcreate_cmd(plan: LabPlan, lun: LunSpec) -> str:
    pre = sudo_prefix(plan.use_sudo)
    parts = [
        f"{pre}lvcreate -y",
        f"--type {lun.raid_type}",
        f"--regionsize={plan.regionsize}",
        f"--maxrecoveryrate {plan.maxrecoveryrate}",
    ]
    if plan.stripesize:
        parts.append(f"--stripesize {plan.stripesize}")
    parts.extend(
        [
            f"-i {lun.stripe_count}",
            f"-L {lun.size_bytes}.0b",
            f"-n {lun.name}",
            plan.vg_name,
            *lun.disks,
            "2>&1",
        ]
    )
    return " ".join(parts)


def lvextend_cmd(plan: LabPlan, lun: LunSpec) -> str:
    pre = sudo_prefix(plan.use_sudo)
    pvs = list(reversed(lun.disks)) if lun.reverse_extend_pvs else list(lun.disks)
    return (
        f"{pre}lvextend -y -L {lun.extend_bytes}b "
        f"{plan.vg_name}/{lun.name} {' '.join(pvs)} 2>&1"
    )


def setup_commands(plan: LabPlan) -> list[str]:
    if not plan.make_pv_vg or not plan.disks_all:
        return []
    pre = sudo_prefix(plan.use_sudo)
    cmds = [f"{pre}pvcreate -y {' '.join(plan.disks_all)} 2>&1"]
    if plan.vg_exists:
        cmds.append(f"{pre}vgextend -y {plan.vg_name} {' '.join(plan.disks_all)} 2>&1")
    else:
        cmds.append(f"{pre}vgcreate {plan.vg_name} {' '.join(plan.disks_all)} 2>&1")
    return cmds


def lun_commands(plan: LabPlan, lun: LunSpec) -> list[str]:
    cmds = [lvcreate_cmd(plan, lun)]
    if lun.extend_bytes > lun.size_bytes:
        cmds.append(lvextend_cmd(plan, lun))
    return cmds


def verify_commands(plan: LabPlan) -> list[str]:
    cmds = list(VERIFY_COMMANDS)
    if not plan.use_sudo:
        cmds = [c.replace("sudo ", "", 1) if c.startswith("sudo ") else c for c in cmds]
    extra = []
    for lun in plan.luns:
        extra.append(
            f"{sudo_prefix(plan.use_sudo)}lvs -a -o+devices,segtype,stripes "
            f"{plan.vg_name}/{lun.name} 2>&1"
        )
    return cmds + extra


def all_mutate_commands(plan: LabPlan) -> list[str]:
    cmds = setup_commands(plan)
    for lun in plan.luns:
        cmds.extend(lun_commands(plan, lun))
    return cmds


def command_groups(plan: LabPlan) -> list[tuple[str, list[str]]]:
    groups: list[tuple[str, list[str]]] = []
    setup = setup_commands(plan)
    if setup:
        groups.append(("PV/VG", setup))
    for lun in plan.luns:
        groups.append((lun.name, lun_commands(plan, lun)))
    return groups


def script_text(commands: Iterable[str]) -> str:
    lines = ["set -e"]
    lines.extend(commands)
    return "\n".join(lines) + "\n"


def under_the_hood(plan: LabPlan) -> str:
    first = plan.luns[0].name if plan.luns else lun_lv_name(1)
    lines = [
        "Что происходит под капотом",
        "===========================",
        "",
        "Цель — лабораторная VM (VG fr, диски /dev/mapper/36000c29…).",
        "Железо ENGINE-0 (VG DDP0/DDP1, 358ce38ee…) недоступно; дампы 5.Disks",
        "нужны только как образец, где смотреть страйп.",
        "",
        "Где видно, на каком диске какая нога страйпа (снимать с VM)",
        "----------------------------------------------------------",
        "1. ls /dev/mapper",
        "   Имена VG-LV_rimage_N и VG-LV_rmeta_N. N — номер ноги RAID.",
        "   Сами по себе имена НЕ показывают физический диск.",
        "",
        "2. lsblk (дерево)",
        "   sdX → /dev/mapper/36000c29… (mpath) → VG-LV_rimage_N → VG-LV",
        "   Это и есть раскладка: какой WWID держит rimage_N.",
        "",
        "3. Точнее на VM:",
        "   lvs -a -o+devices,segtype,stripes,stripesize,regionsize",
        "   lvdisplay -m",
        "   dmsetup table / dmsetup ls --tree",
        "   Кнопка «Проверить» гоняет их по SSH, если сессия открыта.",
        "",
        "Образцы команд с VM (имена LV в образце были ddp01; здесь DataStoreN_SSD):",
        f"  {SAMPLE_LVCREATE}",
        f"  {SAMPLE_LVEXTEND}",
        "  lvextend намеренно получает PV в обратном порядке относительно lvcreate.",
        "",
        "Что сделает lvcreate --type raid0 -i N",
        "--------------------------------------",
        "На каждом указанном PV: rmeta_k (~4M служебные) + rimage_k (данные ноги).",
        "Сверху RAID LV. В /dev/mapper появятся:",
        f"  {plan.vg_name}-{first}, {plan.vg_name}-{first}_rimage_0.., "
        f"{plan.vg_name}-{first}_rmeta_0..",
        "",
        "Ожидаемая раскладка по порядку PV в lvcreate",
        "--------------------------------------------",
    ]
    if not plan.luns:
        lines.append("  (нет LUN)")
    for lun in plan.luns:
        lines.append(f"{lun.name}  {format_gb_tb(lun.size_bytes)}  {lun.raid_type}  -i {lun.stripe_count}")
        if not lun.disks:
            lines.append("  нет дисков")
            continue
        for idx, disk in enumerate(lun.disks):
            lines.append(f"  rimage_{idx} / rmeta_{idx}  →  {disk}")
        if lun.extend_bytes > lun.size_bytes:
            order = "обратный" if lun.reverse_extend_pvs else "тот же"
            lines.append(
                f"  lvextend до {format_gb_tb(lun.extend_bytes)}  (порядок PV: {order})"
            )
        lines.append("")
    lines.extend(
        [
            "Последовательность на реальной системе",
            "--------------------------------------",
            "pvcreate → vgcreate (или vgextend) → lvcreate [--type raid0 -i N] →",
            "необязательно lvextend → проверка lsblk / lvs -a -o+devices / dmsetup.",
            "",
            "После создания сравните дерево lsblk с таблицей выше:",
            "номер rimage_N должен совпасть с порядком PV, переданных в lvcreate.",
        ]
    )
    return "\n".join(lines)


class SshError(RuntimeError):
    pass


class SshClient:
    def __init__(self) -> None:
        self._client: object | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(
        self,
        host: str,
        port: int,
        username: str,
        password: str | None,
        key_filename: str | None,
    ) -> None:
        if paramiko is None:
            raise SshError("Нет модуля paramiko. Установите:  pip install paramiko")
        self.close()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict = {
            "hostname": host,
            "port": port,
            "username": username,
            "timeout": 20,
            "allow_agent": True,
            "look_for_keys": not key_filename,
        }
        if key_filename:
            kwargs["key_filename"] = key_filename
        if password:
            kwargs["password"] = password
        try:
            client.connect(**kwargs)
        except Exception as exc:
            raise SshError(f"{type(exc).__name__}: {exc}") from exc
        self._client = client

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def run(self, command: str, timeout: int = 300) -> tuple[int, str, str]:
        if self._client is None:
            raise SshError("SSH не подключён")
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def run_script(self, script: str, timeout: int = 600) -> tuple[int, str, str]:
        return self.run(_bash_lc(script), timeout)

    def fetch_mapper_disks(self) -> list[str]:
        code, out, err = self.run(FETCH_MAPPER_CMD, timeout=30)
        if code != 0 and not out.strip():
            raise SshError(err.strip() or f"код {code}")
        disks = []
        for line in out.splitlines():
            path = line.strip()
            if NAA_MAPPER_RE.match(path) and path not in disks:
                disks.append(path)
        return disks


def _bash_lc(script: str) -> str:
    # Одна SSH-сессия, команды подряд, без лишней паузы.
    import shlex

    return "bash -lc " + shlex.quote(script)


class LunLabApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=8)
        self.pack(fill="both", expand=True)
        self.ssh = SshClient()
        self._busy = False
        self._build()
        self.refresh_preview()
        self._log_paramiko_status()

    def _build(self) -> None:
        self._build_ssh()
        self._build_plan()
        self._build_actions()
        self._build_panes()
        self._build_status()

    def _build_ssh(self) -> None:
        box = tk.LabelFrame(self, text="SSH", font=("Segoe UI", 10, "bold"), padx=8, pady=6)
        box.pack(fill="x")

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Хост").pack(side="left")
        self.host_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.host_var, width=22).pack(side="left", padx=(4, 10))
        ttk.Label(row, text="Порт").pack(side="left")
        self.port_var = tk.StringVar(value="22")
        ttk.Entry(row, textvariable=self.port_var, width=6).pack(side="left", padx=(4, 10))
        ttk.Label(row, text="Пользователь").pack(side="left")
        self.user_var = tk.StringVar(value="root")
        ttk.Entry(row, textvariable=self.user_var, width=16).pack(side="left", padx=(4, 10))

        self.auth_var = tk.StringVar(value="password")
        ttk.Radiobutton(row, text="Пароль", variable=self.auth_var, value="password", command=self._toggle_auth).pack(
            side="left", padx=(8, 0)
        )
        ttk.Radiobutton(row, text="Ключ", variable=self.auth_var, value="key", command=self._toggle_auth).pack(
            side="left"
        )

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, text="Пароль").pack(side="left")
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(row2, textvariable=self.password_var, show="*", width=22)
        self.password_entry.pack(side="left", padx=(4, 10))
        ttk.Label(row2, text="Файл ключа").pack(side="left")
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(row2, textvariable=self.key_var, width=36)
        self.key_entry.pack(side="left", padx=(4, 4), fill="x", expand=True)
        ttk.Button(row2, text="…", width=3, command=self._browse_key).pack(side="left")
        ttk.Button(row2, text="Подключить", command=self.connect_ssh).pack(side="left", padx=(10, 4))
        ttk.Button(row2, text="Проверить SSH", command=self.test_ssh).pack(side="left", padx=(0, 4))
        ttk.Button(row2, text="Отключить", command=self.disconnect_ssh).pack(side="left")
        self._toggle_auth()

        if paramiko is None:
            ttk.Label(
                box,
                text="paramiko не установлен — доступен только dry-run.  pip install paramiko",
                foreground="darkorange",
            ).pack(anchor="w", pady=(4, 0))

    def _build_plan(self) -> None:
        box = tk.LabelFrame(self, text="LUN / диски", font=("Segoe UI", 10, "bold"), padx=8, pady=6)
        box.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="VG").pack(side="left")
        self.vg_var = tk.StringVar(value="fr")
        ttk.Entry(row, textvariable=self.vg_var, width=10).pack(side="left", padx=(4, 12))
        ttk.Label(row, text="Число LUN").pack(side="left")
        self.count_var = tk.StringVar(value="2")
        ttk.Entry(row, textvariable=self.count_var, width=5).pack(side="left", padx=(4, 12))
        ttk.Label(row, text="Размер").pack(side="left")
        self.size_var = tk.StringVar(value="0.1")
        ttk.Entry(row, textvariable=self.size_var, width=10).pack(side="left", padx=(4, 4))
        self.unit_var = tk.StringVar(value="ГБ")
        ttk.Combobox(
            row, textvariable=self.unit_var, values=[u[0] for u in SIZE_UNITS], width=6, state="readonly"
        ).pack(side="left")
        self.size_lbl = ttk.Label(row, text="")
        self.size_lbl.pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(4, 0))
        ttk.Label(row2, text="RAID").pack(side="left")
        self.raid_var = tk.StringVar(value="raid0")
        ttk.Combobox(row2, textvariable=self.raid_var, values=RAID_TYPES, width=8, state="readonly").pack(
            side="left", padx=(4, 12)
        )
        ttk.Label(row2, text="Страйп -i").pack(side="left")
        self.stripes_var = tk.StringVar(value="2")
        ttk.Entry(row2, textvariable=self.stripes_var, width=5).pack(side="left", padx=(4, 12))
        ttk.Label(row2, text="regionsize").pack(side="left")
        self.region_var = tk.StringVar(value="1G")
        ttk.Entry(row2, textvariable=self.region_var, width=6).pack(side="left", padx=(4, 12))
        ttk.Label(row2, text="stripesize (пусто = default)").pack(side="left")
        self.stripesize_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.stripesize_var, width=8).pack(side="left", padx=(4, 12))
        ttk.Label(row2, text="lvextend до (0 = нет)").pack(side="left")
        self.extend_var = tk.StringVar(value="0")
        ttk.Entry(row2, textvariable=self.extend_var, width=8).pack(side="left", padx=(4, 4))
        ttk.Label(row2, text="тех же единиц").pack(side="left")

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(4, 0))
        self.sudo_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text="sudo", variable=self.sudo_var, command=self.refresh_preview).pack(side="left")
        self.make_vg_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="pvcreate + vgcreate", variable=self.make_vg_var, command=self.refresh_preview).pack(
            side="left", padx=(8, 0)
        )
        self.vg_exists_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row3, text="VG уже есть → vgextend", variable=self.vg_exists_var, command=self.refresh_preview
        ).pack(side="left", padx=(8, 0))
        self.share_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row3, text="Все LUN на одних и тех же дисках", variable=self.share_var, command=self.refresh_preview
        ).pack(side="left", padx=(8, 0))
        self.reverse_ext_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            row3,
            text="Обратный порядок PV на lvextend (как в образце, намеренно)",
            variable=self.reverse_ext_var,
            command=self.refresh_preview,
        ).pack(side="left", padx=(8, 0))

        disks_row = ttk.Frame(box)
        disks_row.pack(fill="both", expand=True, pady=(6, 0))
        left = ttk.Frame(disks_row)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Диски VM /dev/mapper/36000c29… (по одному в строке, # комментарий)").pack(anchor="w")
        self.disks_text = tk.Text(left, height=7, font=("Consolas", 9), wrap="none")
        self.disks_text.pack(fill="both", expand=True)
        self.disks_text.insert("1.0", "\n".join(load_placeholder_disks()) + "\n")
        self.disks_text.bind("<KeyRelease>", lambda _e: self.refresh_preview())

        btns = ttk.Frame(disks_row)
        btns.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(btns, text="Диски VM 36000c29…", command=self.load_placeholders).pack(fill="x", pady=2)
        ttk.Button(btns, text="С хоста (SSH)", command=self.fetch_disks).pack(fill="x", pady=2)
        ttk.Button(btns, text="Дампа ENGINE-0 (не VM)", command=self.load_iron_dump).pack(fill="x", pady=2)

        for var in (
            self.vg_var,
            self.count_var,
            self.size_var,
            self.unit_var,
            self.raid_var,
            self.stripes_var,
            self.region_var,
            self.stripesize_var,
            self.extend_var,
        ):
            var.trace_add("write", lambda *_a: self.refresh_preview())

    def _build_actions(self) -> None:
        box = tk.LabelFrame(self, text="Режим", font=("Segoe UI", 10, "bold"), padx=8, pady=6)
        box.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.mode_var = tk.StringVar(value="dry-run")
        ttk.Radiobutton(row, text="Dry-run (не выполнять SSH)", variable=self.mode_var, value="dry-run").pack(
            side="left"
        )
        ttk.Radiobutton(row, text="Медленно (по LUN, ждать завершения)", variable=self.mode_var, value="slow").pack(
            side="left", padx=(12, 0)
        )
        ttk.Radiobutton(row, text="Пакет (все команды подряд в одной сессии)", variable=self.mode_var, value="batch").pack(
            side="left", padx=(12, 0)
        )

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="Сгенерировать", command=self.refresh_preview).pack(side="left")
        ttk.Button(row2, text="Копировать команды", command=self.copy_commands).pack(side="left", padx=6)
        ttk.Button(row2, text="Выполнить", command=self.execute).pack(side="left", padx=6)
        ttk.Button(row2, text="Проверить", command=self.verify).pack(side="left", padx=6)

    def _build_panes(self) -> None:
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(8, 0))

        cmd_frame = ttk.Frame(panes)
        ttk.Label(cmd_frame, text="Команды (lvcreate / lvextend / анализ)").pack(anchor="w")
        self.cmd_text = tk.Text(cmd_frame, font=("Consolas", 9), wrap="none", height=16)
        cmd_scroll = ttk.Scrollbar(cmd_frame, command=self.cmd_text.yview)
        self.cmd_text.configure(yscrollcommand=cmd_scroll.set)
        self.cmd_text.pack(side="left", fill="both", expand=True)
        cmd_scroll.pack(side="right", fill="y")
        panes.add(cmd_frame, weight=3)

        right = ttk.Panedwindow(panes, orient="vertical")
        hood_frame = ttk.Frame(right)
        ttk.Label(hood_frame, text="Под капотом / раскладка страйпа").pack(anchor="w")
        self.hood_text = tk.Text(hood_frame, font=("Consolas", 9), wrap="word", height=10)
        hood_scroll = ttk.Scrollbar(hood_frame, command=self.hood_text.yview)
        self.hood_text.configure(yscrollcommand=hood_scroll.set)
        self.hood_text.pack(side="left", fill="both", expand=True)
        hood_scroll.pack(side="right", fill="y")
        right.add(hood_frame, weight=2)

        out_frame = ttk.Frame(right)
        ttk.Label(out_frame, text="Журнал SSH / статус (время · событие)").pack(anchor="w")
        self.out_text = tk.Text(out_frame, font=("Consolas", 9), wrap="none", height=10)
        out_scroll = ttk.Scrollbar(out_frame, command=self.out_text.yview)
        self.out_text.configure(yscrollcommand=out_scroll.set)
        self.out_text.pack(side="left", fill="both", expand=True)
        out_scroll.pack(side="right", fill="y")
        right.add(out_frame, weight=2)
        panes.add(right, weight=2)

    def _build_status(self) -> None:
        self.status_var = tk.StringVar(value="Dry-run. Команды можно копировать и запускать вручную.")
        ttk.Label(self, textvariable=self.status_var).pack(fill="x", pady=(6, 0))

    def _toggle_auth(self) -> None:
        key_mode = self.auth_var.get() == "key"
        self.key_entry.configure(state=("normal" if key_mode else "disabled"))
        self.password_entry.configure(state=("normal" if not key_mode else "disabled"))

    def _browse_key(self) -> None:
        path = filedialog.askopenfilename(parent=self.master, title="Ключ SSH")
        if path:
            self.key_var.set(path)
            self.auth_var.set("key")
            self._toggle_auth()

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def _append_out(self, value: str) -> None:
        self.out_text.insert("end", value)
        if not value.endswith("\n"):
            self.out_text.insert("end", "\n")
        self.out_text.see("end")

    def _log(self, message: str) -> None:
        """Timestamped line in the SSH journal + status bar. Call from the UI thread."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self._append_out(f"[{ts}] {message}")
        self.status_var.set(message[:240])

    def _log_async(self, message: str) -> None:
        self.after(0, lambda m=message: self._log(m))

    def _log_paramiko_status(self) -> None:
        if paramiko is None:
            self._log("paramiko: НЕ импортирован. Установите: pip install paramiko")
            self._log("SSH недоступен, пока нет paramiko (это не тихий отказ).")
        else:
            ver = getattr(paramiko, "__version__", "?")
            self._log(f"paramiko: импортирован, версия {ver}")

    def current_plan(self) -> LabPlan:
        disks = parse_disk_list(self.disks_text.get("1.0", "end"))
        size_b = size_to_bytes(self.size_var.get(), self.unit_var.get())
        ext_b = size_to_bytes(self.extend_var.get(), self.unit_var.get())
        return build_plan(
            vg_name=self.vg_var.get(),
            disks=disks,
            lun_count=max(0, parse_int(self.count_var.get(), 0)),
            size_bytes=max(0, size_b),
            raid_type=self.raid_var.get().strip() or "raid0",
            stripe_count=max(1, parse_int(self.stripes_var.get(), 2)),
            share_all=self.share_var.get(),
            extend_bytes=max(0, ext_b),
            reverse_extend=self.reverse_ext_var.get(),
            use_sudo=self.sudo_var.get(),
            regionsize=self.region_var.get(),
            maxrecoveryrate="1000",
            stripesize=self.stripesize_var.get(),
            make_pv_vg=self.make_vg_var.get(),
            vg_exists=self.vg_exists_var.get(),
        )

    def plan_warnings(self, plan: LabPlan) -> list[str]:
        warns: list[str] = []
        need = min_disks_for_raid(plan.luns[0].raid_type) if plan.luns else 2
        stripes = plan.luns[0].stripe_count if plan.luns else 0
        if stripes < need:
            warns.append(f"-i={stripes} меньше минимума {need} для {plan.luns[0].raid_type}")
        if len(plan.disks_all) < stripes:
            warns.append(f"дисков {len(plan.disks_all)} < страйпа -i {stripes}")
        for lun in plan.luns:
            if len(lun.disks) != lun.stripe_count:
                warns.append(f"{lun.name}: дисков {len(lun.disks)}, ожидалось {lun.stripe_count}")
            if lun.size_bytes <= 0:
                warns.append(f"{lun.name}: нулевой размер")
        return warns

    def refresh_preview(self) -> None:
        plan = self.current_plan()
        self.size_lbl.configure(text=format_gb_tb(size_to_bytes(self.size_var.get(), self.unit_var.get())))
        mutate = all_mutate_commands(plan)
        verify = verify_commands(plan)
        chunks = [
            "# --- создание / расширение ---",
            *mutate,
            "",
            "# --- проверка раскладки страйпа ---",
            *verify,
        ]
        self._set_text(self.cmd_text, "\n".join(chunks) + "\n")
        hood = under_the_hood(plan)
        warns = self.plan_warnings(plan)
        if warns:
            hood = "Предупреждения:\n" + "\n".join(f"  • {w}" for w in warns) + "\n\n" + hood
        self._set_text(self.hood_text, hood)
        n = len(plan.luns)
        self.status_var.set(
            f"{'SSH подключён' if self.ssh.connected else 'SSH нет'}  |  "
            f"LUN: {n}  |  дисков: {len(plan.disks_all)}  |  "
            f"режим: {self.mode_var.get()}"
        )

    def copy_commands(self) -> None:
        text = self.cmd_text.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Команды скопированы в буфер.")

    def load_placeholders(self) -> None:
        disks = load_placeholder_disks()
        self._set_text(self.disks_text, "\n".join(disks) + "\n")
        self.refresh_preview()

    def load_iron_dump(self) -> None:
        disks = load_iron_dump_disks()
        if not disks:
            messagebox.showinfo("Диски", "В 5.Disks/pvs нет mapper-устройств.")
            return
        self._set_text(self.disks_text, "\n".join(disks) + "\n")
        self.refresh_preview()

    def _ssh_target_or_log_why_not(self) -> tuple[str, int, str, str | None, str | None] | None:
        """Log paramiko/host/auth; return connect args or None if we must not attempt SSH."""
        if paramiko is None:
            self._log("paramiko: НЕ импортирован. Установите: pip install paramiko")
            self._log("подключение не запускается — нет paramiko (это не тихий отказ).")
            return None
        ver = getattr(paramiko, "__version__", "?")
        self._log(f"paramiko: импортирован, версия {ver}")

        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        port = parse_int(self.port_var.get(), 22) or 22
        auth = self.auth_var.get().strip() or "password"
        password = self.password_var.get() if auth == "password" else None
        key = self.key_var.get().strip() if auth == "key" else None

        if auth == "key":
            auth_desc = f"ключ, файл={key or '(не указан)'}"
        else:
            auth_desc = "пароль" + (" (задан)" if password else " (пустой)")
        self._log(
            f"параметры: host={host or '(пусто)'}  port={port}  user={user or '(пусто)'}  auth={auth_desc}"
        )

        if not host:
            self._log("хост пустой — подключение не запускается")
            return None
        if not user:
            self._log("пользователь пустой — подключение не запускается")
            return None
        if auth == "password" and not password:
            self._log("пароль пустой — подключение не запускается (переключитесь на ключ или введите пароль)")
            return None
        if auth == "key" and not key:
            self._log("файл ключа не указан — подключение не запускается")
            return None
        if auth == "key" and key and not Path(key).is_file():
            self._log(f"файл ключа не найден: {key}")
            return None
        return host, port, user, password, key

    def connect_ssh(self) -> None:
        self._run_ssh_connect(probe=False)

    def test_ssh(self) -> None:
        """Только SSH (uname -a), без LVM. Работает и при включённом dry-run."""
        self._run_ssh_connect(probe=True)

    def _run_ssh_connect(self, probe: bool) -> None:
        if self._busy:
            self._log("SSH: занято другой операцией, подождите")
            return
        kind = "проверка соединения (uname -a, без LVM)" if probe else "подключение"
        self._log(f"SSH: {kind}")
        if probe:
            self._log("dry-run на тест SSH не влияет — соединение будет открыто")
        target = self._ssh_target_or_log_why_not()
        if target is None:
            return
        host, port, user, password, key = target
        self._log(f"подключаюсь к {host}:{port} …")
        self._busy = True

        def work() -> None:
            try:
                self.ssh.connect(host, port, user, password, key)
                self._log_async(f"SSH: успех — сессия {user}@{host}:{port}")
                if probe:
                    self._log_async("SSH: тест — выполняю uname -a")
                    code, out, err = self.ssh.run("uname -a", timeout=30)
                    text = (out or err or "").strip() or "(пустой вывод)"
                    self._log_async(f"SSH: тест uname -a  exit={code}  {text}")
                    if code != 0:
                        self._log_async(f"SSH: тест завершился с кодом {code}")
            except Exception as exc:
                orig = exc.__cause__ if isinstance(exc, SshError) and exc.__cause__ is not None else exc
                self._log_async(f"SSH: ошибка {type(orig).__name__}: {orig}")
                self._log_async(traceback.format_exc())
            finally:
                self.after(0, self._done)

        threading.Thread(target=work, daemon=True).start()

    def disconnect_ssh(self) -> None:
        was = self.ssh.connected
        self.ssh.close()
        if was:
            self._log("SSH отключён")
        else:
            self._log("SSH и так не был подключён")

    def fetch_disks(self) -> None:
        if not self.ssh.connected:
            self._log("SSH не подключён — список дисков с хоста не запрашивается")
            messagebox.showinfo(
                "Диски",
                "SSH не подключён — подставлены диски лабораторной VM (36000c29…).\n"
                "Нажмите «Проверить SSH» / «Подключить», затем «С хоста».",
            )
            self.load_placeholders()
            return

        def work() -> None:
            try:
                self._log_async("SSH: запрос /dev/mapper с хоста")
                disks = self.ssh.fetch_mapper_disks()
            except Exception as exc:
                orig = exc.__cause__ if isinstance(exc, SshError) and exc.__cause__ is not None else exc
                self._log_async(f"SSH: ошибка {type(orig).__name__}: {orig}")
                self._log_async(traceback.format_exc())
                return

            def apply() -> None:
                if disks:
                    self._set_text(self.disks_text, "\n".join(disks) + "\n")
                    self.refresh_preview()
                    self._log(f"получено mapper-устройств: {len(disks)}")
                else:
                    self._log("на хосте не найдено /dev/mapper/3… или 6…")
                    messagebox.showwarning("Диски", "На хосте не найдено /dev/mapper/3… или 6…")

            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def execute(self) -> None:
        if self._busy:
            self._log("SSH: занято другой операцией, подождите")
            return
        plan = self.current_plan()
        warns = self.plan_warnings(plan)
        if any("нулевой" in w for w in warns):
            messagebox.showerror("Выполнить", "Исправьте размер LUN.")
            return
        mode = self.mode_var.get()
        cmds = all_mutate_commands(plan)
        if not cmds:
            messagebox.showinfo("Выполнить", "Нет команд для выполнения.")
            return
        self.refresh_preview()
        if mode == "dry-run":
            self._log("SSH пропущен: включён dry-run — соединение не открывается, LVM не выполняется")
            self._log("Сгенерированные команды (не отправлены):")
            for c in cmds:
                self._append_out(c)
            return
        if not self.ssh.connected:
            self._log(
                "SSH не подключён — выполнение отменено. "
                "Сначала «Проверить SSH» / «Подключить», либо оставьте dry-run."
            )
            messagebox.showerror("Выполнить", "Сначала подключите SSH или оставьте режим Dry-run.")
            return
        host = self.host_var.get().strip()
        if not messagebox.askokcancel(
            "Подтверждение",
            f"Выполнить {len(cmds)} команд LVM на {host}?\n"
            f"Режим: {mode}. Это изменит VG {plan.vg_name} на удалённой машине.",
        ):
            self._log("выполнение отменено пользователем")
            return
        self._busy = True
        self._log(f"SSH: выполняю LVM, режим={mode}, команд={len(cmds)}")

        def work() -> None:
            try:
                if mode == "batch":
                    code, out, err = self.ssh.run_script(script_text(cmds))
                    self.after(0, lambda: self._show_result("ПАКЕТ", "\n".join(cmds), code, out, err))
                else:
                    for title, group in command_groups(plan):
                        for cmd in group:
                            code, out, err = self.ssh.run(cmd)
                            self.after(0, lambda t=title, c=cmd, k=code, o=out, e=err: self._show_result(t, c, k, o, e))
                            if code != 0:
                                self.after(0, lambda t=title: self._log(f"ошибка на {t}"))
                                return
                        time.sleep(0.3)
                    self.after(0, lambda: self._log("готово (медленный режим)"))
            except Exception as exc:
                orig = exc.__cause__ if isinstance(exc, SshError) and exc.__cause__ is not None else exc
                self._log_async(f"SSH: ошибка {type(orig).__name__}: {orig}")
                self._log_async(traceback.format_exc())
            finally:
                self.after(0, self._done)

        threading.Thread(target=work, daemon=True).start()

    def verify(self) -> None:
        if self._busy:
            self._log("SSH: занято другой операцией, подождите")
            return
        plan = self.current_plan()
        cmds = verify_commands(plan)
        self.refresh_preview()
        if self.mode_var.get() == "dry-run":
            self._log("SSH пропущен: включён dry-run — проверка по SSH не выполняется")
            self._log("Команды проверки (не отправлены):")
            for c in cmds:
                self._append_out(c)
            return
        if not self.ssh.connected:
            self._log("SSH не подключён — проверка не запущена. Сначала «Проверить SSH» / «Подключить».")
            self._log("Команды проверки (не отправлены):")
            for c in cmds:
                self._append_out(c)
            return
        if not messagebox.askokcancel("Проверить", "Снять lsblk/lvs/dmsetup с хоста по SSH?"):
            self._log("проверка отменена пользователем")
            return
        self._busy = True
        self._log("SSH: снимаю lsblk/lvs/dmsetup")

        def work() -> None:
            try:
                code, out, err = self.ssh.run_script(script_text(cmds))
                self.after(0, lambda: self._show_result("ПРОВЕРКА", "\n".join(cmds), code, out, err))
            except Exception as exc:
                orig = exc.__cause__ if isinstance(exc, SshError) and exc.__cause__ is not None else exc
                self._log_async(f"SSH: ошибка {type(orig).__name__}: {orig}")
                self._log_async(traceback.format_exc())
            finally:
                self.after(0, self._done)

        threading.Thread(target=work, daemon=True).start()

    def _show_result(self, title: str, cmd: str, code: int, out: str, err: str) -> None:
        self._log(f"===== {title}  exit={code} =====")
        self._append_out(cmd)
        if out.strip():
            self._append_out(out.rstrip())
        if err.strip():
            self._append_out("[stderr]\n" + err.rstrip())
        if code == 0:
            self.status_var.set(f"{title}: ok")
        else:
            self.status_var.set(f"{title}: ошибка {code}")

    def _done(self) -> None:
        self._busy = False


def main() -> None:
    root = tk.Tk()
    root.title("DDP / LUN — лаборатория (SSH / dry-run)")
    root.geometry("1280x820")
    root.minsize(960, 640)
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass
    LunLabApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
