from __future__ import annotations

import ctypes
import io
import json
import math
import mmap
import os
import queue
import re
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import messagebox, ttk

try:
    from ctypes import wintypes
except ImportError:  # pragma: no cover - Windows-only runtime import
    wintypes = None


TRAINER_SETTINGS_VERSION = 1
BMW_GAME_KEY = "black_myth_wukong"
PBZ_GAME_KEY = "phantom_blade_zero"
GAME_DISPLAY_NAMES = {
    BMW_GAME_KEY: "黑神话：悟空",
    PBZ_GAME_KEY: "影之刃零",
}

_TOGGLE_SOUND_CACHE: dict[bool, bytes] = {}
_TOGGLE_SOUND_LOCK = threading.Lock()

DATA_FLAG_PRIMARY = 0x00
DATA_FLAG_SECONDARY = 0x04
DATA_FLAG_SUPER_DAMAGE = 0x08
DATA_FLAG_EDIT_ATTACK = 0x0C
DATA_FLAG_ITEM_QUANTITY = 0x10
DATA_FLAG_GAME_SPEED = 0x14
DATA_FLAG_DAMAGE_MULTIPLIER = 0x18
DATA_PLAYER = 0x20
DATA_PLAYER_ATTRIBUTES = 0x28
DATA_IS_PLAYER_STAT = 0x30
DATA_PLAYER_STAT_ENUM = 0x34
DATA_PLAYER_CONTROLLER = 0x38
DATA_PBZ_FNAME_BASIC_ATTRIBUTE_SET = 0x40
DATA_PBZ_ATTR_HEALTH = 0x44
DATA_PBZ_ATTR_HEALTH_MAX = 0x48
DATA_PBZ_FNAME_HEALTH = 0x4C
DATA_PBZ_FNAME_SHACHI = 0x50
DATA_PLAYER_ATTRIBUTE_WRAPPER = 0x58
DATA_PLAYER_CONTEXT = 0x60
DATA_PLAYER_ATTRIBUTE_CANDIDATE_98 = 0x68
DATA_PLAYER_ATTRIBUTE_CANDIDATE_10 = 0x70
DATA_READ_HP = 0x80
DATA_READ_TENACITY = 0x84
DATA_READ_ATTACK = 0x88
DATA_READ_ATTACK_BASE = 0x8C
DATA_READ_DMG_ADDITION = 0x90
DATA_READ_VALID = 0x94

BMW_ATTR_HP = 151
BMW_ATTR_ATK_BASE = 103
BMW_ATTR_ATK = 153
BMW_ATTR_TENACITY = 164
BMW_ATTR_DMG_ADDITION = 169
BMW_ATTR_ARRAY_BASE = 0x20
BMW_ATTR_HP_OFFSET = BMW_ATTR_ARRAY_BASE + BMW_ATTR_HP * 4
BMW_ATTR_ATK_OFFSET = BMW_ATTR_ARRAY_BASE + BMW_ATTR_ATK * 4
BMW_INFINITE_HP_BITS = 0x497423F0
BMW_MAX_DAMAGE_BITS = 0x4B18961C
BMW_GET_FLOAT_HOOK_OFFSET = 0x38


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    label: str
    default_hotkey: str
    action: str
    description: str
    default_value: float | int | None = None
    value_kind: str = "int"
    minimum: float | int | None = None
    maximum: float | int | None = None
    increment: float | int | None = None

    @property
    def has_value(self) -> bool:
        return self.default_value is not None


BMW_FEATURES = (
    FeatureSpec("infinite_health", "无限生命", "数字键 2", "toggle", "锁定玩家生命值"),
    FeatureSpec("max_poise", "最高韧性", "数字键 -", "toggle", "将玩家韧性提升到最大值"),
    FeatureSpec(
        "super_damage",
        "超级伤害 / 一击必杀",
        "Ctrl+数字键 1",
        "toggle",
        "大幅提高玩家造成的伤害",
    ),
    FeatureSpec(
        "edit_attack",
        "编辑攻击",
        "Shift+F4",
        "value_toggle",
        "开启时记录原攻击力并设置新值，关闭时恢复原攻击力",
        default_value=999,
        minimum=1,
        maximum=9999999,
        increment=1,
    ),
    FeatureSpec(
        "game_speed",
        "设置游戏速度",
        "Ctrl+数字键 5",
        "value_toggle",
        "切换玩家动作速度倍率",
        default_value=2.5,
        value_kind="float",
        minimum=0.1,
        maximum=10.0,
        increment=0.1,
    ),
)

PBZ_FEATURES = (
    FeatureSpec(
        "infinite_health",
        "无限生命",
        "数字键 2",
        "toggle",
        "把玩家当前生命维持在上限的两倍",
    ),
    FeatureSpec(
        "super_damage",
        "超级伤害 / 一击必杀",
        "数字键 0",
        "toggle",
        "将非玩家目标的伤害提高到一击必杀",
    ),
    FeatureSpec(
        "damage_multiplier",
        "伤害倍率",
        "数字键 .",
        "value_toggle",
        "切换玩家伤害倍率",
        default_value=2.0,
        value_kind="float",
        minimum=0.1,
        maximum=100.0,
        increment=0.1,
    ),
    FeatureSpec(
        "unlock_nightmare",
        "解锁六十六天难度",
        "Alt+数字键 2",
        "one_shot",
        "将 PBZGameSystem.bCanSelectNightMare 设为开启",
    ),
    FeatureSpec(
        "game_speed",
        "设置游戏速度",
        "Alt+数字键 3",
        "value_toggle",
        "切换玩家动作速度倍率",
        default_value=2.5,
        value_kind="float",
        minimum=0.1,
        maximum=10.0,
        increment=0.1,
    ),
)

FEATURE_SPECS = {
    BMW_GAME_KEY: BMW_FEATURES,
    PBZ_GAME_KEY: PBZ_FEATURES,
}
FEATURE_BY_KEY = {
    game_key: {spec.key: spec for spec in specs}
    for game_key, specs in FEATURE_SPECS.items()
}

PROCESS_NAMES = {
    BMW_GAME_KEY: ("b1-Win64-Shipping.exe", "BlackMythWukong.exe"),
    PBZ_GAME_KEY: ("PBZ.exe", "ProjectPBZ-Win64-Shipping.exe", "PBZ-Win64-Shipping.exe"),
}


def default_trainer_settings() -> dict[str, dict]:
    return {
        game_key: {
            "hotkeys": {spec.key: spec.default_hotkey for spec in specs},
            "values": {
                spec.key: spec.default_value
                for spec in specs
                if spec.default_value is not None
            },
        }
        for game_key, specs in FEATURE_SPECS.items()
    }


def normalize_trainer_settings(raw: object) -> dict[str, dict]:
    result = default_trainer_settings()
    if not isinstance(raw, dict):
        return result
    for game_key, specs in FEATURE_SPECS.items():
        source = raw.get(game_key)
        if not isinstance(source, dict):
            continue
        raw_hotkeys = source.get("hotkeys")
        if isinstance(raw_hotkeys, dict):
            for spec in specs:
                if spec.key in raw_hotkeys:
                    result[game_key]["hotkeys"][spec.key] = str(
                        raw_hotkeys.get(spec.key) or ""
                    ).strip()
        raw_values = source.get("values")
        if isinstance(raw_values, dict):
            for spec in specs:
                if spec.default_value is None:
                    continue
                value = raw_values.get(spec.key, spec.default_value)
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if spec.value_kind == "int":
                    number = int(round(number))
                if spec.minimum is not None:
                    number = max(spec.minimum, number)
                if spec.maximum is not None:
                    number = min(spec.maximum, number)
                result[game_key]["values"][spec.key] = number
    return result


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    virtual_key: int
    display: str


_VK_NAMES = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
}


def parse_hotkey(text: str, allow_empty: bool = False) -> Hotkey | None:
    clean = (
        str(text)
        .strip()
        .replace("＋", "+")
        .replace("数字键", "Num")
        .replace("小键盘", "Num")
        .replace("Control", "Ctrl")
        .replace("control", "Ctrl")
        .replace("ctrl", "Ctrl")
        .replace("ALT", "Alt")
        .replace("alt", "Alt")
        .replace("SHIFT", "Shift")
        .replace("shift", "Shift")
    )
    if not clean or clean.casefold() in {"none", "无"}:
        if allow_empty:
            return None
        raise ValueError("快捷键不能为空")
    parts = [part.strip() for part in clean.split("+") if part.strip()]
    if not parts:
        raise ValueError("快捷键不能为空")

    modifiers = 0
    key_token = None
    for part in parts:
        token = part.casefold()
        if token in {"ctrl", "control"}:
            modifiers |= 0x0002
        elif token == "alt":
            modifiers |= 0x0001
        elif token == "shift":
            modifiers |= 0x0004
        elif token in {"win", "windows"}:
            modifiers |= 0x0008
        else:
            if key_token is not None:
                raise ValueError(f"无法识别快捷键：{text}")
            key_token = part
    if key_token is None:
        raise ValueError(f"缺少主按键：{text}")

    token = key_token.casefold().replace(" ", "")
    if token.startswith("num"):
        suffix = token[3:]
        if suffix.isdigit() and len(suffix) == 1:
            virtual_key = 0x60 + int(suffix)
        elif suffix in {".", "decimal"}:
            virtual_key = 0x6E
        elif suffix in {"+", "add"}:
            virtual_key = 0x6B
        elif suffix in {"-", "subtract"}:
            virtual_key = 0x6D
        elif suffix in {"*", "multiply"}:
            virtual_key = 0x6A
        elif suffix in {"/", "divide"}:
            virtual_key = 0x6F
        else:
            raise ValueError(f"无法识别小键盘按键：{key_token}")
    elif re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", token):
        virtual_key = 0x70 + int(token[1:]) - 1
    elif len(token) == 1 and "0" <= token <= "9":
        virtual_key = ord(token)
    elif len(token) == 1 and "a" <= token <= "z":
        virtual_key = ord(token.upper())
    elif token in _VK_NAMES:
        virtual_key = _VK_NAMES[token]
    else:
        raise ValueError(f"无法识别主按键：{key_token}")

    return Hotkey(modifiers, virtual_key, clean)


_TK_KEY_NAMES = {
    "Return": "Enter",
    "BackSpace": "Backspace",
    "space": "Space",
    "Prior": "PageUp",
    "Next": "PageDown",
    "Home": "Home",
    "End": "End",
    "Insert": "Insert",
    "Delete": "Delete",
    "Left": "Left",
    "Right": "Right",
    "Up": "Up",
    "Down": "Down",
}


def _pressed_modifier_flags() -> int:
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    modifiers = 0
    if user32.GetAsyncKeyState(0x11) & 0x8000:
        modifiers |= 0x0002
    if user32.GetAsyncKeyState(0x12) & 0x8000:
        modifiers |= 0x0001
    if user32.GetAsyncKeyState(0x10) & 0x8000:
        modifiers |= 0x0004
    if (user32.GetAsyncKeyState(0x5B) & 0x8000) or (
        user32.GetAsyncKeyState(0x5C) & 0x8000
    ):
        modifiers |= 0x0008
    return modifiers


def hotkey_from_tk_event(event: tk.Event, modifiers: int | None = None) -> Hotkey:
    keysym = str(event.keysym)
    if modifiers is None:
        modifiers = _pressed_modifier_flags()

    numpad = {
        "KP_0": "数字键 0",
        "KP_1": "数字键 1",
        "KP_2": "数字键 2",
        "KP_3": "数字键 3",
        "KP_4": "数字键 4",
        "KP_5": "数字键 5",
        "KP_6": "数字键 6",
        "KP_7": "数字键 7",
        "KP_8": "数字键 8",
        "KP_9": "数字键 9",
        "KP_Decimal": "数字键 .",
        "KP_Subtract": "数字键 -",
        "KP_Add": "数字键 +",
        "KP_Multiply": "数字键 *",
        "KP_Divide": "数字键 /",
    }
    if keysym in numpad:
        key_text = numpad[keysym]
    elif re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", keysym):
        key_text = keysym
    elif len(keysym) == 1 and keysym.isalnum():
        key_text = keysym.upper()
    elif keysym in _TK_KEY_NAMES:
        key_text = _TK_KEY_NAMES[keysym]
    else:
        raise ValueError(f"不支持该按键：{keysym}")

    parts = []
    if modifiers & 0x0002:
        parts.append("Ctrl")
    if modifiers & 0x0001:
        parts.append("Alt")
    if modifiers & 0x0004:
        parts.append("Shift")
    if modifiers & 0x0008:
        parts.append("Win")
    parts.append(key_text)
    display = "+".join(parts)
    parsed = parse_hotkey(display)
    if parsed is None:
        raise ValueError("快捷键无效")
    return Hotkey(parsed.modifiers, parsed.virtual_key, display)


def _toggle_sound_bytes(enabled: bool) -> bytes:
    cached = _TOGGLE_SOUND_CACHE.get(enabled)
    if cached is not None:
        return cached
    rate = 22050
    notes = ((880.0, 0.07), (1320.0, 0.11)) if enabled else ((660.0, 0.07), (440.0, 0.11))
    samples: list[int] = []
    for frequency, duration in notes:
        count = int(rate * duration)
        for index in range(count):
            fade = min(1.0, index / 220.0, (count - index - 1) / 300.0)
            value = math.sin(2.0 * math.pi * frequency * index / rate)
            samples.append(int(value * fade * 9000))
        samples.extend([0] * int(rate * 0.012))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    data = buffer.getvalue()
    _TOGGLE_SOUND_CACHE[enabled] = data
    return data


def play_toggle_sound(enabled: bool) -> None:
    if os.name != "nt":
        return

    def worker() -> None:
        try:
            import winsound

            data = _toggle_sound_bytes(enabled)
            with _TOGGLE_SOUND_LOCK:
                winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
        except Exception:
            pass

    threading.Thread(target=worker, name="trainer-toggle-sound", daemon=True).start()


class ToggleSwitch(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        px: Callable[[int], int],
        command: Callable[[], None],
    ) -> None:
        self._px = px
        self._command = command
        self._enabled = False
        self._width = px(52)
        self._height = px(28)
        style = ttk.Style(parent)
        background = style.lookup("TFrame", "background") or "#f0f0f0"
        super().__init__(
            parent,
            width=self._width,
            height=self._height,
            highlightthickness=0,
            borderwidth=0,
            background=background,
            cursor="hand2",
            takefocus=1,
        )
        self.bind("<Button-1>", lambda _event: self._activate())
        self.bind("<space>", lambda _event: self._activate())
        self.bind("<Return>", lambda _event: self._activate())
        self._draw()

    def _activate(self) -> None:
        self.focus_set()
        self._command()

    def set_state(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._enabled:
            return
        self._enabled = enabled
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        width = self._width
        height = self._height
        radius = height / 2
        track = "#5cab77" if self._enabled else "#b9c0c7"
        knob_size = height - self._px(4)
        knob_y = self._px(2)
        knob_x = width - knob_size - self._px(2) if self._enabled else self._px(2)
        self.create_oval(0, 0, height, height, fill=track, outline="")
        self.create_oval(width - height, 0, width, height, fill=track, outline="")
        self.create_rectangle(radius, 0, width - radius, height, fill=track, outline="")
        shadow = "#d4d9dd" if not self._enabled else "#4b9867"
        self.create_oval(
            knob_x + self._px(1),
            knob_y + self._px(1),
            knob_x + knob_size + self._px(1),
            knob_y + knob_size + self._px(1),
            fill=shadow,
            outline="",
        )
        self.create_oval(
            knob_x,
            knob_y,
            knob_x + knob_size,
            knob_y + knob_size,
            fill="#ffffff",
            outline="",
        )


class HotkeyRegistrationError(RuntimeError):
    pass


class GlobalHotkeyManager:
    WM_HOTKEY = 0x0312
    WM_APP_STOP = 0x8001

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._bindings: dict[int, str] = {}
        self._callback: Callable[[str], None] | None = None
        self._thread_id = 0

    def start(self, bindings: dict[str, Hotkey], callback: Callable[[str], None]) -> None:
        self.stop()
        self._bindings = {0xB000 + index: key for index, key in enumerate(bindings)}
        self._callback = callback
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="trainer-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        if self._error:
            error = self._error
            self.stop()
            raise HotkeyRegistrationError(error)

    def stop(self) -> None:
        if self._thread and self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_APP_STOP, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = 0
        self._bindings = {}
        self._callback = None

    def _run(self) -> None:
        if os.name != "nt":
            self._error = "全局快捷键仅支持 Windows"
            self._ready.set()
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = int(kernel32.GetCurrentThreadId())
        registered: list[int] = []
        try:
            for hotkey_id, feature_key in self._bindings.items():
                binding = _current_binding_for_key(self._bindings, feature_key)
                if binding is None:
                    continue
                if not user32.RegisterHotKey(None, hotkey_id, binding.modifiers | 0x4000, binding.virtual_key):
                    raise HotkeyRegistrationError(f"快捷键被占用或无法注册：{binding.display}")
                registered.append(hotkey_id)
            self._ready.set()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY and self._callback:
                    feature_key = self._bindings.get(int(msg.wParam))
                    if feature_key:
                        self._callback(feature_key)
                elif msg.message == self.WM_APP_STOP:
                    break
        except Exception as error:
            self._error = str(error)
            self._ready.set()
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)


_ACTIVE_HOTKEY_BINDINGS: dict[str, Hotkey] = {}


def _current_binding_for_key(_ids: dict[int, str], feature_key: str) -> Hotkey | None:
    return _ACTIVE_HOTKEY_BINDINGS.get(feature_key)


def set_hotkey_bindings(bindings: dict[str, Hotkey]) -> None:
    global _ACTIVE_HOTKEY_BINDINGS
    _ACTIVE_HOTKEY_BINDINGS = dict(bindings)


@dataclass
class ProcessModule:
    pid: int
    base: int
    size: int
    path: Path


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def _windows_api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.FlushInstructionCache.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t]
    kernel32.VirtualAllocEx.restype = ctypes.c_void_p
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    kernel32.VirtualProtectEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetProcAddress.restype = ctypes.c_void_p
    kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
    kernel32.LoadLibraryW.restype = wintypes.HMODULE
    kernel32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    return kernel32, user32


def _snapshot_processes(kernel32) -> list[tuple[int, str]]:
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    result: list[tuple[int, str]] = []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            result.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _suspend_process_threads(pid: int) -> list[int]:
    if os.name != "nt":
        return []
    kernel32, _user32 = _windows_api()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    handles: list[int] = []
    entry = THREADENTRY32()
    entry.dwSize = ctypes.sizeof(THREADENTRY32)
    try:
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if int(entry.th32OwnerProcessID) == pid:
                handle = kernel32.OpenThread(0x0002, False, int(entry.th32ThreadID))
                if handle and kernel32.SuspendThread(handle) != 0xFFFFFFFF:
                    handles.append(int(handle))
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return handles


def _resume_process_threads(handles: list[int]) -> None:
    if os.name != "nt":
        return
    kernel32, _user32 = _windows_api()
    for handle in reversed(handles):
        kernel32.ResumeThread(handle)
        kernel32.CloseHandle(handle)


def _visible_window_processes(user32) -> set[int]:
    result: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value:
            result.add(int(pid.value))
        return True

    user32.EnumWindows(callback, 0)
    return result


def find_game_pid(game_key: str) -> int | None:
    if os.name != "nt":
        return None
    kernel32, user32 = _windows_api()
    names = {name.casefold() for name in PROCESS_NAMES[game_key]}
    candidates = [pid for pid, name in _snapshot_processes(kernel32) if name.casefold() in names]
    if not candidates:
        return None
    visible = _visible_window_processes(user32)
    for pid in candidates:
        if pid in visible:
            return pid
    return candidates[-1] if len(candidates) == 1 else None


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return code.value == 259


def query_process_module(pid: int) -> ProcessModule | None:
    if os.name != "nt":
        return None
    kernel32, _user32 = _windows_api()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, pid)
    if snapshot == wintypes.HANDLE(-1).value:
        return None
    entry = MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
    try:
        if not kernel32.Module32FirstW(snapshot, ctypes.byref(entry)):
            return None
        return ProcessModule(
            pid=pid,
            base=int(entry.modBaseAddr or 0),
            size=int(entry.modBaseSize),
            path=Path(entry.szExePath),
        )
    finally:
        kernel32.CloseHandle(snapshot)


def _map_file_offset(sections: list[tuple[int, int, int]], offset: int) -> int | None:
    for virtual_address, virtual_size, raw_offset in sections:
        raw_size = virtual_size
        if raw_offset <= offset < raw_offset + raw_size:
            return virtual_address + (offset - raw_offset)
    return None


@dataclass(frozen=True)
class PESection:
    name: str
    virtual_address: int
    virtual_size: int
    raw_size: int
    raw_offset: int
    characteristics: int


def parse_pe_layout(path: Path) -> list[PESection]:
    with path.open("rb") as handle:
        dos = handle.read(0x40)
        if len(dos) < 0x40 or dos[:2] != b"MZ":
            raise ValueError("游戏主程序不是有效的 PE 文件")
        pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
        handle.seek(pe_offset)
        header = handle.read(24)
        if len(header) < 24 or header[:4] != b"PE\0\0":
            raise ValueError("游戏主程序 PE 头无效")
        section_count = struct.unpack_from("<H", header, 6)[0]
        optional_size = struct.unpack_from("<H", header, 20)[0]
        handle.seek(pe_offset + 24 + optional_size)
        sections: list[PESection] = []
        for _ in range(section_count):
            raw = handle.read(40)
            if len(raw) < 40:
                break
            name = raw[:8].rstrip(b"\0").decode(errors="replace")
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", raw, 8)
            characteristics = struct.unpack_from("<I", raw, 36)[0]
            sections.append(
                PESection(
                    name=name,
                    virtual_address=virtual_address,
                    virtual_size=max(virtual_size, raw_size),
                    raw_size=raw_size,
                    raw_offset=raw_offset,
                    characteristics=characteristics,
                )
            )
    return sections


def parse_pe_sections(path: Path) -> list[tuple[int, int, int]]:
    return [
        (section.virtual_address, section.virtual_size, section.raw_offset)
        for section in parse_pe_layout(path)
    ]


def compile_pattern(pattern: str) -> re.Pattern[bytes]:
    parts: list[bytes] = []
    for token in pattern.split():
        if token in {"*", "??"} or token.startswith("s"):
            parts.append(b".")
        else:
            parts.append(re.escape(bytes([int(token, 16)])))
    return re.compile(b"".join(parts), re.DOTALL)


@dataclass(frozen=True)
class PatternMatch:
    file_offset: int
    rva: int


def scan_executable_patterns(
    exe_path: Path,
    patterns: dict[str, str],
) -> dict[str, PatternMatch]:
    sections = parse_pe_sections(exe_path)
    result: dict[str, PatternMatch] = {}
    with exe_path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            for name, pattern in patterns.items():
                matches = []
                for match in compile_pattern(pattern).finditer(mapped):
                    rva = _map_file_offset(sections, match.start())
                    if rva is not None:
                        matches.append(PatternMatch(match.start(), rva))
                    if len(matches) >= 32:
                        break
                if not matches:
                    raise RuntimeError(f"未找到特征码：{name}")
                matches.sort(key=lambda item: item.rva)
                result[name] = matches[0]
    return result


@dataclass(frozen=True)
class CodeCave:
    rva: int
    size: int


def find_code_caves(exe_path: Path, minimum_size: int = 0x80) -> list[CodeCave]:
    sections = [section for section in parse_pe_layout(exe_path) if section.characteristics & 0x20000000]
    if not sections:
        return []
    result: list[CodeCave] = []
    with exe_path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            cave_pattern = re.compile(b"\xCC{" + str(minimum_size).encode() + b",}")
            for section in sections:
                start = section.raw_offset
                end = min(len(mapped), start + section.raw_size)
                if end <= start:
                    continue
                for match in cave_pattern.finditer(mapped, start, end):
                    rva = section.virtual_address + (match.start() - start)
                    result.append(CodeCave(rva, match.end() - match.start()))
    result.sort(key=lambda item: (item.size, -item.rva), reverse=True)
    return result


def _jmp_rel32(source: int, target: int) -> bytes:
    displacement = target - (source + 5)
    if not -(1 << 31) <= displacement < (1 << 31):
        raise RuntimeError("跳转距离超出 rel32 范围")
    return b"\xE9" + struct.pack("<i", displacement)


def _assemble(code: str, address: int) -> bytes:
    if not code.strip():
        return b""
    try:
        from keystone import KS_ARCH_X86, KS_MODE_64, Ks, KsError
    except ImportError as error:
        raise RuntimeError("缺少 keystone-engine，无法动态汇编修改器代码") from error
    try:
        assembler = Ks(KS_ARCH_X86, KS_MODE_64)
        encoding, _count = assembler.asm(code, address)
        return bytes(encoding)
    except KsError as error:
        raise RuntimeError(f"修改器汇编失败：{error}") from error


BMW_PATTERNS = {
    "get_player_pe_value": (
        "FF 50 * F3 0F 11 45 * F3 0F 10 * * 0F 57 * * 0F 2F * "
        "0F 82 * * 00 00 E9 * * * * 48 8B 86 * * 00 00"
    ),
    "get_float_attribute": (
        "55 48 8B EC 48 83 EC 30 48 89 4D F8 48 89 55 F0 48 8B C1 "
        "48 63 40 20 39 45 F0 0F 83 27 00 00 00 48 8B 45 F8 48 8B "
        "40 10 48 63 4D F0 48 63 C9 39 48 18 0F 86 16 00 00 00 48 "
        "8D 44 88 20 F3 0F 10 00 48 8D 65 00 5D C3 E8 EA FC 44 00 "
        "EB D2 BA 2B 00 00 00"
    ),
}

PBZ_PATTERNS = {
    "player_pawn": (
        "48 8B 9F * * 00 00 0F 84 * * 00 00 * 85 * 0F 84 * * 00 00 "
        "* 8B * E8 * * * * * 3B * 0F * * 84 * * * * 85 * 74"
    ),
    "god_mode": "74 * 48 8B BA * * 00 00 * 85 * 74 * B2 01 * 8B * E8",
    "attributes": "48 63 * * 48 03 CF 74 * F3 0F 10 41 0C * 8B * 24",
    "damage": "F3 0F 10 5C C8 38 * * * * * * * 74 * * * * 48 8B CF E8",
}


class RemoteMemory:
    def __init__(self, pid: int) -> None:
        if os.name != "nt":
            raise RuntimeError("修改器仅支持 Windows")
        self.pid = pid
        self.kernel32, _user32 = _windows_api()
        self.handle = self.kernel32.OpenProcess(0x001F0FFF, False, pid)
        if not self.handle:
            raise RuntimeError(f"无法打开游戏进程（错误 {ctypes.get_last_error()}），请尝试以管理员身份运行")

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def read(self, address: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        if not self.kernel32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)
        ):
            raise RuntimeError(f"读取游戏内存失败：0x{address:X}")
        return buffer.raw[: read.value]

    def write(self, address: int, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data)
        written = ctypes.c_size_t()
        if not self.kernel32.WriteProcessMemory(
            self.handle, ctypes.c_void_p(address), buffer, len(data), ctypes.byref(written)
        ) or written.value != len(data):
            raise RuntimeError(f"写入游戏内存失败：0x{address:X}")

    def alloc(self, size: int, preferred: int | None = None, executable: bool = False) -> int:
        protect = 0x40 if executable else 0x04
        address = self.kernel32.VirtualAllocEx(
            self.handle,
            ctypes.c_void_p(preferred) if preferred else None,
            size,
            0x00003000,
            protect,
        )
        if not address:
            raise RuntimeError(f"无法在游戏进程分配内存（错误 {ctypes.get_last_error()}）")
        return int(address)

    def alloc_near(self, targets: list[int], size: int, executable: bool = False) -> int:
        if not targets:
            raise RuntimeError("缺少修改器 Hook 目标")
        lower = max(0x10000, max(target - 0x7FFF0000 for target in targets))
        upper = min(0x00007FFFFFFF0000, min(target + 0x7FFF0000 for target in targets))
        center = (max(targets) + min(targets)) // 2
        for direction in (1, -1):
            address = center & ~0xFFFF
            attempts = 0
            while lower <= address <= upper and attempts < 65536:
                attempts += 1
                info = MEMORY_BASIC_INFORMATION()
                result = self.kernel32.VirtualQueryEx(
                    self.handle,
                    ctypes.c_void_p(address),
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
                if not result:
                    break
                base = int(info.BaseAddress or 0)
                region_size = int(info.RegionSize)
                end = base + region_size
                if info.State == 0x10000 and region_size >= size:
                    preferred = max(base, address)
                    try:
                        return self.alloc(size, preferred, executable=executable)
                    except RuntimeError:
                        pass
                if direction > 0:
                    next_address = max(end, address + 0x10000)
                    if next_address <= address:
                        break
                    address = next_address
                else:
                    next_address = min(base - 0x10000, address - 0x10000)
                    if next_address >= address:
                        break
                    address = next_address
        raise RuntimeError("无法在游戏模块附近分配修改器代码页")

    def free(self, address: int | None) -> None:
        if address:
            self.kernel32.VirtualFreeEx(self.handle, ctypes.c_void_p(address), 0, 0x8000)

    def patch(self, address: int, data: bytes, restore: bool = False) -> None:
        old_protect = wintypes.DWORD()
        if not self.kernel32.VirtualProtectEx(
            self.handle,
            ctypes.c_void_p(address),
            len(data),
            0x40,
            ctypes.byref(old_protect),
        ):
            raise RuntimeError(f"无法修改游戏代码页：0x{address:X}")
        try:
            self.write(address, data)
            self.kernel32.FlushInstructionCache(self.handle, ctypes.c_void_p(address), len(data))
        finally:
            if not restore:
                self.kernel32.VirtualProtectEx(
                    self.handle,
                    ctypes.c_void_p(address),
                    len(data),
                    old_protect.value,
                    ctypes.byref(old_protect),
                )

    def write_u32(self, address: int, value: int) -> None:
        self.write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def write_u64(self, address: int, value: int) -> None:
        self.write(address, struct.pack("<Q", value & 0xFFFFFFFFFFFFFFFF))

    def write_float(self, address: int, value: float) -> None:
        self.write(address, struct.pack("<f", float(value)))


class CodeCaveAllocator:
    def __init__(self, module_base: int, caves: list[CodeCave]) -> None:
        if not caves:
            raise RuntimeError("未找到可用的游戏代码洞穴")
        self.module_base = module_base
        self.caves = caves
        self.index = 0
        self.offset = 0

    def candidate(self) -> tuple[int, int]:
        cave = self.caves[self.index]
        address = self.module_base + cave.rva + self.offset
        end = self.module_base + cave.rva + cave.size
        return address, end

    def advance(self) -> None:
        self.index += 1
        self.offset = 0
        if self.index >= len(self.caves):
            raise RuntimeError("游戏代码洞穴空间不足")

    def commit(self, size: int) -> int:
        address, _end = self.candidate()
        self.offset = (self.offset + size + 0xF) & ~0xF
        return address


class TrainerSession:
    def __init__(
        self,
        game_key: str,
        module: ProcessModule,
        bridge_path: Path | None = None,
        speedhack_path: Path | None = None,
    ) -> None:
        self.game_key = game_key
        self.module = module
        self.bridge_path = bridge_path
        self.speedhack_path = speedhack_path
        self.speedhack_active = False
        self.memory = RemoteMemory(module.pid)
        self.data_address: int | None = None
        self.cave_allocator: CodeCaveAllocator | None = None
        self.patches: list[tuple[int, bytes]] = []
        self.cave_writes: list[tuple[int, bytes]] = []
        self.installed_hooks: set[str] = set()
        self.matches: dict[str, PatternMatch] = {}
        self._edit_attack_original: float | None = None

    def close(self) -> None:
        if self._edit_attack_original is not None:
            try:
                self.restore_edit_attack()
            except Exception:
                pass
        if self.speedhack_active:
            try:
                self.set_game_speed(1.0)
            except Exception:
                pass
        handles = _suspend_process_threads(self.module.pid)
        try:
            for address, original in reversed(self.patches):
                try:
                    self.memory.patch(address, original)
                except Exception:
                    pass
        finally:
            self.patches.clear()
            self.cave_writes.clear()
            _resume_process_threads(handles)
        self.memory.free(self.data_address)
        self.memory.close()

    def install_hook(
        self,
        name: str,
        target: int,
        hook_length: int,
        pre: str = "",
        post: str = "",
    ) -> None:
        if hook_length < 5:
            raise RuntimeError(f"{name} Hook 长度不足")
        original = self.memory.read(target, hook_length)
        if self.cave_allocator is None:
            raise RuntimeError("修改器代码洞穴尚未初始化")
        while True:
            stub_address, cave_end = self.cave_allocator.candidate()
            code = b""
            code += _assemble(pre, stub_address)
            code += original
            code += _assemble(post, stub_address + len(code))
            code += _jmp_rel32(stub_address + len(code), target + hook_length)
            if stub_address + len(code) <= cave_end:
                break
            self.cave_allocator.advance()
        cave_original = self.memory.read(stub_address, len(code))
        self.memory.patch(stub_address, code)
        self.cave_allocator.commit(len(code))
        self.cave_writes.append((stub_address, cave_original))
        patch = _jmp_rel32(target, stub_address) + b"\x90" * (hook_length - 5)
        self.memory.patch(target, patch)
        self.patches.append((target, original))

    @staticmethod
    def _data(data_address: int, offset: int) -> str:
        return f"0x{data_address + offset:X}"

    def install(self, only_hooks: set[str] | None = None) -> None:
        self.data_address = self.memory.alloc(0x1000)
        self.memory.write(self.data_address, b"\0" * 0x1000)
        self.cave_allocator = CodeCaveAllocator(self.module.base, find_code_caves(self.module.path))
        if self.game_key == BMW_GAME_KEY:
            self.matches = scan_executable_patterns(self.module.path, BMW_PATTERNS)
        elif self.game_key == PBZ_GAME_KEY:
            self.matches = scan_executable_patterns(self.module.path, PBZ_PATTERNS)
        else:
            raise RuntimeError("未知游戏")
        if only_hooks:
            self.ensure_hooks(set(only_hooks))

    def ensure_hooks(self, names: set[str]) -> None:
        pending = names - self.installed_hooks
        if not pending:
            return
        if not self.matches or self.cave_allocator is None:
            raise RuntimeError("修改器尚未初始化")
        if self.game_key == BMW_GAME_KEY:
            installer = lambda: self._install_bmw(self.matches, pending)
        else:
            installer = lambda: self._install_pbz(self.matches, pending)
        handles = _suspend_process_threads(self.module.pid)
        try:
            installer()
        finally:
            _resume_process_threads(handles)
        self.installed_hooks.update(pending)

    def ensure_feature_hooks(self, feature_key: str) -> None:
        if self.game_key == BMW_GAME_KEY:
            mapping = {
                "infinite_health": {"get_player_pe_value", "get_float_attribute"},
                "max_poise": {"get_player_pe_value", "get_float_attribute"},
                "super_damage": {"get_player_pe_value", "get_float_attribute"},
                "edit_attack": {"get_player_pe_value", "get_float_attribute"},
                "game_speed": set(),
            }
        else:
            mapping = {
                "infinite_health": {"player_pawn", "attributes"},
                "super_damage": {"player_pawn", "damage"},
                "damage_multiplier": {"player_pawn", "damage"},
                "game_speed": set(),
            }
            if feature_key in {"infinite_health", "super_damage", "damage_multiplier"}:
                self.ensure_pbz_trainer_indices()
        self.ensure_hooks(mapping.get(feature_key, set()))

    def ensure_pbz_trainer_indices(self) -> None:
        if self.game_key != PBZ_GAME_KEY:
            return
        if self.data_address is None:
            raise RuntimeError("修改器尚未初始化")
        if not self.bridge_path or not self.bridge_path.is_file():
            raise RuntimeError("缺少 PBZ 桥接组件，无法解析影之刃属性偏移")

        def query(value: str) -> int:
            result = inject_and_call_export(
                self.module.pid,
                self.bridge_path,
                "PBZ_QueryTrainerValueW",
                value,
                timeout_ms=15000,
            )
            if result == 0xFFFFFFFF or result == 0:
                raise RuntimeError(f"无法解析影之刃属性：{value}")
            return int(result)

        basic_fname = query("fname:BasicAttributeSet")
        health_offset = query("prop:BasicAttributeSet|Health")
        health_max_offset = query("prop:BasicAttributeSet|Health_Max")
        health_fname = query("fname:Health")
        shachi_fname = query("fname:Shachi")
        self.memory.write_u32(self.data_address + DATA_PBZ_FNAME_BASIC_ATTRIBUTE_SET, basic_fname)
        self.memory.write_u32(self.data_address + DATA_PBZ_ATTR_HEALTH, health_offset)
        self.memory.write_u32(self.data_address + DATA_PBZ_ATTR_HEALTH_MAX, health_max_offset)
        self.memory.write_u32(self.data_address + DATA_PBZ_FNAME_HEALTH, health_fname)
        self.memory.write_u32(self.data_address + DATA_PBZ_FNAME_SHACHI, shachi_fname)

    def set_game_speed(self, value: float) -> None:
        if not self.speedhack_path or not self.speedhack_path.is_file():
            raise RuntimeError("缺少游戏速度组件 TrSpeedHack_x64.dll")
        if not self.speedhack_active:
            init_result = inject_and_call_ordinal(
                self.module.pid,
                self.speedhack_path,
                1,
                timeout_ms=15000,
            )
            if init_result != 0:
                raise RuntimeError("游戏速度组件初始化失败")
            self.speedhack_active = True
        result = inject_and_call_ordinal(
            self.module.pid,
            self.speedhack_path,
            3,
            argument=int(round(float(value) * 1000.0)),
            timeout_ms=10000,
        )
        if result != 0:
            raise RuntimeError("设置游戏速度失败")

    def _install_bmw(
        self, matches: dict[str, PatternMatch], only_hooks: set[str] | None = None
    ) -> None:
        data = self.data_address
        get_pe_pre = f"""
            push rax
            push rbx
            mov eax, edx
            mov rbx, {self._data(data, 0)}
            mov dword ptr [rbx + {DATA_IS_PLAYER_STAT}], 1
            mov dword ptr [rbx + {DATA_PLAYER_STAT_ENUM}], eax
            mov rax, qword ptr [rsi + 0x10]
            mov qword ptr [rbx + {DATA_PLAYER}], rax
            pop rbx
            pop rax
        """
        get_pe_post = f"""
            push rax
            mov rax, {self._data(data, 0)}
            mov dword ptr [rax + {DATA_IS_PLAYER_STAT}], 0
            pop rax
        """
        get_float_pre = f"""
            push rax
            push rbx
            push rdx
            mov rbx, rax
            lea rax, [rax + rcx*4 + {BMW_ATTR_ARRAY_BASE}]
            movss xmm0, dword ptr [rax]
            mov rdx, {self._data(data, 0)}
            cmp qword ptr [rdx + {DATA_PLAYER_ATTRIBUTES}], rbx
            je player_attributes_ready
            cmp dword ptr [rdx + {DATA_IS_PLAYER_STAT}], 1
            jne cleanup
            cmp dword ptr [rdx + {DATA_PLAYER_STAT_ENUM}], ecx
            jne cleanup
            mov dword ptr [rdx + {DATA_IS_PLAYER_STAT}], 0
            mov qword ptr [rdx + {DATA_PLAYER_ATTRIBUTES}], rbx
        player_attributes_ready:
            mov dword ptr [rdx + {DATA_READ_VALID}], 1
            cmp ecx, {BMW_ATTR_HP}
            jne read_tenacity
            movss dword ptr [rdx + {DATA_READ_HP}], xmm0
        read_tenacity:
            cmp ecx, {BMW_ATTR_TENACITY}
            jne read_attack
            movss dword ptr [rdx + {DATA_READ_TENACITY}], xmm0
        read_attack:
            cmp ecx, {BMW_ATTR_ATK}
            jne read_attack_base
            movss dword ptr [rdx + {DATA_READ_ATTACK}], xmm0
        read_attack_base:
            cmp ecx, {BMW_ATTR_ATK_BASE}
            jne read_damage_addition
            movss dword ptr [rdx + {DATA_READ_ATTACK_BASE}], xmm0
        read_damage_addition:
            cmp ecx, {BMW_ATTR_DMG_ADDITION}
            jne check_infinite_health
            movss dword ptr [rdx + {DATA_READ_DMG_ADDITION}], xmm0
        check_infinite_health:
            cmp dword ptr [rdx + {DATA_FLAG_PRIMARY}], 1
            jne check_edit_attack
            mov dword ptr [rbx + {BMW_ATTR_HP_OFFSET}], {BMW_INFINITE_HP_BITS}
        check_edit_attack:
            cmp dword ptr [rdx + {DATA_FLAG_EDIT_ATTACK}], 0
            jle cleanup
            mov eax, dword ptr [rdx + {DATA_FLAG_EDIT_ATTACK}]
            mov dword ptr [rbx + {BMW_ATTR_ATK_OFFSET}], eax
        cleanup:
            pop rdx
            pop rbx
            pop rax
        """
        get_float_post = f"""
            push rax
            push rdx
            mov rdx, {self._data(data, 0)}
            cmp dword ptr [rdx + {DATA_FLAG_SECONDARY}], 1
            jne check_super_damage
            cmp ecx, {BMW_ATTR_TENACITY}
            jne check_super_damage
            mov eax, {BMW_MAX_DAMAGE_BITS}
            movd xmm0, eax
            jmp override_done
        check_super_damage:
            cmp dword ptr [rdx + {DATA_FLAG_SUPER_DAMAGE}], 1
            jne override_done
            cmp ecx, {BMW_ATTR_DMG_ADDITION}
            jne override_done
            mov eax, {BMW_MAX_DAMAGE_BITS}
            movd xmm0, eax
        override_done:
            pop rdx
            pop rax
        """
        if only_hooks is None or "get_player_pe_value" in only_hooks:
            self.install_hook(
                "get_player_pe_value",
                self.module.base + matches["get_player_pe_value"].rva,
                8,
                pre=get_pe_pre,
                post=get_pe_post,
            )
        if only_hooks is None or "get_float_attribute" in only_hooks:
            self.install_hook(
                "get_float_attribute",
                self.module.base
                + matches["get_float_attribute"].rva
                + BMW_GET_FLOAT_HOOK_OFFSET,
                9,
                pre=get_float_pre,
                post=get_float_post,
            )

    def _install_pbz(
        self, matches: dict[str, PatternMatch], only_hooks: set[str] | None = None
    ) -> None:
        data = self.data_address
        player_post = f"""
            push rax
            mov rax, {self._data(data, 0)}
            mov qword ptr [rax + {DATA_PLAYER}], rbx
            mov qword ptr [rax + {DATA_PLAYER_CONTROLLER}], rdi
            pop rax
        """
        attributes_post = f"""
            test rdi, rdi
            je attributes_skip
            push rax
            push rbx
            push rdx
            sub rsp, 0x20
            movups xmmword ptr [rsp], xmm1
            mov rbx, qword ptr [rdi + 0x10]
            test rbx, rbx
            je attributes_done
            mov edx, dword ptr [rbx + 0x18]
            mov rax, {self._data(data, 0)}
            mov rax, qword ptr [rax + {DATA_PLAYER}]
            test rax, rax
            je attributes_done
            cmp qword ptr [rdi + 0x20], rax
            jne attributes_done
            mov rax, {self._data(data, 0)}
            cmp dword ptr [rax + {DATA_PBZ_FNAME_BASIC_ATTRIBUTE_SET}], edx
            jne attributes_done
            cmp dword ptr [rax + {DATA_FLAG_PRIMARY}], 1
            jne attributes_done
            mov edx, dword ptr [rax + {DATA_PBZ_ATTR_HEALTH_MAX}]
            movss xmm1, dword ptr [rdi + rdx + 0x0C]
            addss xmm1, xmm1
            mov edx, dword ptr [rax + {DATA_PBZ_ATTR_HEALTH}]
            movss dword ptr [rdi + rdx + 0x08], xmm1
            movss dword ptr [rdi + rdx + 0x0C], xmm1
        attributes_done:
            movups xmm1, xmmword ptr [rsp]
            add rsp, 0x20
            pop rdx
            pop rbx
            pop rax
        attributes_skip:
        """
        damage_pre = f"""
            test rdi, rdi
            je damage_orig
            cmp dword ptr [rax + rcx*8 + 0x38], 0
            jge damage_orig
            push rax
            push rbx
            push rdx
            push r8
            lea rbx, [rax + rcx*8]
            cmp dword ptr [rbx + 0x28], 1
            jl damage_done
            mov rdx, qword ptr [rbx + 0x20]
            test rdx, rdx
            je damage_done
            mov edx, dword ptr [rdx]
            mov r8, {self._data(data, 0)}
            mov rax, qword ptr [r8 + {DATA_PLAYER}]
            cmp qword ptr [rdi + 0x20], rax
            je damage_done
            cmp edx, dword ptr [r8 + {DATA_PBZ_FNAME_HEALTH}]
            jne damage_done
            mov rdx, r8
            cmp dword ptr [rdx + {DATA_FLAG_SUPER_DAMAGE}], 1
            jne damage_multiplier
            mov dword ptr [rbx + 0x38], 0xC97423F0
            jmp damage_done
        damage_multiplier:
            cmp dword ptr [rdx + {DATA_FLAG_DAMAGE_MULTIPLIER}], 0
            jle damage_done
            fld dword ptr [rbx + 0x38]
            fmul dword ptr [rdx + {DATA_FLAG_DAMAGE_MULTIPLIER}]
            fstp dword ptr [rbx + 0x38]
        damage_done:
            pop r8
            pop rdx
            pop rbx
            pop rax
        damage_orig:
        """
        speed_post = f"""
            push rax
            mov rax, {self._data(data, 0)}
            cmp dword ptr [rax + {DATA_FLAG_GAME_SPEED}], 0
            jle speed_done
            mov rax, qword ptr [rax + {DATA_PLAYER}]
            cmp qword ptr [rbx + 0x20], rax
            jne speed_done
            mov rax, {self._data(data, 0)}
            mulss xmm0, dword ptr [rax + {DATA_FLAG_GAME_SPEED}]
        speed_done:
            pop rax
        """

        if only_hooks is None or "player_pawn" in only_hooks:
            self.install_hook(
                "player_pawn",
                self.module.base + matches["player_pawn"].rva,
                7,
                post=player_post,
            )
        if only_hooks is None or "attributes" in only_hooks:
            self.install_hook(
                "attributes",
                self.module.base + matches["attributes"].rva + 9,
                5,
                post=attributes_post,
            )
        if only_hooks is None or "damage" in only_hooks:
            self.install_hook(
                "damage",
                self.module.base + matches["damage"].rva,
                6,
                pre=damage_pre,
            )
    def set_flag(self, offset: int, enabled: bool) -> None:
        if self.data_address is None:
            raise RuntimeError("修改器尚未连接")
        self.memory.write_u32(self.data_address + offset, 1 if enabled else 0)

    def set_float(self, offset: int, value: float) -> None:
        if self.data_address is None:
            raise RuntimeError("修改器尚未连接")
        self.memory.write_float(self.data_address + offset, value)

    def set_u32(self, offset: int, value: int) -> None:
        if self.data_address is None:
            raise RuntimeError("修改器尚未连接")
        self.memory.write_u32(self.data_address + offset, value)

    def player_attributes_address(self) -> int | None:
        if self.data_address is None:
            return None
        raw = self.memory.read(self.data_address + DATA_PLAYER_ATTRIBUTES, 8)
        address = struct.unpack("<Q", raw)[0]
        return int(address) or None

    def read_player_attribute(self, attr_id: int) -> float | None:
        attributes = self.player_attributes_address()
        if attributes is None:
            return None
        offset = BMW_ATTR_ARRAY_BASE + int(attr_id) * 4
        raw = self.memory.read(attributes + offset, 4)
        return float(struct.unpack("<f", raw)[0])

    def write_player_attribute(self, attr_id: int, value: float) -> None:
        attributes = self.player_attributes_address()
        if attributes is None:
            raise RuntimeError("尚未找到玩家属性地址，请进入游戏后再试。")
        offset = BMW_ATTR_ARRAY_BASE + int(attr_id) * 4
        self.memory.write_float(attributes + offset, float(value))

    def enable_edit_attack(self, value: float) -> None:
        if self._edit_attack_original is None:
            original = self.read_player_attribute(BMW_ATTR_ATK)
            if original is None:
                raise RuntimeError("尚未读到玩家攻击力，请进入游戏后再试。")
            self._edit_attack_original = original
        target = float(value)
        self.set_float(DATA_FLAG_EDIT_ATTACK, target)
        self.write_player_attribute(BMW_ATTR_ATK, target)

    def restore_edit_attack(self) -> None:
        self.set_flag(DATA_FLAG_EDIT_ATTACK, False)
        if self._edit_attack_original is None:
            return
        self.write_player_attribute(BMW_ATTR_ATK, self._edit_attack_original)
        self._edit_attack_original = None

    def read_display_values(self) -> dict[str, float]:
        if self.game_key != BMW_GAME_KEY or self.data_address is None:
            return {}
        try:
            if struct.unpack(
                "<I", self.memory.read(self.data_address + DATA_READ_VALID, 4)
            )[0] != 1:
                return {}
            values: dict[str, float] = {}
            for key, attr_id in (
                ("infinite_health", BMW_ATTR_HP),
                ("max_poise", BMW_ATTR_TENACITY),
                ("super_damage", BMW_ATTR_ATK),
                ("edit_attack", BMW_ATTR_ATK),
            ):
                value = self.read_player_attribute(attr_id)
                if value is not None and math.isfinite(value):
                    values[key] = value
            return values
        except (OSError, RuntimeError, struct.error):
            return {}


def _find_process_by_name(name: str) -> int | None:
    for key in (BMW_GAME_KEY, PBZ_GAME_KEY):
        if name.casefold() in {item.casefold() for item in PROCESS_NAMES[key]}:
            return find_game_pid(key)
    return None


def inject_and_call_export(
    pid: int,
    dll_path: Path,
    export_name: str,
    argument: str | None = None,
    timeout_ms: int = 10000,
) -> int:
    kernel32, _user32 = _windows_api()
    handle = kernel32.OpenProcess(0x001F0FFF, False, pid)
    if not handle:
        raise RuntimeError(f"无法打开游戏进程（错误 {ctypes.get_last_error()}）")
    remote_argument = None
    remote_path = None
    try:
        load_library = kernel32.GetProcAddress(kernel32._handle, b"LoadLibraryW")
        path_bytes = str(dll_path.resolve()).encode("utf-16-le") + b"\x00\x00"
        remote_path = kernel32.VirtualAllocEx(handle, None, len(path_bytes), 0x3000, 0x04)
        written = ctypes.c_size_t()
        kernel32.WriteProcessMemory(handle, remote_path, path_bytes, len(path_bytes), ctypes.byref(written))
        thread_id = wintypes.DWORD()
        thread = kernel32.CreateRemoteThread(handle, None, 0, load_library, remote_path, 0, ctypes.byref(thread_id))
        if not thread:
            raise RuntimeError("无法向游戏进程加载桥接组件")
        kernel32.WaitForSingleObject(thread, 15000)
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code))
        kernel32.CloseHandle(thread)

        remote_base = _module_base_by_name(pid, dll_path.name)
        if not remote_base:
            raise RuntimeError("桥接组件已加载，但无法定位模块基址")
        local_library = ctypes.WinDLL(str(dll_path))
        local_base = int(local_library._handle)
        try:
            local_export = ctypes.cast(getattr(local_library, export_name), ctypes.c_void_p).value
        except AttributeError as error:
            raise RuntimeError(f"桥接组件缺少导出：{export_name}") from error
        remote_export = remote_base + (int(local_export) - local_base)

        if argument is not None:
            arg_bytes = argument.encode("utf-16-le") + b"\x00\x00"
            remote_argument = kernel32.VirtualAllocEx(handle, None, len(arg_bytes), 0x3000, 0x04)
            kernel32.WriteProcessMemory(
                handle, remote_argument, arg_bytes, len(arg_bytes), ctypes.byref(written)
            )
        thread = kernel32.CreateRemoteThread(
            handle, None, 0, ctypes.c_void_p(remote_export), remote_argument, 0, ctypes.byref(thread_id)
        )
        if not thread:
            raise RuntimeError("无法执行游戏内修改器操作")
        kernel32.WaitForSingleObject(thread, timeout_ms)
        kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code))
        kernel32.CloseHandle(thread)
        return int(exit_code.value)
    finally:
        if remote_argument:
            kernel32.VirtualFreeEx(handle, remote_argument, 0, 0x8000)
        if remote_path:
            kernel32.VirtualFreeEx(handle, remote_path, 0, 0x8000)
        kernel32.CloseHandle(handle)


def inject_and_call_ordinal(
    pid: int,
    dll_path: Path,
    ordinal: int,
    argument: int | None = None,
    timeout_ms: int = 10000,
) -> int:
    kernel32, _user32 = _windows_api()
    handle = kernel32.OpenProcess(0x001F0FFF, False, pid)
    if not handle:
        raise RuntimeError(f"无法打开游戏进程（错误 {ctypes.get_last_error()}）")
    remote_path = None
    try:
        load_library = kernel32.GetProcAddress(kernel32._handle, b"LoadLibraryW")
        path_bytes = str(dll_path.resolve()).encode("utf-16-le") + b"\x00\x00"
        remote_path = kernel32.VirtualAllocEx(handle, None, len(path_bytes), 0x3000, 0x04)
        written = ctypes.c_size_t()
        kernel32.WriteProcessMemory(handle, remote_path, path_bytes, len(path_bytes), ctypes.byref(written))
        thread_id = wintypes.DWORD()
        thread = kernel32.CreateRemoteThread(handle, None, 0, load_library, remote_path, 0, ctypes.byref(thread_id))
        if not thread:
            raise RuntimeError("无法向游戏进程加载速度组件")
        kernel32.WaitForSingleObject(thread, 15000)
        kernel32.CloseHandle(thread)

        remote_base = _module_base_by_name(pid, dll_path.name)
        if not remote_base:
            raise RuntimeError("速度组件已加载，但无法定位模块基址")
        local_library = ctypes.WinDLL(str(dll_path))
        local_export = kernel32.GetProcAddress(
            local_library._handle,
            ctypes.cast(ctypes.c_void_p(ordinal), ctypes.c_char_p),
        )
        if not local_export:
            raise RuntimeError(f"速度组件缺少导出序号 {ordinal}")
        remote_export = remote_base + (int(local_export) - int(local_library._handle))
        thread = kernel32.CreateRemoteThread(
            handle,
            None,
            0,
            ctypes.c_void_p(remote_export),
            ctypes.c_void_p(argument) if argument is not None else None,
            0,
            ctypes.byref(thread_id),
        )
        if not thread:
            raise RuntimeError("无法执行游戏速度设置")
        kernel32.WaitForSingleObject(thread, timeout_ms)
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeThread(thread, ctypes.byref(exit_code))
        kernel32.CloseHandle(thread)
        return int(exit_code.value)
    finally:
        if remote_path:
            kernel32.VirtualFreeEx(handle, remote_path, 0, 0x8000)
        kernel32.CloseHandle(handle)


def _module_base_by_name(pid: int, name: str) -> int | None:
    kernel32, _user32 = _windows_api()
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, pid)
    if snapshot == wintypes.HANDLE(-1).value:
        return None
    entry = MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
    try:
        ok = kernel32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szModule.casefold() == name.casefold():
                return int(entry.modBaseAddr or 0)
            ok = kernel32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return None


class TrainerController:
    def __init__(
        self, bridge_path: Path, speedhack_path: Path, event_queue: queue.Queue
    ) -> None:
        self.bridge_path = bridge_path
        self.speedhack_path = speedhack_path
        self.events = event_queue
        self.active_game_key = BMW_GAME_KEY
        self._commands: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="trainer-controller", daemon=True)
        self._stopping = threading.Event()
        self._active = threading.Event()
        self._session: TrainerSession | None = None
        self._session_pid: int | None = None
        self._states: dict[str, dict[str, bool]] = {
            game_key: {spec.key: False for spec in specs}
            for game_key, specs in FEATURE_SPECS.items()
        }
        self._pending_values: dict[tuple[str, str], float | int] = {}
        self._last_value_snapshot: dict[str, float] = {}
        self._thread.start()

    def set_game(self, game_key: str) -> None:
        if game_key in FEATURE_SPECS:
            self.active_game_key = game_key
            self._commands.put(("set_game", game_key))

    def activate(self) -> None:
        self._active.set()

    def toggle(self, game_key: str, feature_key: str, enabled: bool, value: float | int | None) -> None:
        self._commands.put(("toggle", game_key, feature_key, enabled, value))

    def apply(self, game_key: str, feature_key: str, value: float | int | None) -> None:
        self._commands.put(("apply", game_key, feature_key, value))

    def close(self) -> None:
        self._stopping.set()
        self._commands.put(("close",))
        self._thread.join(timeout=3.0)

    def _emit(self, kind: str, **payload) -> None:
        self.events.put({"type": kind, **payload})

    def _run(self) -> None:
        last_poll = 0.0
        while not self._stopping.is_set():
            try:
                command = self._commands.get(timeout=0.15)
            except queue.Empty:
                command = None
            if command:
                if command[0] == "close":
                    self._detach()
                    break
                if command[0] == "set_game":
                    self.active_game_key = command[1]
                    self._detach()
                elif command[0] == "toggle":
                    self._handle_toggle(*command[1:])
                elif command[0] == "apply":
                    self._handle_apply(*command[1:])

            now = time.monotonic()
            if now - last_poll >= 0.5:
                last_poll = now
                self._poll_game()
        self._detach()

    def _poll_game(self) -> None:
        if not self._active.is_set():
            return
        pid = find_game_pid(self.active_game_key)
        if pid is None:
            if self._session:
                self._detach()
                self._emit("status", state="waiting", message="等待游戏启动，启动后会自动连接。")
            return
        if self._session and self._session_pid == pid and process_alive(pid):
            self._emit_values()
            return
        self._detach()
        module = query_process_module(pid)
        if module is None:
            self._emit("status", state="error", message="已找到游戏进程，但无法读取主模块。")
            return
        self._emit("status", state="connecting", message=f"正在连接 {module.path.name}（PID {pid}）…")
        session: TrainerSession | None = None
        try:
            session = TrainerSession(
                self.active_game_key,
                module,
                self.bridge_path,
                self.speedhack_path,
            )
            session.install()
            if self.active_game_key == BMW_GAME_KEY:
                session.ensure_hooks({"get_player_pe_value", "get_float_attribute"})
            else:
                session.ensure_pbz_trainer_indices()
                session.ensure_hooks({"player_pawn", "attributes"})
            self._session = session
            self._session_pid = pid
            for feature_key, enabled in self._states[self.active_game_key].items():
                if enabled:
                    spec = FEATURE_BY_KEY[self.active_game_key][feature_key]
                    self._apply_state(
                        session,
                        spec,
                        True,
                        self._value_for(self.active_game_key, feature_key),
                    )
            self._emit(
                "status",
                state="connected",
                message=f"已连接 {module.path.name}（PID {pid}），开启功能时加载对应 Hook。",
            )
            self._emit_values()
        except Exception as error:
            if session is not None:
                session.close()
            self._emit("status", state="error", message=str(error))

    def _detach(self) -> None:
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None
        self._session_pid = None
        if self._last_value_snapshot:
            self._last_value_snapshot = {}
            self._emit("values", game_key=self.active_game_key, values={})

    def _emit_values(self) -> None:
        if not self._session:
            values: dict[str, float] = {}
        else:
            values = self._session.read_display_values()
        if values == self._last_value_snapshot:
            return
        self._last_value_snapshot = dict(values)
        self._emit("values", game_key=self.active_game_key, values=values)

    def _value_for(self, game_key: str, feature_key: str) -> float | int | None:
        return self._pending_values.get((game_key, feature_key))

    def _handle_toggle(
        self, game_key: str, feature_key: str, enabled: bool, value: float | int | None
    ) -> None:
        self._states[game_key][feature_key] = enabled
        if value is not None:
            self._pending_values[(game_key, feature_key)] = value
        if not self._session or game_key != self.active_game_key:
            self._emit(
                "state", game_key=game_key, feature_key=feature_key, enabled=False, sound=False
            )
            self._emit("error", message="修改器尚未连接游戏，请先启动游戏。")
            return
        spec = FEATURE_BY_KEY[game_key][feature_key]
        try:
            self._apply_state(self._session, spec, enabled, value)
            self._emit(
                "state", game_key=game_key, feature_key=feature_key, enabled=enabled, sound=True
            )
        except Exception as error:
            self._states[game_key][feature_key] = not enabled
            self._emit(
                "state",
                game_key=game_key,
                feature_key=feature_key,
                enabled=not enabled,
                sound=False,
            )
            self._emit("error", message=str(error))
            self._detach()

    def _handle_apply(self, game_key: str, feature_key: str, value: float | int | None) -> None:
        if not self._session or game_key != self.active_game_key:
            self._emit("error", message="修改器尚未连接游戏，请先启动游戏。")
            return
        spec = FEATURE_BY_KEY[game_key][feature_key]
        try:
            if spec.action == "one_shot" and game_key == PBZ_GAME_KEY:
                exit_code = inject_and_call_export(
                    self._session.module.pid,
                    self.bridge_path,
                    "PBZ_UnlockNightmareW",
                )
                if exit_code != 0:
                    raise RuntimeError("游戏内属性写入失败，请确认已进入标题界面或游戏内。")
            else:
                self._apply_state(self._session, spec, True, value)
            self._emit(
                "applied",
                game_key=game_key,
                feature_key=feature_key,
                value=value,
                sound=True,
            )
        except Exception as error:
            self._emit("error", message=str(error))
            self._detach()

    def _apply_state(
        self, session: TrainerSession, spec: FeatureSpec, enabled: bool, value: float | int | None
    ) -> None:
        if spec.action != "one_shot":
            session.ensure_feature_hooks(spec.key)
        if session.game_key == BMW_GAME_KEY:
            if spec.key == "infinite_health":
                session.set_flag(DATA_FLAG_PRIMARY, enabled)
            elif spec.key == "max_poise":
                session.set_flag(DATA_FLAG_SECONDARY, enabled)
            elif spec.key == "super_damage":
                session.set_flag(DATA_FLAG_SUPER_DAMAGE, enabled)
            elif spec.key == "edit_attack":
                if enabled:
                    session.enable_edit_attack(float(value or 0))
                else:
                    session.restore_edit_attack()
            elif spec.key == "game_speed":
                session.set_game_speed(float(value or 1.0) if enabled else 1.0)
        elif session.game_key == PBZ_GAME_KEY:
            if spec.key == "infinite_health":
                session.set_flag(DATA_FLAG_PRIMARY, enabled)
            elif spec.key == "super_damage":
                session.set_flag(DATA_FLAG_SUPER_DAMAGE, enabled)
            elif spec.key == "damage_multiplier":
                session.set_float(
                    DATA_FLAG_DAMAGE_MULTIPLIER, float(value or 0) if enabled else 0.0
                )
            elif spec.key == "game_speed":
                session.set_game_speed(float(value or 1.0) if enabled else 1.0)


class TrainerPanel(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        owner,
        settings: dict[str, dict],
        game_key: str,
        persist_callback: Callable[[dict[str, dict]], None],
        px: Callable[[int], int],
        bridge_path: Path,
        speedhack_path: Path,
    ) -> None:
        super().__init__(parent)
        self.owner = owner
        self.settings = settings
        self.active_game_key = game_key
        self.persist_callback = persist_callback
        self._px = px
        self._bridge_path = bridge_path
        self._speedhack_path = speedhack_path
        self._events: queue.Queue = queue.Queue()
        self._hotkeys = GlobalHotkeyManager()
        self._controller = TrainerController(bridge_path, speedhack_path, self._events)
        self._states: dict[str, dict[str, bool]] = {
            item: {spec.key: False for spec in specs}
            for item, specs in FEATURE_SPECS.items()
        }
        self.status_var = tk.StringVar(value="切换到修改器页后自动连接。")
        self.game_var = tk.StringVar(value=GAME_DISPLAY_NAMES[game_key])
        self.hotkey_vars: dict[str, tk.StringVar] = {}
        self.hotkey_buttons: dict[str, ttk.Button] = {}
        self.value_vars: dict[str, tk.StringVar] = {}
        self.read_value_vars: dict[str, tk.StringVar] = {}
        self.action_buttons: dict[str, ttk.Button | ToggleSwitch] = {}
        self.state_vars: dict[str, tk.StringVar] = {}
        self._capture_feature: str | None = None
        self._capture_original = ""
        self._capture_bound = False
        self._activated = False
        self._resize_column_index: int | None = None
        self._resize_column_key = ""
        self._resize_start_x = 0
        self._resize_start_width = 0
        self._build()
        self._load_game(game_key)
        self.after(120, self._pump)

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        header = ttk.Frame(self, padding=(12, 12, 12, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, textvariable=self.game_var, style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Button(header, text="立即连接", command=self._reconnect).grid(
            row=0, column=1, rowspan=2, sticky="e"
        )

        ttk.Label(
            self,
            text="游戏启动后自动连接；只有当前游戏对应的全局快捷键会注册。",
            style="Muted.TLabel",
            padding=(12, 0, 12, 8),
        ).grid(row=1, column=0, sticky="w")

        table = ttk.LabelFrame(self, text=" 功能与快捷键 ", padding=(10, 8))
        table.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))
        self.table = table
        for column in range(5):
            table.columnconfigure(column, weight=1 if column == 0 else 0)
        headings = ("功能", "快捷键", "数值", "状态", "操作")
        column_keys = ("feature", "hotkey", "value", "status", "action")
        default_widths = (250, 165, 90, 110, 78)
        minimum_widths = (130, 120, 65, 80, 65)
        for column, (column_key, heading, default_width, minimum_width) in enumerate(
            zip(column_keys, headings, default_widths, minimum_widths)
        ):
            width = max(
                self._px(minimum_width),
                self.owner._table_column_width_px("trainer_table", column_key, default_width),
            )
            table.columnconfigure(column, minsize=width, weight=0)
            header = ttk.Label(table, text=heading, style="Muted.TLabel", width=1, anchor="w")
            header.grid(row=0, column=column, sticky="ew", padx=(0, 8), pady=(0, 6))
            header.configure(cursor="sb_h_double_arrow")
            header.bind(
                "<ButtonPress-1>",
                lambda event, index=column, key=column_key: self._start_column_resize(event, index, key),
            )
            header.bind("<B1-Motion>", self._resize_column)
            header.bind("<ButtonRelease-1>", self._finish_column_resize)

        self.row_frame = table
        ttk.Separator(self, orient="horizontal").grid(row=3, column=0, sticky="ew", padx=12)
        footer = ttk.Frame(self, padding=(12, 8, 12, 12))
        footer.grid(row=4, column=0, sticky="ew")
        ttk.Label(
            footer,
            text="首次使用请先在游戏中进入可操作角色；修改器会校验游戏版本和特征码。",
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(footer, text="保存快捷键", command=self._save_hotkeys).pack(side="right")

    def _start_column_resize(self, event: tk.Event, column: int, column_key: str) -> None:
        self._resize_column_index = column
        self._resize_column_key = column_key
        self._resize_start_x = int(event.x_root)
        self._resize_start_width = int(self.table.grid_columnconfigure(column, "minsize") or 1)

    def _resize_column(self, event: tk.Event) -> None:
        if self._resize_column_index is None:
            return
        minimum = (130, 120, 65, 80, 65)[self._resize_column_index]
        width = max(self._px(minimum), self._resize_start_width + int(event.x_root) - self._resize_start_x)
        self.table.columnconfigure(self._resize_column_index, minsize=width)

    def _finish_column_resize(self, _event: tk.Event) -> None:
        if self._resize_column_index is None:
            return
        width = int(self.table.grid_columnconfigure(self._resize_column_index, "minsize") or 0)
        column_key = self._resize_column_key
        self._resize_column_index = None
        self.owner._remember_table_column_width("trainer_table", column_key, width)

    def _clear_rows(self) -> None:
        for widget in list(self.row_frame.grid_slaves()):
            info = widget.grid_info()
            if int(info.get("row", 0)) > 0:
                widget.destroy()
        self.hotkey_vars.clear()
        self.hotkey_buttons.clear()
        self.value_vars.clear()
        self.read_value_vars.clear()
        self.action_buttons.clear()
        self.state_vars.clear()

    def _load_game(self, game_key: str) -> None:
        if self._capture_feature is not None:
            self._cancel_hotkey_capture()
        self.active_game_key = game_key
        self.game_var.set(GAME_DISPLAY_NAMES[game_key])
        self._clear_rows()
        config = self.settings[game_key]
        for row, spec in enumerate(FEATURE_SPECS[game_key], start=1):
            ttk.Label(self.row_frame, text=spec.label, width=1, anchor="w").grid(
                row=row, column=0, sticky="ew", padx=(0, 8), pady=4
            )
            saved_hotkey = config["hotkeys"].get(spec.key, spec.default_hotkey)
            hotkey_var = tk.StringVar(value=saved_hotkey or "无")
            hotkey_button = ttk.Button(
                self.row_frame,
                textvariable=hotkey_var,
                width=1,
                command=lambda key=spec.key: self._begin_hotkey_capture(key),
            )
            hotkey_button.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=4)
            self.hotkey_vars[spec.key] = hotkey_var
            self.hotkey_buttons[spec.key] = hotkey_button
            if spec.has_value:
                value_var = tk.StringVar(value=str(config["values"].get(spec.key, spec.default_value)))
                spin = ttk.Spinbox(
                    self.row_frame,
                    textvariable=value_var,
                    from_=spec.minimum,
                    to=spec.maximum,
                    increment=spec.increment,
                    width=1,
                )
                spin.grid(row=row, column=2, sticky="ew", padx=(0, 8), pady=4)
                spin.bind("<FocusOut>", lambda _event: self._save_values())
                self.value_vars[spec.key] = value_var
            else:
                read_value_var = tk.StringVar(value="—")
                self.read_value_vars[spec.key] = read_value_var
                ttk.Label(
                    self.row_frame,
                    textvariable=read_value_var,
                    style="Muted.TLabel",
                    width=1,
                    anchor="w",
                ).grid(row=row, column=2, sticky="ew", padx=(0, 8), pady=4)
            state_var = tk.StringVar(value="未连接")
            self.state_vars[spec.key] = state_var
            ttk.Label(self.row_frame, textvariable=state_var, style="Muted.TLabel", width=1, anchor="w").grid(
                row=row, column=3, sticky="ew", padx=(0, 8), pady=4
            )
            if spec.action in {"toggle", "value_toggle"}:
                button: ttk.Button | ToggleSwitch = ToggleSwitch(
                    self.row_frame,
                    self._px,
                    command=lambda key=spec.key: self._toggle(key),
                )
            else:
                button = ttk.Button(
                    self.row_frame,
                    text="应用",
                    width=1,
                    command=lambda key=spec.key: self._apply(key),
                )
            button.grid(
                row=row,
                column=4,
                sticky="w" if isinstance(button, ToggleSwitch) else "ew",
                padx=(0, 8) if not isinstance(button, ToggleSwitch) else 0,
                pady=4,
            )
            self.action_buttons[spec.key] = button
        self._load_hotkeys()
        self._controller.set_game(game_key)

    def set_game(self, game_key: str) -> None:
        if game_key == self.active_game_key:
            return
        self._load_game(game_key)

    def activate(self) -> None:
        if self._activated:
            return
        self._activated = True
        self.status_var.set("等待游戏启动，启动后会自动连接。")
        self._controller.activate()

    def _load_hotkeys(self) -> None:
        bindings: dict[str, Hotkey] = {}
        for key, var in self.hotkey_vars.items():
            value = var.get().strip()
            if not value or value == "无":
                continue
            try:
                parsed = parse_hotkey(value, allow_empty=True)
                if parsed is not None:
                    bindings[key] = parsed
            except ValueError:
                continue
        set_hotkey_bindings(bindings)
        try:
            self._hotkeys.start(bindings, self._on_hotkey)
        except Exception as error:
            self.status_var.set(f"快捷键注册失败：{error}")

    def _save_hotkeys(self, show_error: bool = True) -> None:
        config = self.settings[self.active_game_key]
        for key, var in self.hotkey_vars.items():
            try:
                text = var.get().strip()
                if text == "无":
                    text = ""
                parsed = parse_hotkey(text, allow_empty=True)
                if parsed is None:
                    var.set("无")
                    config["hotkeys"][key] = ""
                else:
                    var.set(parsed.display)
                    config["hotkeys"][key] = parsed.display
            except ValueError as error:
                if show_error:
                    messagebox.showerror("快捷键无效", str(error), parent=self)
                return
        self.persist_callback(self.settings)
        self._load_hotkeys()

    def _begin_hotkey_capture(self, feature_key: str) -> None:
        if self._capture_feature is not None:
            self._cancel_hotkey_capture()
        self._capture_feature = feature_key
        self._capture_original = self.hotkey_vars[feature_key].get()
        self.hotkey_vars[feature_key].set("请按按键…")
        self.hotkey_buttons[feature_key].state(["pressed"])
        self._hotkeys.stop()
        self.bind_all("<KeyPress>", self._on_hotkey_capture_key, add="+")
        self._capture_bound = True
        self.winfo_toplevel().focus_force()

    def _cancel_hotkey_capture(self) -> None:
        if self._capture_feature is None:
            return
        feature_key = self._capture_feature
        self.hotkey_vars[feature_key].set(self._capture_original or "无")
        self.hotkey_buttons[feature_key].state(["!pressed"])
        self._capture_feature = None
        if self._capture_bound:
            self.unbind_all("<KeyPress>")
            self._capture_bound = False
        self._load_hotkeys()

    def _finish_hotkey_capture(self, display: str) -> None:
        if self._capture_feature is None:
            return
        feature_key = self._capture_feature
        self.hotkey_vars[feature_key].set(display or "无")
        self.hotkey_buttons[feature_key].state(["!pressed"])
        self._capture_feature = None
        if self._capture_bound:
            self.unbind_all("<KeyPress>")
            self._capture_bound = False
        self._save_hotkeys(show_error=False)

    def _on_hotkey_capture_key(self, event: tk.Event) -> str:
        if self._capture_feature is None:
            return "break"
        if event.keysym == "Escape":
            self._finish_hotkey_capture("")
            play_toggle_sound(False)
            return "break"
        modifier_keys = {
            "Shift_L",
            "Shift_R",
            "Control_L",
            "Control_R",
            "Alt_L",
            "Alt_R",
            "Win_L",
            "Win_R",
            "Caps_Lock",
        }
        if event.keysym in modifier_keys:
            return "break"
        try:
            parsed = hotkey_from_tk_event(event)
        except ValueError as error:
            self.status_var.set(str(error))
            return "break"
        for key, var in self.hotkey_vars.items():
            if key == self._capture_feature:
                continue
            try:
                existing = parse_hotkey(var.get(), allow_empty=True)
            except ValueError:
                existing = None
            if (
                existing is not None
                and existing.modifiers == parsed.modifiers
                and existing.virtual_key == parsed.virtual_key
            ):
                messagebox.showerror(
                    "快捷键重复",
                    f"{parsed.display} 已被“{FEATURE_BY_KEY[self.active_game_key][key].label}”使用。",
                    parent=self,
                )
                self._cancel_hotkey_capture()
                return "break"
        self._finish_hotkey_capture(parsed.display)
        play_toggle_sound(True)
        return "break"

    def _save_values(self) -> None:
        config = self.settings[self.active_game_key]
        for key, var in self.value_vars.items():
            spec = FEATURE_BY_KEY[self.active_game_key][key]
            try:
                number = float(var.get())
            except ValueError:
                continue
            if spec.value_kind == "int":
                number = int(round(number))
            if spec.minimum is not None:
                number = max(spec.minimum, number)
            if spec.maximum is not None:
                number = min(spec.maximum, number)
            var.set(str(number))
            config["values"][key] = number
        self.persist_callback(self.settings)

    def _value_for(self, key: str) -> float | int | None:
        var = self.value_vars.get(key)
        if var is None:
            return None
        spec = FEATURE_BY_KEY[self.active_game_key][key]
        value = float(var.get())
        return int(round(value)) if spec.value_kind == "int" else value

    @staticmethod
    def _format_read_value(value: float) -> str:
        if abs(value - round(value)) < 0.01:
            return f"{int(round(value)):,}"
        return f"{value:,.1f}"

    def _clear_read_values(self) -> None:
        for var in self.read_value_vars.values():
            var.set("—")

    def _toggle(self, key: str) -> None:
        self._save_values()
        enabled = not self._states[self.active_game_key][key]
        self._controller.toggle(self.active_game_key, key, enabled, self._value_for(key))

    def _apply(self, key: str) -> None:
        self._save_values()
        self._controller.apply(self.active_game_key, key, self._value_for(key))

    def _on_hotkey(self, key: str) -> None:
        spec = FEATURE_BY_KEY[self.active_game_key].get(key)
        if spec is None:
            return
        if spec.action in {"toggle", "value_toggle"}:
            self._toggle(key)
        else:
            self._apply(key)

    def _reconnect(self) -> None:
        self._activated = True
        self._controller.activate()
        self._controller.set_game(self.active_game_key)
        self.status_var.set("正在重新连接游戏…")

    def _pump(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                kind = event.get("type")
                if kind == "status":
                    self.status_var.set(str(event.get("message") or ""))
                    state = event.get("state")
                    if state == "connected":
                        for key, state_var in self.state_vars.items():
                            state_var.set("已开启" if self._states[self.active_game_key][key] else "已连接")
                    elif state in {"waiting", "error"}:
                        self._clear_read_values()
                elif kind == "values":
                    game_key = event.get("game_key")
                    values = event.get("values")
                    if game_key == self.active_game_key and isinstance(values, dict):
                        for key, var in self.read_value_vars.items():
                            value = values.get(key)
                            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                                var.set(self._format_read_value(float(value)))
                            else:
                                var.set("—")
                elif kind == "state":
                    game_key = event.get("game_key")
                    feature_key = event.get("feature_key")
                    enabled = bool(event.get("enabled"))
                    if game_key == self.active_game_key and feature_key in self.state_vars:
                        self._states[game_key][feature_key] = enabled
                        self.state_vars[feature_key].set("已开启" if enabled else "已关闭")
                        button = self.action_buttons[feature_key]
                        if isinstance(button, ToggleSwitch):
                            button.set_state(enabled)
                        else:
                            button.configure(text="关闭" if enabled else "开启")
                        if event.get("sound"):
                            play_toggle_sound(enabled)
                elif kind == "applied":
                    key = event.get("feature_key")
                    if key in self.state_vars:
                        self.state_vars[key].set("已应用")
                    if event.get("sound"):
                        play_toggle_sound(True)
                elif kind == "error":
                    self.status_var.set(str(event.get("message") or "修改器操作失败"))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._pump)

    def close(self) -> None:
        if self._capture_bound:
            self.unbind_all("<KeyPress>")
            self._capture_bound = False
        self._hotkeys.stop()
        self._controller.close()


def validate_trainer_runtime() -> str | None:
    if os.name != "nt":
        return "修改器仅支持 Windows"
    try:
        import keystone  # noqa: F401
    except ImportError:
        return "缺少 keystone-engine，无法启动修改器"
    return None
