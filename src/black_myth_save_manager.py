from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk


APP_NAME = "黑猴存档管理"
APP_VERSION = "1.4.5"
SETTINGS_VERSION = 1
DEFAULT_ARCHIVE_ROOT: Path | None = None
USER_SETTING_SAVE = "UserSettingSaveGame.sav"
STARTUPINFO = None
if os.name == "nt":
    STARTUPINFO = subprocess.STARTUPINFO()
    STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW


def asset_path(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / "assets" / name


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "BlackMythSaveManager"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_root_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "BlackMythSaveManager" / "Backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_file() -> Path:
    return app_data_dir() / "settings.json"


def load_settings() -> dict:
    path = settings_file()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    path = settings_file()
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def natural_key(value: str):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def format_size(size: int) -> str:
    value = float(max(0, size))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def format_time(timestamp: float) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def safe_relative(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
        return str(rel)
    except (OSError, ValueError):
        return str(path)


def parse_libraryfolders(path: Path) -> set[Path]:
    libraries: set[Path] = set()
    if not path.is_file():
        return libraries
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return libraries
    for match in re.finditer(r'"path"\s*"([^"]+)"', text, flags=re.IGNORECASE):
        raw = match.group(1).replace("\\\\", "\\")
        libraries.add(Path(os.path.expandvars(raw)))
    for match in re.finditer(r'^\s*"\d+"\s*"([A-Za-z]:[^"]+)"', text, flags=re.MULTILINE):
        raw = match.group(1).replace("\\\\", "\\")
        libraries.add(Path(os.path.expandvars(raw)))
    return libraries


def registry_steam_paths() -> set[Path]:
    if os.name != "nt":
        return set()
    paths: set[Path] = set()
    try:
        import winreg
    except ImportError:
        return paths

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ("SteamPath", "InstallPath")),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", ("InstallPath", "SteamPath")),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", ("InstallPath", "SteamPath")),
    )
    for hive, key_name, value_names in locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for value_name in value_names:
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                        if value:
                            paths.add(Path(os.path.expandvars(str(value))))
                    except OSError:
                        continue
        except OSError:
            continue
    return paths


def discover_steam_libraries() -> set[Path]:
    libraries: set[Path] = set(registry_steam_paths())
    for steam_path in list(libraries):
        libraries.add(steam_path / "steamapps")
        libraries.update(parse_libraryfolders(steam_path / "steamapps" / "libraryfolders.vdf"))
        libraries.update(parse_libraryfolders(steam_path / "config" / "libraryfolders.vdf"))

    drive_mask = 0
    if os.name == "nt":
        try:
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        except Exception:
            drive_mask = 0
    for index in range(26):
        if drive_mask and not (drive_mask & (1 << index)):
            continue
        drive = Path(f"{chr(65 + index)}:\\")
        if not drive.exists():
            continue
        libraries.update(
            {
                drive / "SteamLibrary" / "steamapps",
                drive / "Steam" / "steamapps",
                drive / "Games" / "SteamLibrary" / "steamapps",
                drive / "Games" / "Steam" / "steamapps",
            }
        )
    return {path for path in libraries if path.exists()}


def discover_savegames_roots() -> list[Path]:
    roots: dict[str, Path] = {}
    candidates: set[Path] = set()
    for library in discover_steam_libraries():
        candidates.add(library / "common" / "BlackMythWukong" / "b1" / "Saved" / "SaveGames")
        candidates.add(library / "common" / "BlackMythWukong" / "b1" / "Saved" / "SaveGames" / "SaveGames")
    candidates.add(Path(r"E:\SteamLibrary\steamapps\common\BlackMythWukong\b1\Saved\SaveGames"))
    candidates.add(Path(r"C:\Program Files (x86)\Steam\steamapps\common\BlackMythWukong\b1\Saved\SaveGames"))

    for candidate in candidates:
        if not candidate.is_dir():
            continue
        try:
            key = str(candidate.resolve()).casefold()
        except OSError:
            key = str(candidate).casefold()
        roots.setdefault(key, candidate)
    return sorted(roots.values(), key=lambda item: os.path.getmtime(item), reverse=True)


def direct_save_files(folder: Path) -> list[Path]:
    try:
        return sorted(
            (item for item in folder.iterdir() if item.is_file() and item.suffix.casefold() == ".sav"),
            key=lambda item: natural_key(item.name),
        )
    except OSError:
        return []


def is_archive_save_folder(folder: Path) -> bool:
    files = direct_save_files(folder)
    if not files:
        return False
    return any(item.name.casefold().startswith("archivesavefile") for item in files)


def folder_score(folder: Path, depth: int = 0) -> float:
    files = direct_save_files(folder)
    if not files:
        return float("-inf")
    archive_files = [item for item in files if item.name.casefold().startswith("archivesavefile")]
    newest = 0.0
    for item in files:
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            continue
    useful = len(archive_files) if archive_files else len(files)
    return useful * 1000000 + newest - depth * 500000


def detect_actual_save_folder(folder: Path, max_depth: int = 2) -> Path | None:
    folder = Path(folder)
    if not folder.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    try:
        children = [item for item in folder.iterdir() if item.is_dir()]
    except OSError:
        children = []
    for child in children:
        if child.name.startswith(".bmw_"):
            continue
        stack: list[tuple[Path, int]] = [(child, 1)]
        while stack:
            current, depth = stack.pop()
            if is_archive_save_folder(current):
                candidates.append((folder_score(current, depth), current))
            if depth >= max_depth:
                continue
            try:
                for sub in current.iterdir():
                    if sub.is_dir() and not sub.name.startswith(".bmw_"):
                        stack.append((sub, depth + 1))
            except OSError:
                continue

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    if is_archive_save_folder(folder):
        return folder
    return None


def resolve_current_save_folder(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    text = str(path).strip().strip('"')
    if not text:
        return None
    folder = Path(text)
    if not folder.is_dir():
        return None
    detected = detect_actual_save_folder(folder)
    if detected is not None:
        return detected
    return folder


def auto_detect_current_save() -> Path | None:
    for root in discover_savegames_roots():
        detected = detect_actual_save_folder(root)
        if detected is not None:
            return detected
        if root.name.casefold() == "savegames":
            try:
                children = [item for item in root.iterdir() if item.is_dir()]
            except OSError:
                children = []
            if len(children) == 1:
                return children[0]
    return None


@dataclass
class ScanFile:
    path: Path
    name: str
    size: int
    modified: float


@dataclass
class SaveNode:
    path: Path
    name: str
    parent: "SaveNode | None" = None
    children: list["SaveNode"] = field(default_factory=list)
    direct_files: list[ScanFile] = field(default_factory=list)
    total_files: int = 0
    total_size: int = 0
    total_save_dirs: int = 0
    modified: float = 0.0

    @property
    def is_save_dir(self) -> bool:
        return any(item.path.suffix.casefold() == '.sav' for item in self.direct_files)

    @property
    def direct_save_count(self) -> int:
        return sum(1 for item in self.direct_files if item.name.casefold().startswith("archivesavefile"))

    def relative_path(self, root: Path) -> str:
        return safe_relative(self.path, root)


def scan_save_tree(root: Path) -> tuple[SaveNode, dict[str, SaveNode]]:
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    nodes: dict[str, SaveNode] = {}
    errors: list[str] = []

    def onerror(error: OSError) -> None:
        errors.append(str(error))

    for current_text, dir_names, file_names in os.walk(root, topdown=True, onerror=onerror):
        current = Path(current_text)
        dir_names[:] = sorted(
            (name for name in dir_names if not name.startswith(".bmw_")),
            key=natural_key,
        )
        file_names.sort(key=natural_key)
        files: list[ScanFile] = []
        newest = 0.0
        for name in file_names:
            path = current / name
            try:
                stat = path.stat()
            except OSError:
                continue
            newest = max(newest, stat.st_mtime)
            files.append(ScanFile(path=path, name=name, size=stat.st_size, modified=stat.st_mtime))
        try:
            folder_modified = current.stat().st_mtime
        except OSError:
            folder_modified = newest
        nodes[str(current)] = SaveNode(
            path=current,
            name=current.name or str(current),
            direct_files=files,
            modified=max(newest, folder_modified),
        )

    root_node = nodes[str(root)]
    for current_text, node in list(nodes.items()):
        current = Path(current_text)
        if current == root:
            continue
        parent = nodes.get(str(current.parent), root_node)
        node.parent = parent
        parent.children.append(node)

    def aggregate(node: SaveNode) -> tuple[int, int, int]:
        total_files = len(node.direct_files)
        total_size = sum(item.size for item in node.direct_files)
        save_dirs = 1 if node.is_save_dir else 0
        for child in node.children:
            child_files, child_size, child_saves = aggregate(child)
            total_files += child_files
            total_size += child_size
            save_dirs += child_saves
        node.total_files = total_files
        node.total_size = total_size
        node.total_save_dirs = save_dirs
        return total_files, total_size, save_dirs

    aggregate(root_node)
    return root_node, nodes


def backup_current_save(source: Path, destination: Path) -> Path:
    destination = Path(destination)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    folder_name = destination.name or "SaveGames"
    backup_path = backup_root_dir() / f"{folder_name}_{timestamp}"
    counter = 1
    while backup_path.exists():
        backup_path = backup_root_dir() / f"{folder_name}_{timestamp}_{counter}"
        counter += 1
    shutil.copytree(destination, backup_path)
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_archive": str(source),
        "original_current_path": str(destination),
    }
    try:
        (backup_path / "_backup_info.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass
    return backup_path


def is_subpath(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def configure_tk_scaling(root: tk.Tk) -> float:
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except tk.TclError:
        dpi = 96.0
    if dpi <= 0:
        dpi = 96.0
    scale = max(1.0, min(3.0, dpi / 96.0))
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass
    return scale


def copy_tree_in_place(source: Path, destination: Path) -> list[str]:
    warnings: list[str] = []
    for current_text, _dir_names, file_names in os.walk(source):
        current = Path(current_text)
        relative = current.relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in file_names:
            target = target_dir / name
            if name.casefold() == USER_SETTING_SAVE.casefold() and target.exists():
                continue
            shutil.copy2(current / name, target)

    for current_text, dir_names, file_names in os.walk(destination, topdown=False):
        current = Path(current_text)
        relative = current.relative_to(destination)
        for name in file_names:
            if name.casefold() == USER_SETTING_SAVE.casefold():
                continue
            source_file = source / relative / name
            if source_file.is_file():
                continue
            target = current / name
            try:
                target.unlink()
            except OSError as error:
                warnings.append(f"无法删除旧文件 {target}: {error}")
        for name in dir_names:
            source_dir = source / relative / name
            if source_dir.is_dir():
                continue
            target = current / name
            try:
                target.rmdir()
            except OSError as error:
                warnings.append(f"无法删除旧目录 {target}: {error}")
    return warnings


def apply_save_folder(source: Path, destination: Path, allow_locked: bool = False) -> list[str]:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"源存档目录不存在: {source}")
    if not direct_save_files(source):
        raise ValueError("所选目录中没有 .sav 存档文件。")
    if destination.exists() and not destination.is_dir():
        raise NotADirectoryError(f"当前存档路径不是文件夹: {destination}")
    if source == destination:
        raise ValueError("源存档和当前存档是同一个目录，无需覆盖。")
    if is_subpath(destination, source) or is_subpath(source, destination):
        raise ValueError("源存档与当前存档存在包含关系，拒绝执行以避免递归覆盖。")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copytree(source, destination)
        return []

    if allow_locked:
        return copy_tree_in_place(source, destination)

    if destination.name.casefold() == "savegames":
        sync_tree_in_place(source, destination)
        return []

    token = uuid.uuid4().hex[:10]
    temp = destination.parent / f".bmw_new_{destination.name}_{token}"
    old = destination.parent / f".bmw_old_{destination.name}_{token}"
    if temp.exists():
        shutil.rmtree(temp)
    shutil.copytree(source, temp)
    preserved_setting = destination / USER_SETTING_SAVE
    if preserved_setting.is_file():
        shutil.copy2(preserved_setting, temp / USER_SETTING_SAVE)
    moved_old = False
    try:
        os.replace(destination, old)
        moved_old = True
        os.replace(temp, destination)
    except OSError as error:
        if moved_old and old.exists() and not destination.exists():
            os.replace(old, destination)
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        winerror = getattr(error, "winerror", None)
        if allow_locked and (isinstance(error, PermissionError) or winerror in (5, 32, 33)):
            return copy_tree_in_place(source, destination)
        raise
    else:
        shutil.rmtree(old, ignore_errors=True)
        return []


def sync_tree_in_place(source: Path, destination: Path) -> None:
    token = uuid.uuid4().hex[:10]
    staging = destination.parent / f".bmw_stage_{destination.name}_{token}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(source, staging)
    preserved_setting = destination / USER_SETTING_SAVE
    if preserved_setting.is_file():
        shutil.copy2(preserved_setting, staging / USER_SETTING_SAVE)
    try:
        for item in list(destination.iterdir()):
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        for item in staging.iterdir():
            os.replace(item, destination / item.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def is_game_running() -> bool:
    if os.name != "nt":
        return False
    executables = ("b1-Win64-Shipping.exe", "BlackMythWukong.exe")
    for executable in executables:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {executable}", "/NH"],
                capture_output=True,
                text=True,
                errors="ignore",
                startupinfo=STARTUPINFO,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if executable.casefold() in (result.stdout or "").casefold():
            return True
    return False


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def send_paths_to_recycle_bin(paths: list[Path]) -> list[Path]:
    if os.name != "nt":
        return [Path(path) for path in paths]
    existing: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        item = Path(path)
        key = str(item).casefold()
        if key in seen or not item.exists():
            continue
        seen.add(key)
        existing.append(item)
    if not existing:
        return []

    path_buffer = ctypes.create_unicode_buffer("\0".join(str(path) for path in existing) + "\0\0")
    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3
    operation.pFrom = ctypes.cast(path_buffer, ctypes.c_wchar_p)
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    except Exception:
        return existing
    if result == 0 and not operation.fAnyOperationsAborted:
        return []
    return existing


def send_to_recycle_bin(path: Path) -> bool:
    return not send_paths_to_recycle_bin([Path(path)])


def recycle_paths_in_thread(paths: list[Path]) -> list[Path]:
    initialized = False
    if os.name == "nt":
        try:
            result = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
            initialized = result in (0, 1)
        except Exception:
            initialized = False
    try:
        return send_paths_to_recycle_bin(paths)
    finally:
        if initialized:
            try:
                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass


def delete_paths_permanently_in_thread(paths: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as error:
            errors.append(f"{path}: {error}")
    return errors


class BackupPrompt(tk.Toplevel):
    def __init__(self, parent: tk.Misc, source: Path, destination: Path, game_running: bool = False):
        super().__init__(parent)
        self.result: str | None = None
        self.remember = False
        self.title("覆盖前确认")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="即将覆盖当前存档", style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="建议先备份当前存档，若覆盖结果不符合预期可以恢复。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(6, 10 if game_running else 14))
        if game_running:
            ttk.Label(
                frame,
                text="检测到游戏正在运行：允许覆盖；请先回到标题界面，覆盖后重新读取存档。",
                style="Warning.TLabel",
                wraplength=520,
            ).pack(anchor="w", pady=(0, 14))

        info = ttk.Frame(frame)
        info.pack(fill="x")
        info.columnconfigure(1, weight=1)
        ttk.Label(info, text="来源：").grid(row=0, column=0, sticky="nw", pady=2)
        ttk.Label(info, text=str(source), wraplength=500).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(info, text="目标：").grid(row=1, column=0, sticky="nw", pady=2)
        ttk.Label(info, text=str(destination), wraplength=500).grid(row=1, column=1, sticky="w", pady=2)

        self.remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="本次运行不再提醒（记住本次选择，重启后恢复提醒）",
            variable=self.remember_var,
        ).pack(anchor="w", pady=(16, 14))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="直接覆盖", command=lambda: self._finish("direct")).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="备份并覆盖", style="Accent.TButton", command=lambda: self._finish("backup")).pack(side="right")

        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_force()
        self.bind("<Escape>", lambda _event: self._cancel())

    def _finish(self, result: str) -> None:
        self.result = result
        self.remember = bool(self.remember_var.get())
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class SlimProgressDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, total: int, worker_count: int, permanent: bool = False):
        super().__init__(parent)
        self.title("永久删除存档文件" if permanent else "存档瘦身")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.total = max(1, total)
        self.done = 0
        self.worker_count = worker_count

        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="正在永久删除剩余文件…" if permanent else "正在后台执行存档瘦身…",
            style="DialogTitle.TLabel",
        ).pack(anchor="w")
        self.detail_var = tk.StringVar(
            value=f"0 / {total}，线程数：{worker_count}（CPU逻辑线程数的一半）"
        )
        ttk.Label(frame, textvariable=self.detail_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 10))
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=self.total, length=430)
        self.progress.pack(fill="x")
        ttk.Label(frame, text="窗口仍可移动；任务完成前请勿关闭主程序。", style="Muted.TLabel").pack(
            anchor="w", pady=(10, 0)
        )

        self.update_idletasks()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{x}+{y}")

    def update_progress(self, done: int) -> None:
        self.done = max(0, min(done, self.total))
        self.progress["value"] = self.done
        self.detail_var.set(
            f"{self.done} / {self.total}，线程数：{self.worker_count}（CPU逻辑线程数的一半）"
        )
        self.update_idletasks()


class SaveManagerApp:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.runtime_backup_preference: str | None = None
        self.source_root: Path | None = None
        self.root_node: SaveNode | None = None
        self.nodes: dict[str, SaveNode] = {}
        self.tree_items: dict[str, str] = {}
        self.item_nodes: dict[str, SaveNode] = {}
        self.selected_node: SaveNode | None = None
        self.slim_running = False
        self.slim_queue: queue.Queue | None = None
        self.slim_dialog: SlimProgressDialog | None = None
        self.slim_target_root: Path | None = None
        self._suppress_settings = True

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.icon_photo = None
        icon_ico = asset_path("logo.ico")
        icon_png = asset_path("logo.png")
        if icon_ico.is_file():
            try:
                self.root.iconbitmap(default=str(icon_ico))
            except tk.TclError:
                pass
        if icon_png.is_file():
            try:
                self.icon_photo = tk.PhotoImage(file=str(icon_png))
                self.root.iconphoto(True, self.icon_photo)
            except tk.TclError:
                self.icon_photo = None
        self.dpi_scale = configure_tk_scaling(self.root)
        desired_width = min(self._px(1480), max(self._px(980), self.root.winfo_screenwidth() - self._px(40)))
        desired_height = min(self._px(820), max(self._px(560), self.root.winfo_screenheight() - self._px(100)))
        self.root.minsize(min(self._px(980), desired_width), min(self._px(620), desired_height))
        self.root.geometry(f"{desired_width}x{desired_height}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.source_var = tk.StringVar()
        self.current_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在初始化…")
        self.summary_var = tk.StringVar(value="请选择存档库")
        self._build_ui()
        self._configure_style()
        self._load_initial_settings()
        self._suppress_settings = False
        self.root.after_idle(self._set_initial_sash)
        self.root.after(120, self._startup_scan)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")
        try:
            style.configure(".", font=("Microsoft YaHei UI", 10))
            style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
            style.configure("Section.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
            style.configure("Muted.TLabel", foreground="#5d6675", font=("Microsoft YaHei UI", 9))
            style.configure("DialogTitle.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
            style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
            style.configure("Warning.TLabel", foreground="#b45309", font=("Microsoft YaHei UI", 10, "bold"))
            style.configure("Treeview", rowheight=self._px(27))
            style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
            style.configure("Status.TLabel", foreground="#475569")
        except tk.TclError:
            pass


    def _px(self, value: int | float) -> int:
        return max(1, int(round(float(value) * self.dpi_scale)))

    def _set_initial_sash(self) -> None:
        try:
            width = self.paned.winfo_width()
            if width <= 1:
                self.root.after(30, self._set_initial_sash)
                return
            target = max(self._px(820), int(width * 0.64))
            self.paned.sashpos(0, min(target, max(self._px(650), width - self._px(380))))
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=self._px(14))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="保留存档库目录树 · 自动定位 Steam 当前存档 · 覆盖前可选备份",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Button(header, text="打开备份目录", command=self._open_backup_root).grid(row=0, column=1, rowspan=2, sticky="e")

        paths = ttk.LabelFrame(outer, text=" 路径设置 ", padding=(10, 8))
        paths.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="存档库：").grid(row=0, column=0, sticky="w", pady=3)
        source_entry = ttk.Entry(paths, textvariable=self.source_var)
        source_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=3)
        source_entry.bind("<Return>", lambda _event: self._load_source())
        ttk.Button(paths, text="浏览…", command=self._browse_source).grid(row=0, column=2, padx=(0, 6), pady=3)
        ttk.Button(paths, text="读取/刷新", command=self._load_source).grid(row=0, column=3, pady=3)

        ttk.Label(paths, text="当前存档：").grid(row=1, column=0, sticky="w", pady=3)
        current_entry = ttk.Entry(paths, textvariable=self.current_var)
        current_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=3)
        current_entry.bind("<Return>", lambda _event: self._normalize_current_path(show_message=True))
        ttk.Button(paths, text="浏览…", command=self._browse_current).grid(row=1, column=2, padx=(0, 6), pady=3)
        ttk.Button(paths, text="自动识别", command=self._auto_detect_current).grid(row=1, column=3, pady=3)
        ttk.Label(
            paths,
            text="提示：可直接选择 Steam 的 SaveGames 文件夹，程序会自动定位实际 SteamID 存档目录。",
            style="Muted.TLabel",
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.overwrite_button = ttk.Button(
            toolbar,
            text="覆盖当前存档",
            style="Accent.TButton",
            command=self._overwrite_current,
            state="disabled",
        )
        self.overwrite_button.pack(side="left", padx=(0, 8))
        self.new_button = ttk.Button(toolbar, text="新建存档", command=self._new_save, state="disabled")
        self.new_button.pack(side="left", padx=(0, 8))
        self.new_folder_button = ttk.Button(toolbar, text="新建文件夹", command=self._new_folder, state="disabled")
        self.new_folder_button.pack(side="left", padx=(0, 8))
        self.slim_button = ttk.Button(toolbar, text="存档瘦身", command=self._slim_selected, state="disabled")
        self.slim_button.pack(side="left", padx=(0, 8))
        self.copy_button = ttk.Button(toolbar, text="复制存档", command=self._copy_save, state="disabled")
        self.copy_button.pack(side="left", padx=(0, 8))
        self.delete_button = ttk.Button(toolbar, text="删除存档", command=self._delete_save, state="disabled")
        self.delete_button.pack(side="left", padx=(0, 8))
        self.delete_folder_button = ttk.Button(toolbar, text="删除文件夹", command=self._delete_folder, state="disabled")
        self.delete_folder_button.pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="在资源管理器中打开", command=self._open_selected).pack(side="right")

        self.paned = ttk.Panedwindow(outer, orient="horizontal")
        self.paned.grid(row=3, column=0, sticky="nsew")

        tree_frame = ttk.Frame(self.paned, padding=(0, 0, 6, 0))
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.paned.add(tree_frame, weight=4)

        columns = ("total_size", "modified")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="存档名称")
        self.tree.heading("total_size", text="总大小")
        self.tree.heading("modified", text="修改时间")
        self.tree.column("#0", width=self._px(360), minwidth=self._px(220), stretch=True)
        self.tree.column("total_size", width=self._px(85), minwidth=self._px(75), anchor="e", stretch=False)
        self.tree.column("modified", width=self._px(135), minwidth=self._px(115), anchor="center", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        tree_x.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.tag_configure("save", foreground="#147a3d", font=("Microsoft YaHei UI", 10, "bold"))
        self.tree.tag_configure("container", foreground="#2f3b4c")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu_indices: dict[str, int] = {}

        def add_context_command(label: str, command) -> None:
            self.context_menu.add_command(label=label, command=command)
            self.context_menu_indices[label] = int(self.context_menu.index("end"))

        add_context_command("覆盖当前存档", self._overwrite_current)
        add_context_command("新建存档", self._new_save)
        add_context_command("新建文件夹", self._new_folder)
        add_context_command("存档瘦身", self._slim_selected)
        self.context_menu.add_separator()
        add_context_command("复制存档", self._copy_save)
        add_context_command("删除存档", self._delete_save)
        add_context_command("删除文件夹", self._delete_folder)
        add_context_command("重命名文件夹/存档", self._rename_node)
        self.context_menu.add_separator()
        add_context_command("在资源管理器中打开", self._open_selected)
        add_context_command("刷新目录树", self._load_source)
        add_context_command("自动识别当前存档", self._auto_detect_current)
        add_context_command("打开备份目录", self._open_backup_root)

        detail_frame = ttk.Frame(self.paned, padding=(8, 0, 0, 0))
        detail_frame.rowconfigure(4, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        self.paned.add(detail_frame, weight=1)

        ttk.Label(detail_frame, text="目录详情", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(detail_frame, textvariable=self.summary_var, style="Muted.TLabel", wraplength=380).grid(
            row=1, column=0, sticky="ew", pady=(4, 8)
        )
        ttk.Separator(detail_frame, orient="horizontal").grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(detail_frame, text="本目录文件", style="Section.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 5))
        file_columns = ("size", "modified")
        self.file_tree = ttk.Treeview(detail_frame, columns=file_columns, show="tree headings", selectmode="browse")
        self.file_tree.heading("#0", text="文件名")
        self.file_tree.heading("size", text="大小")
        self.file_tree.heading("modified", text="修改时间")
        self.file_tree.column("#0", width=self._px(250), minwidth=self._px(170), stretch=True)
        self.file_tree.column("size", width=self._px(95), minwidth=self._px(80), anchor="e", stretch=False)
        self.file_tree.column("modified", width=self._px(165), minwidth=self._px(145), anchor="center", stretch=False)
        self.file_tree.grid(row=4, column=0, sticky="nsew")
        file_y = ttk.Scrollbar(detail_frame, orient="vertical", command=self.file_tree.yview)
        file_y.grid(row=4, column=1, sticky="ns")
        self.file_tree.configure(yscrollcommand=file_y.set)

        status = ttk.Frame(outer)
        status.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status, text="覆盖会完全替换当前存档内容", style="Muted.TLabel").grid(row=0, column=1, sticky="e")


    def _load_initial_settings(self) -> None:
        geometry = self.settings.get("geometry")
        saved_scale = self.settings.get("ui_scale")
        try:
            scale_matches = saved_scale is not None and abs(float(saved_scale) - self.dpi_scale) <= 0.05
        except (TypeError, ValueError):
            scale_matches = False
        if scale_matches and isinstance(geometry, str) and re.match(r"^\d+x\d+[+-]\d+[+-]\d+$", geometry):
            try:
                self.root.geometry(geometry)
            except tk.TclError:
                pass

        source_text = str(self.settings.get("source_directory") or "").strip()
        if source_text and Path(source_text).is_dir():
            self.source_var.set(source_text)
        elif DEFAULT_ARCHIVE_ROOT is not None and DEFAULT_ARCHIVE_ROOT.is_dir():
            self.source_var.set(str(DEFAULT_ARCHIVE_ROOT))

        current_text = str(self.settings.get("current_save_directory") or "").strip()
        if current_text and Path(current_text).is_dir():
            self.current_var.set(current_text)

    def _startup_scan(self) -> None:
        if self.source_var.get().strip():
            self._load_source(select_saved=True)
        if not self._normalize_current_path(show_message=False):
            self._auto_detect_current(show_message=False)

    def _persist_settings(self) -> None:
        if self._suppress_settings:
            return
        data = {
            "version": SETTINGS_VERSION,
            "source_directory": self.source_var.get().strip(),
            "current_save_directory": self.current_var.get().strip(),
            "geometry": self.root.geometry(),
            "ui_scale": self.dpi_scale,
        }
        save_settings(data)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _browse_source(self) -> None:
        initial = self.source_var.get().strip() or str(
            DEFAULT_ARCHIVE_ROOT if DEFAULT_ARCHIVE_ROOT is not None and DEFAULT_ARCHIVE_ROOT.exists() else Path.home()
        )
        selected = filedialog.askdirectory(title="选择存档库文件夹", initialdir=initial, mustexist=True)
        if selected:
            self.source_var.set(selected)
            self._load_source()

    def _browse_current(self) -> None:
        initial = self.current_var.get().strip()
        if not initial:
            detected_roots = discover_savegames_roots()
            initial = str(detected_roots[0]) if detected_roots else str(Path.home())
        selected = filedialog.askdirectory(title="选择当前存档文件夹", initialdir=initial, mustexist=True)
        if not selected:
            return
        detected = resolve_current_save_folder(selected)
        self.current_var.set(str(detected or selected))
        if detected and str(detected) != selected:
            self._set_status(f"已从所选目录自动定位实际存档：{detected}")
        else:
            self._set_status(f"当前存档已设置为：{self.current_var.get()}")
        self._persist_settings()
        self._update_action_buttons()

    def _normalize_current_path(self, show_message: bool) -> bool:
        text = self.current_var.get().strip().strip('"')
        if not text:
            return False
        path = Path(text)
        if not path.is_dir():
            if show_message:
                messagebox.showerror("路径无效", f"当前存档文件夹不存在：\n{text}", parent=self.root)
            return False
        detected = resolve_current_save_folder(path)
        if detected is None:
            if show_message:
                messagebox.showerror("路径无效", f"无法识别当前存档目录：\n{text}", parent=self.root)
            return False
        if str(detected) != text:
            self.current_var.set(str(detected))
            if show_message:
                self._set_status(f"已自动定位实际存档：{detected}")
        self._persist_settings()
        return True

    def _auto_detect_current(self, show_message: bool = True) -> None:
        self._set_status("正在扫描 Steam 库并识别当前存档…")
        self.root.update_idletasks()
        self.root.configure(cursor="watch")
        try:
            detected = auto_detect_current_save()
        except Exception:
            detected = None
        finally:
            self.root.configure(cursor="")
        if detected is None:
            self.current_var.set("")
            self._set_status("未找到当前存档，请手动选择 SaveGames 或其实际存档文件夹。")
            if show_message:
                messagebox.showinfo(
                    "未自动识别",
                    "没有找到《黑神话：悟空》的当前存档。\n\n请点击“浏览…”手动选择 SaveGames 文件夹或其中的 SteamID 文件夹。",
                    parent=self.root,
                )
        else:
            self.current_var.set(str(detected))
            self._set_status(f"已自动识别当前存档：{detected}")
            self._persist_settings()
        self._update_action_buttons()

    def _load_source(self, select_saved: bool = False) -> None:
        text = self.source_var.get().strip().strip('"')
        if not text:
            self._set_status("请先选择存档库文件夹。")
            return
        root = Path(text)
        if not root.is_dir():
            self._set_status("存档库路径无效。")
            messagebox.showerror("路径无效", f"存档库文件夹不存在：\n{root}", parent=self.root)
            return

        same_root = self.source_root is not None and self.source_root == root
        selected_path = self.selected_node.path if same_root and self.selected_node is not None else None
        expanded_paths = set()
        yview = self.tree.yview()
        xview = self.tree.xview()
        top_item = self.tree.identify_row(1)
        top_node = self.item_nodes.get(top_item)
        top_path = top_node.path if top_node is not None else None
        if same_root:
            for item, node in self.item_nodes.items():
                if self.tree.exists(item) and self.tree.item(item, "open"):
                    expanded_paths.add(node.path)

        self.root.configure(cursor="watch")
        self._set_status("正在读取存档目录树…")
        self.root.update_idletasks()
        try:
            root_node, nodes = scan_save_tree(root)
        except Exception as error:
            self.root.configure(cursor="")
            self._set_status("读取存档库失败。")
            messagebox.showerror("读取失败", f"无法读取存档库：\n{error}", parent=self.root)
            return
        finally:
            self.root.configure(cursor="")

        self.source_root = root
        self.root_node = root_node
        self.nodes = nodes
        self.selected_node = None
        self._populate_tree(
            root_node,
            selected_path=selected_path if same_root else None,
            expanded_paths=expanded_paths if same_root else None,
            top_path=top_path if same_root else None,
            yview=yview if same_root else None,
            xview=xview if same_root else None,
        )
        self.source_var.set(str(root))
        self._persist_settings()

        save_dirs = root_node.total_save_dirs
        self._set_status(f"已读取 {save_dirs} 套存档，共 {root_node.total_files} 个文件。")
        if save_dirs == 0:
            self.summary_var.set("未发现 .sav 存档文件，但空文件夹和目录结构仍会保留。")
        self._update_action_buttons()

    def _nearest_tree_item(self, path: Path | None) -> str | None:
        if path is None:
            return None
        candidate = Path(path)
        while True:
            item = self.tree_items.get(str(candidate))
            if item is not None and self.tree.exists(item):
                return item
            if self.source_root is None or candidate == self.source_root or candidate.parent == candidate:
                return None
            candidate = candidate.parent

    def _populate_tree(
        self,
        root_node: SaveNode,
        selected_path: Path | None = None,
        expanded_paths: set[Path] | None = None,
        top_path: Path | None = None,
        yview: tuple[float, float] | None = None,
        xview: tuple[float, float] | None = None,
    ) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tree_items.clear()
        self.item_nodes.clear()

        def insert(node: SaveNode, parent_item: str = "") -> str:
            suffix = "  [存档]" if node.is_save_dir else ""
            item = self.tree.insert(
                parent_item,
                "end",
                text=f"{node.name}{suffix}",
                values=(format_size(node.total_size), format_time(node.modified)),
                tags=("save",) if node.is_save_dir else ("container",),
                open=node is root_node,
            )
            self.tree_items[str(node.path)] = item
            self.item_nodes[item] = node
            for child in node.children:
                insert(child, item)
            return item

        root_item = insert(root_node)
        if expanded_paths is not None:
            for path in expanded_paths:
                item = self.tree_items.get(str(path))
                if item is not None and self.tree.exists(item):
                    self.tree.item(item, open=True)

        selected_item = self._nearest_tree_item(selected_path) or root_item
        self.tree.selection_set(selected_item)
        self.tree.focus(selected_item)
        selected_node = self.item_nodes.get(selected_item, root_node)
        self.selected_node = selected_node
        self._show_node(selected_node)

        def restore_view() -> None:
            top_item = self._nearest_tree_item(top_path)
            if top_item is not None:
                self.tree.see(top_item)
                self.root.update_idletasks()
            if yview is not None:
                self.tree.yview_moveto(max(0.0, min(1.0, yview[0])))
            if xview is not None:
                self.tree.xview_moveto(max(0.0, min(1.0, xview[0])))

        self.root.after_idle(restore_view)

    def _select_path(self, path: Path) -> None:
        item = self.tree_items.get(str(path))
        if item and self.tree.exists(item):
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.see(item)

    def _on_tree_double_click(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        if not item:
            return None
        node = self.item_nodes.get(item)
        if node is None:
            return None
        self.tree.selection_set(item)
        self.tree.focus(item)
        self.selected_node = node
        self._show_node(node)
        self._update_action_buttons()
        if node.is_save_dir:
            self._open_path(node.path)
        else:
            self.tree.item(item, open=not bool(self.tree.item(item, "open")))
        return "break"

    def _show_context_menu(self, event) -> str | None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)
            node = self.item_nodes.get(item)
            if node is not None:
                self.selected_node = node
                self._show_node(node)
        self._update_action_buttons()
        x_root = event.x_root
        y_root = event.y_root
        self.root.after_idle(lambda: self._post_context_menu(x_root, y_root))
        return "break"

    def _post_context_menu(self, x_root: int, y_root: int) -> None:
        if not self.root.winfo_exists():
            return
        try:
            self.context_menu.tk_popup(x_root, y_root)
        finally:
            self.context_menu.grab_release()

    def _node_has_archive_save(self, node: SaveNode) -> bool:
        for file_info in node.direct_files:
            if file_info.name.casefold().startswith("archivesavefile"):
                return True
        return any(self._node_has_archive_save(child) for child in node.children)

    def _on_tree_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            self.selected_node = None
            self._clear_details()
            self._update_action_buttons()
            return
        node = self.item_nodes.get(selection[0])
        self.selected_node = node
        if node is not None:
            self._show_node(node)
        self._update_action_buttons()

    def _clear_details(self) -> None:
        self.summary_var.set("请选择目录")
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)

    def _show_node(self, node: SaveNode) -> None:
        self.summary_var.set(
            f"{node.path}\n"
            f"本目录 {len(node.direct_files)} 个文件；子树共 {node.total_files} 个文件，"
            f"{format_size(node.total_size)}；包含 {node.total_save_dirs} 套存档。"
        )
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        for file_info in node.direct_files:
            self.file_tree.insert(
                "",
                "end",
                text=file_info.name,
                values=(format_size(file_info.size), format_time(file_info.modified)),
            )

    def _update_action_buttons(self) -> None:
        has_tree = self.root_node is not None
        node = self.selected_node
        can_save = bool(node and node.is_save_dir and node.path != self.source_root)
        can_mutate = not self.slim_running
        can_delete_folder = bool(
            can_mutate and node and self.source_root is not None
            and node.path != self.source_root and node.total_save_dirs == 0
        )
        can_rename = bool(
            can_mutate and has_tree and node and self.source_root is not None and node.path != self.source_root
        )
        can_slim = bool(can_mutate and has_tree and node and self._node_has_archive_save(node))
        can_create = has_tree and can_mutate
        can_save = bool(can_save and can_mutate)
        self.overwrite_button.configure(state="normal" if can_save else "disabled")
        self.new_button.configure(state="normal" if can_create else "disabled")
        self.new_folder_button.configure(state="normal" if can_create else "disabled")
        self.slim_button.configure(state="normal" if can_slim else "disabled")
        self.copy_button.configure(state="normal" if can_save else "disabled")
        self.delete_button.configure(state="normal" if can_save else "disabled")
        self.delete_folder_button.configure(state="normal" if can_delete_folder else "disabled")
        menu_states = {
            "覆盖当前存档": can_save,
            "新建存档": can_create,
            "新建文件夹": can_create,
            "存档瘦身": can_slim,
            "复制存档": can_save,
            "删除存档": can_save,
            "删除文件夹": can_delete_folder,
            "重命名文件夹/存档": can_rename,
            "在资源管理器中打开": bool(has_tree),
            "刷新目录树": bool(has_tree),
            "自动识别当前存档": True,
            "打开备份目录": True,
        }
        for label, enabled in menu_states.items():
            index = self.context_menu_indices.get(label)
            if index is not None:
                self.context_menu.entryconfigure(index, state="normal" if enabled else "disabled")


    def _overwrite_current(self) -> None:
        source = self.selected_node
        if source is None or not source.is_save_dir or source.path == self.source_root:
            messagebox.showinfo("请选择存档", "请在左侧选择标记为“[存档]”的目录。", parent=self.root)
            return
        if not self._normalize_current_path(show_message=False):
            self._set_status("当前存档路径为空或无效，请先指定或自动识别。")
            messagebox.showwarning(
                "需要当前存档路径",
                "尚未识别到当前存档。\n\n请点击“自动识别”，或手动选择 SaveGames / 实际 SteamID 存档文件夹。",
                parent=self.root,
            )
            return

        destination = Path(self.current_var.get())
        game_running = is_game_running()

        preference = self.runtime_backup_preference
        remembered = preference is not None
        if preference is None:
            dialog = BackupPrompt(self.root, source.path, destination, game_running=game_running)
            self.root.wait_window(dialog)
            if dialog.result is None:
                self._set_status("已取消覆盖。")
                return
            preference = dialog.result
            if dialog.remember:
                self.runtime_backup_preference = preference
        else:
            will_backup = "自动备份" if preference == "backup" else "直接覆盖"
            self._set_status(f"按本次运行记住的方式执行：{will_backup}")

        backup_path: Path | None = None
        self.root.configure(cursor="watch")
        self._set_status("正在应用存档…")
        self.root.update_idletasks()
        try:
            if preference == "backup":
                self._set_status("正在备份当前存档…")
                self.root.update_idletasks()
                backup_path = backup_current_save(source.path, destination)
            self._set_status("正在覆盖当前存档…")
            self.root.update_idletasks()
            warnings = apply_save_folder(source.path, destination, allow_locked=game_running)
        except Exception as error:
            self._set_status("覆盖失败，当前存档已尽量保持原状。")
            messagebox.showerror(
                "覆盖失败",
                f"{error}\n\n如果已经生成备份，可在备份目录中找到：\n{backup_path or backup_root_dir()}",
                parent=self.root,
            )
            return
        finally:
            self.root.configure(cursor="")

        if backup_path is not None:
            message = f"存档：{source.name}\n覆盖完成，原当前存档已备份到：\n{backup_path}"
            self._set_status(f"覆盖完成：{source.name}，已备份到 {backup_path}")
        else:
            message = f"存档：{source.name}\n覆盖完成。"
            self._set_status(f"覆盖完成：{source.name} -> {destination}")
        if game_running:
            message += "\n\n游戏当前正在运行：请回到标题界面并重新读取存档。"
        if warnings:
            message += "\n\n以下旧文件未能清理（通常正在被游戏占用）：\n" + "\n".join(warnings[:8])
            messagebox.showwarning("操作完成", message, parent=self.root)
        elif not remembered:
            messagebox.showinfo("操作完成", message, parent=self.root)

    def _valid_save_name(self, name: str) -> str | None:
        name = name.strip()
        if not name:
            return "名称不能为空。"
        if name in {".", ".."} or Path(name).name != name:
            return "名称不能包含路径分隔符。"
        if re.search(r'[<>:"/\\|?*]', name):
            return '名称不能包含 < > : " / \\ | ? * 等字符。'
        if name.endswith((" ", ".")):
            return "名称不能以空格或句点结尾。"
        if name.casefold() in {
            "con", "prn", "aux", "nul",
            "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
            "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
        }:
            return "该名称是 Windows 保留名称。"
        return None

    def _new_folder(self) -> None:
        if self.root_node is None or self.source_root is None:
            return
        selected = self.selected_node or self.root_node
        if selected.is_save_dir and selected.path != self.source_root:
            parent = selected.path.parent
        else:
            parent = selected.path

        name = simpledialog.askstring(
            "新建文件夹",
            f"空文件夹将创建在：\n{parent}\n\n请输入文件夹名称：",
            parent=self.root,
        )
        if name is None:
            return
        error = self._valid_save_name(name)
        if error:
            messagebox.showerror("名称无效", error, parent=self.root)
            return
        new_path = parent / name.strip()
        if new_path.exists():
            messagebox.showerror("名称已存在", f"目标已存在：\n{new_path}", parent=self.root)
            return
        try:
            new_path.mkdir(parents=False, exist_ok=False)
        except OSError as error_message:
            messagebox.showerror("新建失败", str(error_message), parent=self.root)
            return

        self._load_source(select_saved=True)
        parent_item = self.tree_items.get(str(parent))
        if parent_item is not None:
            self.tree.item(parent_item, open=True)
        self._select_path(new_path)
        self._set_status(f"已新建空文件夹：{new_path}")

    def _rename_node(self) -> None:
        node = self.selected_node
        if node is None or self.source_root is None or node.path == self.source_root:
            return
        name = simpledialog.askstring(
            "重命名文件夹/存档",
            f"请输入新的名称：\n{node.path}",
            initialvalue=node.name,
            parent=self.root,
        )
        if name is None:
            return
        error = self._valid_save_name(name)
        if error:
            messagebox.showerror("名称无效", error, parent=self.root)
            return
        name = name.strip()
        if name == node.name:
            return
        new_path = node.path.with_name(name)
        if new_path.exists():
            messagebox.showerror("名称已存在", f"目标已存在：\n{new_path}", parent=self.root)
            return
        old_path = node.path
        try:
            old_path.rename(new_path)
        except OSError as error_message:
            messagebox.showerror("重命名失败", str(error_message), parent=self.root)
            return
        self._load_source(select_saved=True)
        self._select_path(new_path)
        self._set_status(f"已重命名：{old_path.name} -> {name}")

    def _new_save(self) -> None:
        if self.root_node is None or self.source_root is None:
            return
        selected = self.selected_node or self.root_node
        if selected.is_save_dir and selected.path != self.source_root:
            parent = selected.path.parent
        else:
            parent = selected.path

        name = simpledialog.askstring(
            "新建存档",
            f"新存档将创建在：\n{parent}\n\n请输入文件夹名称：",
            parent=self.root,
        )
        if name is None:
            return
        error = self._valid_save_name(name)
        if error:
            messagebox.showerror("名称无效", error, parent=self.root)
            return
        name = name.strip()
        new_path = parent / name
        if new_path.exists():
            messagebox.showerror("名称已存在", f"目标已存在：\n{new_path}", parent=self.root)
            return
        if not self._normalize_current_path(show_message=False):
            messagebox.showwarning(
                "需要当前存档",
                "尚未识别到当前存档，无法复制其内容来新建存档。\n\n请先指定当前存档路径。",
                parent=self.root,
            )
            return

        current = Path(self.current_var.get())
        self.root.configure(cursor="watch")
        self._set_status(f"正在从当前存档新建：{name}")
        self.root.update_idletasks()
        try:
            shutil.copytree(current, new_path)
        except Exception as error_message:
            shutil.rmtree(new_path, ignore_errors=True)
            self._set_status("新建存档失败。")
            messagebox.showerror("新建失败", f"{error_message}", parent=self.root)
            return
        finally:
            self.root.configure(cursor="")

        self._load_source(select_saved=False)
        self._select_path(new_path)
        self._set_status(f"已新建存档：{new_path}")
        messagebox.showinfo("新建完成", f"已从当前存档复制内容：\n{new_path}", parent=self.root)

    def _copy_save(self) -> None:
        source = self.selected_node
        if source is None or not source.is_save_dir or source.path == self.source_root:
            return
        parent = source.path.parent
        base_name = f"{source.name} - 副本"
        destination = parent / base_name
        counter = 2
        while destination.exists():
            destination = parent / f"{base_name} {counter}"
            counter += 1
        self.root.configure(cursor="watch")
        self._set_status(f"正在复制存档：{source.name}")
        self.root.update_idletasks()
        try:
            shutil.copytree(source.path, destination)
        except Exception as error:
            self._set_status("复制失败。")
            messagebox.showerror("复制失败", str(error), parent=self.root)
            return
        finally:
            self.root.configure(cursor="")
        self._load_source(select_saved=False)
        self._select_path(destination)
        self._set_status(f"已复制存档：{destination}")

    def _delete_folder(self) -> None:
        node = self.selected_node
        if node is None or self.source_root is None or node.path == self.source_root:
            return
        if node.total_save_dirs > 0:
            messagebox.showwarning(
                "不能删除",
                "所选文件夹的子树中包含存档文件。\n\n请先删除其中的存档，或将存档移动到其他位置。",
                parent=self.root,
            )
            return
        confirmed = messagebox.askyesno(
            "确认删除文件夹",
            f"确定删除该文件夹吗？\n\n{node.path}\n\n"
            f"子树共 {node.total_files} 个文件、{format_size(node.total_size)}，"
            "其中不包含任何 .sav 存档。\n删除后会先移入 Windows 回收站。",
            parent=self.root,
        )
        if not confirmed:
            return
        parent = node.path.parent
        if not send_to_recycle_bin(node.path):
            permanent = messagebox.askyesno(
                "回收站操作失败",
                "无法移入回收站。是否永久删除该文件夹？",
                parent=self.root,
            )
            if not permanent:
                return
            try:
                shutil.rmtree(node.path)
            except Exception as error:
                messagebox.showerror("删除失败", str(error), parent=self.root)
                return
        self._load_source(select_saved=True)
        self._select_path(parent)
        self._set_status(f"已删除文件夹：{node.path}")

    def _slim_selected(self) -> None:
        node = self.selected_node
        if node is None or not self._node_has_archive_save(node):
            messagebox.showinfo("没有可瘦身内容", "所选目录及其子目录中没有 ArchiveSaveFile.*.sav。", parent=self.root)
            return
        if not self._node_has_target_save(node):
            messagebox.showinfo(
                "没有目标存档",
                "所选目录及其子目录中没有 ArchiveSaveFile.2.sav。\n瘦身会删除其他存档，因此已取消。",
                parent=self.root,
            )
            return

        targets = self._collect_slim_targets(node)
        if not targets:
            messagebox.showinfo("无需瘦身", "所选目录已经只保留 ArchiveSaveFile.2.sav。", parent=self.root)
            return
        total_size = 0
        for target in targets:
            try:
                total_size += target.stat().st_size
            except OSError:
                pass
        confirmed = messagebox.askyesno(
            "确认存档瘦身",
            f"瘦身范围：\n{node.path}\n\n"
            f"将保留每个目录中的 ArchiveSaveFile.2.sav。\n"
            f"将删除 {len(targets)} 个其他文件，约 {format_size(total_size)}。\n\n"
            "删除内容会先移入 Windows 回收站。是否继续？",
            parent=self.root,
        )
        if not confirmed:
            return

        self.slim_target_root = node.path
        self._start_slim_operation(targets, permanent=False)

    def _start_slim_operation(self, targets: list[Path], permanent: bool) -> None:
        if not targets:
            self.slim_running = False
            return
        cpu_count = os.cpu_count() or 2
        worker_count = max(1, cpu_count // 2)
        self.slim_running = True
        self.slim_queue = queue.Queue()
        self.slim_dialog = SlimProgressDialog(
            self.root,
            total=len(targets),
            worker_count=worker_count,
            permanent=permanent,
        )
        self._set_status(
            f"{'正在永久删除' if permanent else '正在后台瘦身'}，线程数：{worker_count}/{cpu_count}"
        )
        self._update_action_buttons()
        threading.Thread(
            target=self._slim_worker,
            args=(targets, worker_count, self.slim_queue, permanent),
            daemon=True,
            name="bmw-slim-dispatcher",
        ).start()
        self.root.after(60, self._poll_slim_queue)

    def _slim_worker(
        self,
        targets: list[Path],
        worker_count: int,
        events: queue.Queue,
        permanent: bool,
    ) -> None:
        total = len(targets)
        task_target = max(1, worker_count * 4)
        chunk_size = max(1, (total + task_target - 1) // task_target)
        chunks = [targets[index:index + chunk_size] for index in range(0, total, chunk_size)]
        failed: list[Path] = []
        errors: list[str] = []
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="bmw-slim") as executor:
                futures = {
                    executor.submit(
                        delete_paths_permanently_in_thread if permanent else recycle_paths_in_thread,
                        chunk,
                    ): chunk
                    for chunk in chunks
                }
                for future in as_completed(futures):
                    chunk = futures[future]
                    try:
                        result = future.result()
                    except Exception as error:
                        errors.append(str(error))
                        failed.extend(chunk)
                    else:
                        if permanent:
                            if result:
                                errors.extend(result)
                                failed.extend(chunk)
                        else:
                            failed.extend(result)
                    done += len(chunk)
                    events.put(("progress", done, total))
        except Exception as error:
            events.put(("error", f"{error}\n{traceback.format_exc()}"))
            return
        events.put(("done", failed, errors, permanent))

    def _poll_slim_queue(self) -> None:
        if self.slim_queue is None:
            return
        finished = False
        try:
            while True:
                event = self.slim_queue.get_nowait()
                event_type = event[0]
                if event_type == "progress":
                    if self.slim_dialog is not None and self.slim_dialog.winfo_exists():
                        self.slim_dialog.update_progress(event[1])
                elif event_type == "error":
                    finished = True
                    self.slim_running = False
                    if self.slim_dialog is not None:
                        self.slim_dialog.destroy()
                        self.slim_dialog = None
                    messagebox.showerror("存档瘦身失败", event[1], parent=self.root)
                elif event_type == "done":
                    finished = True
                    self.slim_running = False
                    if self.slim_dialog is not None:
                        self.slim_dialog.destroy()
                        self.slim_dialog = None
                    self._handle_slim_done(event[1], event[2], event[3])
                    break
        except queue.Empty:
            pass
        if self.slim_running and not finished:
            self.root.after(60, self._poll_slim_queue)

    def _handle_slim_done(self, failed: list[Path], errors: list[str], permanent: bool) -> None:
        node_path = self.slim_target_root
        failed_remaining = False
        if failed and not permanent:
            ask_permanent = messagebox.askyesno(
                "部分文件无法移入回收站",
                f"{len(failed)} 个文件无法移入回收站。是否使用后台线程永久删除这些文件？",
                parent=self.root,
            )
            if ask_permanent:
                self._load_source(select_saved=True)
                self._start_slim_operation(failed, permanent=True)
                return
            failed_remaining = True
        elif failed and permanent:
            failed_remaining = True
            if errors:
                messagebox.showerror("部分文件删除失败", "\n".join(errors[:10]), parent=self.root)

        if node_path is not None:
            self._load_source(select_saved=True)
            self._select_path(node_path)
        if failed_remaining:
            self._set_status(f"存档瘦身部分完成：{node_path}")
        else:
            self._set_status(f"存档瘦身完成：{node_path}")
        self.slim_queue = None

    def _node_has_target_save(self, node: SaveNode) -> bool:
        if any(file_info.name.casefold() == "archivesavefile.2.sav" for file_info in node.direct_files):
            return True
        return any(self._node_has_target_save(child) for child in node.children)

    def _collect_slim_targets(self, node: SaveNode) -> list[Path]:
        targets: list[Path] = []
        for file_info in node.direct_files:
            if file_info.name.casefold() != "archivesavefile.2.sav":
                targets.append(file_info.path)
        for child in node.children:
            targets.extend(self._collect_slim_targets(child))
        return targets

    def _delete_save(self) -> None:
        node = self.selected_node
        if node is None or not node.is_save_dir or node.path == self.source_root:
            return
        confirmed = messagebox.askyesno(
            "确认删除",
            f"确定删除该存档吗？\n\n{node.path}\n\n"
            f"包含 {len(node.direct_files)} 个本层文件，子树共 {node.total_files} 个文件、{format_size(node.total_size)}。\n"
            "删除后会先移入 Windows 回收站。",
            parent=self.root,
        )
        if not confirmed:
            return
        parent = node.path.parent
        if not send_to_recycle_bin(node.path):
            permanent = messagebox.askyesno(
                "回收站操作失败",
                "无法移入回收站。是否永久删除该存档？",
                parent=self.root,
            )
            if not permanent:
                return
            try:
                shutil.rmtree(node.path)
            except Exception as error:
                messagebox.showerror("删除失败", str(error), parent=self.root)
                return
        self._load_source(select_saved=False)
        self._select_path(parent)
        self._set_status(f"已删除存档：{node.path}")

    def _open_path(self, target: Path | str) -> None:
        if not Path(target).exists():
            return
        try:
            os.startfile(str(target))
        except OSError as error:
            messagebox.showerror("打开失败", str(error), parent=self.root)

    def _open_selected(self) -> None:
        target = self.selected_node.path if self.selected_node is not None else self.source_root
        if target is not None:
            self._open_path(target)

    def _open_backup_root(self) -> None:
        try:
            os.startfile(str(backup_root_dir()))
        except OSError as error:
            messagebox.showerror("打开失败", str(error), parent=self.root)

    def _on_close(self) -> None:
        if self.slim_running:
            messagebox.showinfo("任务进行中", "存档瘦身正在后台执行，请等待任务完成后再关闭程序。", parent=self.root)
            return
        self._persist_settings()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def set_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main() -> int:
    set_dpi_awareness()
    try:
        app = SaveManagerApp()
        app.run()
    except Exception:
        error_text = traceback.format_exc()
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, f"程序发生错误：\n\n{error_text}")
            root.destroy()
        except Exception:
            print(error_text, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
