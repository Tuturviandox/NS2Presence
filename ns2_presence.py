#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NS2 Presence - Discord Rich Presence for the Switch 2 emulators.

The program lives OUTSIDE the emulator folders: it never touches the emulator
binaries, so an emulator update can not remove or break it.

How it works
------------
* It watches the emulator processes (eglantine.exe / lichii.exe / ...).
* It reads the emulator log to know which game is running (name + title id).
* It can scan a folder of .nup packages to read the real game name, version
  and 256x256 icon, without loading the (possibly 25 GB) file.
* It publishes a Discord Rich Presence with:
      - the game name,
      - the game icon (extracted .nup icon or public box art URL),
      - the emulator in use (name + build) and its logo.
* A small tkinter window lets the user pick the games folder and review the
  scanned library.

Only the Python standard library is required, plus pystray/Pillow for the
optional tray icon and thumbnails.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import msvcrt
import os
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

APP_NAME = "NS2 Presence"

if getattr(sys, "frozen", False):  # PyInstaller one-file build
    BASE_DIR = Path(sys.executable).resolve().parent
    RES_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
else:
    BASE_DIR = Path(__file__).resolve().parent
    RES_DIR = BASE_DIR

ROOT_DIR = BASE_DIR.parent
EMUS_DIR = ROOT_DIR / "emus"

CONFIG_PATH = BASE_DIR / "config.json"
ICONS_MAP_PATH = BASE_DIR / "icons.json"
CACHE_DIR = BASE_DIR / "cache"
TITLES_CACHE_PATH = CACHE_DIR / "titles.json"
ICONS_CACHE_PATH = CACHE_DIR / "icons.json"
NUP_CACHE_DIR = CACHE_DIR / "nup"
NUP_DB_PATH = CACHE_DIR / "nup_games.json"
LOG_PATH = BASE_DIR / "presence.log"

ICON_PNG = RES_DIR / "assets" / "icon.png"

EMULATOR_EXES = (
    "eglantine.exe",
    "lichii.exe",
    "eden.exe",
    "yuzu.exe",
    "sudachi.exe",
    "citron.exe",
    "ryujinx.exe",
)

# Helper programs shipped next to the emulator: they must never be watched.
# Anything matching these suffixes (eden-cli.exe, lichii-updater.exe, ...) is
# ignored, which lets a fork rename its main executable without breaking the
# automatic detection.
HELPER_EXES = {
    "eden-cli.exe",
    "lichii-cli.exe",
    "eglantine-cli.exe",
    "eden-room.exe",
    "lichii-room.exe",
    "tests.exe",
    "vc_redist.x64.exe",
    "vc_redist.x86.exe",
    "unins000.exe",
    "crashpad_handler.exe",
    "qtwebengineprocess.exe",
}
HELPER_RE = re.compile(
    r"(?i)(?:^|[-_.])(cli|updater|room|tests?|installer|setup|unins\d*|"
    r"crash\w*|vc_redist|repair|helper)(?:[-_.]|$)"
)

# Files that identify an Eden-family emulator installation.
EMULATOR_MARKERS = ("qt.conf", "emu-manager.json", "Qt6Core.dll", "Qt6Gui.dll")

# Folders that only contain backups / old builds and must never be watched.
SKIP_PARTS = (
    "update-old",
    ".update-backup",
    "plugins",
    "generic",
    "iconengines",
    "imageformats",
    "networkinformation",
    "platforms",
    "styles",
    "tls",
    "recipes",
    "user",
)

# Friendly names for the folders the user already has (fallback: folder name).
FRIENDLY_NAMES = {
    "eglantine main": "Eglantine Main",
    "eglantine selvet": "Eglantine Selvet",
    "fritchii-v4": "Fritchii v4",
    "litchi emu": "Lichii",
    "pokedinner (fork)": "PokeDinner",
    "lichii build-130 (older)": "Lichii build 130",
}

# Brand detected from the executable name.
BRANDS = {
    "eglantine.exe": "Eglantine",
    "lichii.exe": "Lichii",
    "eden.exe": "Eden",
    "yuzu.exe": "Yuzu",
    "sudachi.exe": "Sudachi",
    "citron.exe": "Citron",
    "ryujinx.exe": "Ryujinx",
}

# Public logo used as small image / fallback large image.
DEFAULT_LOGO = (
    "https://git.eden-emu.dev/eden-emu/eden/raw/branch/master/"
    "dist/qt_themes/default/icons/256x256/eden.png"
)
EGLANTINE_LOGO = (
    "https://raw.githubusercontent.com/Djihads80/Eglantine/master/"
    "dist/qt_themes/default/icons/256x256/eden.png"
)
LICHII_LOGO = "https://lichii.zortos.me/branding/icon.png"

LOGO_BY_BRAND = {
    "Eglantine": EGLANTINE_LOGO,
    "Lichii": LICHII_LOGO,
    "Eden": DEFAULT_LOGO,
    "Yuzu": DEFAULT_LOGO,
    "Sudachi": DEFAULT_LOGO,
    "Citron": DEFAULT_LOGO,
    "Ryujinx": DEFAULT_LOGO,
}

DEFAULT_CONFIG = {
    "client_id": "1397286652128264252",
    "poll_seconds": 3,
    "scan_seconds": 30,
    "show_version": True,
    "show_timer": True,
    "idle_presence": True,
    "disable_builtin_presence": True,
    "debug": False,
    "details_playing": "On {emulator}",
    "state_playing": "Nintendo Switch 2",
    "details_idle": "In the menu",
    "state_idle": "{emulator}",
    "ignore": [],
    "emulators": {},
    "icons": {},
    "games_dir": "",
    "upload_icons": True,
}

RE_LOADING = re.compile(r"Loading\s+(.+?)\s+\(([0-9A-Fa-f]{16})\)")
RE_TITLE_ID = re.compile(r"title_id=([0-9A-Fa-f]{16})")
RE_STOP = re.compile(r"OnEmulationStopped|Emulation cleanup completed|Core <Info> core/core\.cpp:\d+:Shutdown")

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

_log_lock = threading.Lock()


def log(message: str) -> None:
    try:
        with _log_lock:
            p = LOG_PATH
            if p.exists() and p.stat().st_size > 512 * 1024:
                try:
                    p.replace(p.with_suffix(".old.log"))
                except OSError:
                    pass
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
            if DEBUG:
                try:
                    print(message)
                except Exception:
                    pass
    except Exception:
        pass


DEBUG = False

_config_cache: dict | None = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception as exc:
            log(f"config.json unreadable ({exc}), using defaults")

    # keep nested dicts valid
    for key in ("emulators", "icons"):
        if not isinstance(cfg.get(key), dict):
            cfg[key] = {}
    if not isinstance(cfg.get("ignore"), list):
        cfg["ignore"] = []

    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.write_text(
                json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    _config_cache = cfg
    return cfg


def save_config(cfg: dict) -> None:
    global _config_cache
    _config_cache = cfg
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log(f"could not write config.json: {exc}")


# --------------------------------------------------------------------------- #
# Win32 helpers (processes / windows)
# --------------------------------------------------------------------------- #

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.c_void_p]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.PeekNamedPipe.argtypes = [
    wt.HANDLE,
    wt.LPVOID,
    wt.DWORD,
    wt.LPVOID,
    ctypes.POINTER(wt.DWORD),
    wt.LPVOID,
]
kernel32.PeekNamedPipe.restype = wt.BOOL


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def _process_path(pid: int) -> str | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wt.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(handle)


def process_snapshot() -> dict[str, tuple[int, str]]:
    """Return {normalized exe path: (pid, exe name)} for every running process."""
    result: dict[str, tuple[int, str]] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == wt.HANDLE(-1).value:
        return result
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return result
        while True:
            name = entry.szExeFile
            path = _process_path(entry.th32ProcessID)
            if path:
                result[path.lower()] = (entry.th32ProcessID, name)
            elif name:
                result.setdefault(f"<name>{name.lower()}", (entry.th32ProcessID, name))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return result


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def window_title_for_pid(pid: int) -> str | None:
    titles: list[str] = []

    def callback(hwnd, _lparam):
        wnd_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wnd_pid))
        if wnd_pid.value != pid:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        text = buf.value.strip()
        if text and user32.IsWindowVisible(hwnd):
            titles.append(text)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    if not titles:
        return None
    # The main window is usually the longest title.
    return max(titles, key=len)


# --------------------------------------------------------------------------- #
# Emulator discovery
# --------------------------------------------------------------------------- #

_EMU_DISPLAY_REPLACE = {
    "™": "",
    "®": "",
    "©": "",
}


def _clean_folder_name(name: str) -> str:
    key = name.strip().lower()
    if key in FRIENDLY_NAMES:
        return FRIENDLY_NAMES[key]
    for bad, good in _EMU_DISPLAY_REPLACE.items():
        name = name.replace(bad, good)
    name = re.sub(r"\((?:fork|older|old|copy)\)", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -_")
    return name or "Emulator"


BRAND_KEYWORDS = (
    ("Lichii", ("lichii", "fritchii")),
    ("Eglantine", ("eglantine", "pokedinner", "poke dinner")),
    ("Eden", ("eden",)),
    ("Sudachi", ("sudachi",)),
    ("Citron", ("citron",)),
    ("Ryujinx", ("ryujinx",)),
    ("Yuzu", ("yuzu",)),
)


def _brand_from_text(*texts: str) -> str:
    """Guess the brand from exe / folder / VERSION text (fork-proof)."""
    for text in texts:
        lowered = (text or "").lower()
        for brand, words in BRAND_KEYWORDS:
            if any(word in lowered for word in words):
                return brand
    return ""


class Emulator:
    __slots__ = ("exe", "folder", "brand", "name", "version", "logo", "config_dirs")

    def __init__(self, exe: Path):
        self.exe = exe
        self.folder = exe.parent
        version_name, version = self._read_version()
        self.brand = (
            BRANDS.get(exe.name.lower())
            or _brand_from_text(version_name, self.folder.name, exe.stem)
            or exe.stem.title()
        )
        folder_name = _clean_folder_name(self.folder.name)
        self.name = (
            FRIENDLY_NAMES.get(self.folder.name.strip().lower())
            or version_name
            or folder_name
        )
        self.version = version
        self.logo = LOGO_BY_BRAND.get(self.brand, DEFAULT_LOGO)
        self.config_dirs = self._config_dirs()

    # -- version ----------------------------------------------------------- #
    def _read_version(self) -> tuple[str, str]:
        """Return (product name, version) from the VERSION - *.txt file."""
        for path in sorted(self.folder.glob("VERSION - *.txt")):
            try:
                lines = [
                    line.strip()
                    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                ]
            except Exception:
                continue
            lines = [line for line in lines if line]
            if not lines:
                continue
            name = lines[0]
            version = ""
            for line in lines[:3]:
                match = re.search(r"(?i)^version\s*:\s*(.+?)(?:\s*\(|$)", line)
                if match:
                    version = match.group(1).strip()
                    break
            if not version:
                # Fall back to the file name: "VERSION - build-150.txt"
                version = path.stem.replace("VERSION -", "").strip()
            return name, version

        # emu-manager.json (used by the installer frontend)
        manager = self.folder / "emu-manager.json"
        if manager.exists():
            try:
                data = json.loads(manager.read_text(encoding="utf-8"))
                name = str(data.get("Name") or "").strip()
                tag = str(data.get("Tag") or "").strip()
                if tag or name:
                    return name, tag
            except Exception:
                pass
        return "", ""

    def _appdata_brands(self) -> list[str]:
        folder = self.folder.name.lower()
        brands: list[str] = []
        if "fritchii" in folder:
            brands.append("fritchii")
        if "pokedinner" in folder or "poke" in folder:
            brands.append("pokedinner")
        brands.append(self.brand.lower())
        if self.brand.lower() in ("eglantine", "pokedinner"):
            brands.append("eglantine")
        if self.brand.lower() in ("lichii", "fritchii"):
            brands.append("lichii")
            brands.append("fritchii")
        brands.append("eden")
        seen, out = set(), []
        for brand in brands:
            if brand and brand not in seen:
                seen.add(brand)
                out.append(brand)
        return out

    def _config_dirs(self) -> list[Path]:
        dirs = [self.folder / "user" / "config"]
        appdata = Path(os.environ.get("APPDATA", ""))
        for name in self._appdata_brands():
            dirs.append(appdata / name / "config")
        return dirs

    # -- display ----------------------------------------------------------- #
    @property
    def label(self) -> str:
        if self.version and self.version.lower() not in self.name.lower():
            return f"{self.name} ({self.version})"
        return self.name

    @property
    def short_label(self) -> str:
        if self.name and self.name.lower() != self.brand.lower():
            return self.name
        return self.brand

    def log_files(self) -> list[Path]:
        files: list[Path] = []
        user_log = self.folder / "user" / "log"
        for name in ("eden_log.txt", "lichii_log.txt", "yuzu_log.txt"):
            p = user_log / name
            if p.exists():
                files.append(p)
        appdata = Path(os.environ.get("APPDATA", ""))
        for brand in self._appdata_brands():
            for name in ("eden_log.txt", "lichii_log.txt"):
                p = appdata / brand / "log" / name
                if p.exists():
                    files.append(p)
        # Deduplicate while keeping order.
        seen, out = set(), []
        for f in files:
            key = str(f).lower()
            if key not in seen:
                seen.add(key)
                out.append(f)
        return out


def _is_helper_exe(path: Path) -> bool:
    name = path.name.lower()
    return name in HELPER_EXES or bool(HELPER_RE.search(name))


def _looks_like_emulator_folder(folder: Path) -> bool:
    return any((folder / marker).exists() for marker in EMULATOR_MARKERS)


def _main_exe_in(folder: Path, exes: list[Path]) -> Path | None:
    """Pick the actual emulator among the executables of one folder."""
    # 1) Well-known executable names always win.
    for exe in exes:
        if exe.name.lower() in EMULATOR_EXES:
            return exe

    # 2) The installer manifest lists the files it installed.
    manager = folder / "emu-manager.json"
    if manager.exists():
        try:
            data = json.loads(manager.read_text(encoding="utf-8"))
            entries = {str(x).lower() for x in (data.get("Entries") or [])}
            for exe in exes:
                if exe.name.lower() in entries:
                    return exe
        except Exception:
            pass

    # 3) A fork with a brand-new name: take the largest real executable.
    usable = []
    for exe in exes:
        try:
            size = exe.stat().st_size
        except OSError:
            continue
        if size >= 512 * 1024:
            usable.append((size, exe))
    if usable:
        return max(usable, key=lambda item: item[0])[1]
    return exes[0] if exes else None


def discover_emulators() -> list[Emulator]:
    """Find every emulator install, including renamed / unknown forks.

    The detection is folder based: any folder that contains an emulator
    signature (qt.conf, emu-manager.json, Qt6Core.dll, ...) and a real
    executable is watched, even if its exe is not in EMULATOR_EXES. This is
    what keeps working when a fork is updated and renames its binary.
    """
    emulators: list[Emulator] = []
    if not EMUS_DIR.exists():
        return emulators
    cfg = load_config()
    ignore = {str(x).lower() for x in cfg.get("ignore", [])}

    by_folder: dict[str, list[Path]] = {}
    for path in EMUS_DIR.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".exe":
            continue
        parts = {p.lower() for p in path.parts}
        if any(part in parts for part in SKIP_PARTS):
            continue
        if _is_helper_exe(path):
            continue
        if str(path).lower() in ignore:
            continue
        if not _looks_like_emulator_folder(path.parent):
            continue
        by_folder.setdefault(str(path.parent).lower(), []).append(path)

    for folder_key, exes in by_folder.items():
        folder = Path(folder_key)
        chosen = _main_exe_in(folder, exes)
        if chosen is None or str(chosen).lower() in ignore:
            continue
        emulators.append(Emulator(chosen))

    # Deduplicate by executable path.
    uniq: dict[str, Emulator] = {}
    for emu in emulators:
        uniq[str(emu.exe).lower()] = emu
    return list(uniq.values())


def apply_overrides(emulators: list[Emulator], cfg: dict) -> None:
    overrides = cfg.get("emulators") or {}
    for emu in emulators:
        data = overrides.get(emu.folder.name) or overrides.get(str(emu.exe)) or {}
        if not isinstance(data, dict):
            continue
        if data.get("name"):
            emu.name = str(data["name"])
        if data.get("version"):
            emu.version = str(data["version"])
        if data.get("logo"):
            emu.logo = str(data["logo"])


# --------------------------------------------------------------------------- #
# Game detection (logs + window title)
# --------------------------------------------------------------------------- #

_titles_cache: dict[str, str] | None = None
_titles_lock = threading.Lock()


def load_titles_cache() -> dict[str, str]:
    global _titles_cache
    with _titles_lock:
        if _titles_cache is None:
            data: dict[str, str] = {}
            try:
                if TITLES_CACHE_PATH.exists():
                    raw = json.loads(TITLES_CACHE_PATH.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = {str(k).upper(): str(v) for k, v in raw.items()}
            except Exception:
                data = {}
            _titles_cache = data
        return _titles_cache


def remember_title(title_id: str, name: str) -> None:
    if not title_id or not name:
        return
    cache = load_titles_cache()
    title_id = title_id.upper()
    if cache.get(title_id) == name:
        return
    cache[title_id] = name
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        TITLES_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _read_tail(path: Path, size: int = 512 * 1024) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            length = fh.tell()
            fh.seek(max(0, length - size))
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


MAX_INITIAL_READ = 32 * 1024 * 1024


def _read_whole(path: Path, cap: int = MAX_INITIAL_READ) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > cap:
                fh.seek(size - cap)
            return fh.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def scan_logs_for_titles(emulators: list[Emulator]) -> None:
    """Build the title-id -> name cache from every known log file."""
    cache = load_titles_cache()
    for emu in emulators:
        for log_file in emu.log_files():
            text = _read_whole(log_file)
            for match in RE_LOADING.finditer(text):
                cache[match.group(2).upper()] = match.group(1).strip()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        TITLES_CACHE_PATH.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


class LogWatcher:
    """Incrementally follows an emulator log and keeps the last known game.

    Eden-family emulators append megabytes of log while a game runs, so the
    "Loading ..." line quickly ends up far from the end of the file. Following
    the file with an offset keeps the detection correct and cheap.
    """

    __slots__ = ("path", "offset", "game")

    def __init__(self):
        self.path: Path | None = None
        self.offset = 0
        self.game: tuple[str, str] | None = None

    def _apply(self, text: str) -> None:
        last_load = -1
        for match in RE_LOADING.finditer(text):
            self.game = (match.group(2).upper(), match.group(1).strip())
            last_load = match.start()
        stops = [m.start() for m in RE_STOP.finditer(text)]
        if stops and stops[-1] > last_load:
            self.game = None

    def update(self, path: Path) -> tuple[str, str] | None:
        try:
            st = path.stat()
        except OSError:
            return self.game

        if path != self.path:
            self.path = path
            self.offset = 0
            self.game = None
        elif st.st_size < self.offset:
            # Log rotated / truncated.
            self.offset = 0
            self.game = None

        if st.st_size > self.offset:
            start = self.offset
            if start == 0 and st.st_size > MAX_INITIAL_READ:
                start = st.st_size - MAX_INITIAL_READ
            try:
                with path.open("rb") as fh:
                    fh.seek(start)
                    data = fh.read()
            except OSError:
                return self.game
            self.offset = start + len(data)
            self._apply(data.decode("utf-8", errors="replace"))
        return self.game


_watchers: dict[str, LogWatcher] = {}


def _strip_emulator_from_title(title: str) -> str:
    # Typical titles: "Game - Eden", "Eden | Game", "Game | Eden"
    for sep in (" | ", " - ", " – ", " — "):
        if sep in title:
            left, right = [p.strip() for p in title.rsplit(sep, 1)]
            lowered = right.lower()
            if any(b in lowered for b in ("eden", "lichii", "eglantine", "yuzu", "sudachi", "ryujinx")):
                return left
            lowered = left.lower()
            if any(b in lowered for b in ("eden", "lichii", "eglantine", "yuzu", "sudachi", "ryujinx")):
                return right
    return title


class GameInfo:
    __slots__ = ("title_id", "name")

    def __init__(self, title_id: str, name: str):
        self.title_id = title_id.upper()
        self.name = name

    def __eq__(self, other):
        return (
            isinstance(other, GameInfo)
            and other.title_id == self.title_id
            and other.name == self.name
        )

    def __repr__(self):
        return f"GameInfo({self.title_id}, {self.name!r})"


def detect_game(emu: Emulator, pid: int | None) -> GameInfo | None:
    """Return the running game, or None when the emulator is in its menu."""
    logs = emu.log_files()
    if logs:
        try:
            logs.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        except OSError:
            pass
        watcher = _watchers.setdefault(str(emu.exe).lower(), LogWatcher())
        found = watcher.update(logs[0])
        if found:
            title_id, name = found
            entry = nup_entry(title_id)
            if entry and entry.get("name"):
                name = str(entry["name"])
            remember_title(title_id, name)
            return GameInfo(title_id, name)
        # A log exists and is up to date -> the emulator is in its menu.
        return None

    # Fallback: window title (only when no log file exists at all).
    if pid:
        title = window_title_for_pid(pid)
        if title:
            cleaned = _strip_emulator_from_title(title)
            lowered = cleaned.lower()
            if cleaned and not any(
                word in lowered
                for word in ("eden", "lichii", "eglantine", "yuzu", "sudachi", "ryujinx", "pokedinner")
            ):
                return GameInfo("", cleaned)
    return None


# --------------------------------------------------------------------------- #
# Icon resolution (public URL used directly by Discord)
# --------------------------------------------------------------------------- #

_icon_cache: dict[str, dict] | None = None
_icon_lock = threading.Lock()

ICON_SUCCESS_TTL = 7 * 24 * 3600
ICON_FAILURE_TTL = 24 * 3600


def load_icon_cache() -> dict[str, dict]:
    global _icon_cache
    with _icon_lock:
        if _icon_cache is None:
            data: dict[str, dict] = {}
            try:
                if ICONS_CACHE_PATH.exists():
                    raw = json.loads(ICONS_CACHE_PATH.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data = {str(k).upper(): v for k, v in raw.items() if isinstance(v, dict)}
            except Exception:
                data = {}
            _icon_cache = data
        return _icon_cache


def save_icon_cache() -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ICONS_CACHE_PATH.write_text(
            json.dumps(load_icon_cache(), indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def eden_slug(title: str) -> str:
    """Reproduce Eden's box art slug algorithm."""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = text.lower().replace(" ", "-")
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = text.strip("-")
    text = re.sub(r"-{2,}", "-", text)
    return text


def _url_ok(url: str, timeout: float = 4.0) -> bool:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "NS2Presence/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 300
    except Exception:
        return False


RE_SIS_ITEM = re.compile(
    r"<a href='\./detail\.php\?id=(\d+)'><img src='\./images/100/\d+\.png'[^>]*></a>"
    r"</td><td><center>(.*?)</center>",
    re.S,
)


def _norm_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _name_score(query: str, candidate: str) -> int:
    a, b = _norm_name(query), _norm_name(candidate)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 60 + int(35 * min(len(a), len(b)) / max(len(a), len(b)))
    ta, tb = set(re.findall(r"[a-z0-9]+", query.lower())), set(re.findall(r"[a-z0-9]+", candidate.lower()))
    if ta and tb:
        return int(70 * len(ta & tb) / len(ta | tb))
    return 0


def _sis_search(query: str) -> list[tuple[str, str]]:
    url = "https://switchiconshowdown.com/search.php?query=" + urllib.parse.quote(query)
    request = urllib.request.Request(url, headers={"User-Agent": "NS2Presence/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    return [
        (item_id, re.sub(r"<[^>]+>", "", name).strip())
        for item_id, name in RE_SIS_ITEM.findall(html)
    ]


def find_icon_on_switchiconshowdown(title: str) -> str | None:
    """Search switchiconshowdown.com (the icon database Eden also uses)."""
    if not title:
        return None
    queries = [title]
    if ":" in title:
        before = title.split(":", 1)[0].strip()
        if before and before.lower() != title.lower():
            queries.append(before)
    words = title.split()
    if len(words) > 3:
        queries.append(" ".join(words[:3]))

    best_id, best_score = None, 0
    for query in queries[:2]:
        for item_id, name in _sis_search(query):
            score = _name_score(title, name)
            if score > best_score:
                best_id, best_score = item_id, score
        if best_score >= 90:
            break
    if best_id and best_score >= 70:
        return f"https://switchiconshowdown.com/images/100/{best_id}.png"
    return None


NUP_SB_SIZE = 0x80
NUP_MAX_TOC = 64 * 1024 * 1024
NUP_MAX_META = 4 * 1024 * 1024
NUP_MAX_ICON = 2 * 1024 * 1024

RE_NUP_FILENAME_TID = re.compile(r"\[([0-9A-Fa-f]{16})\]")
RE_NUP_FILENAME_DOTTED = re.compile(r"(?:\(|\[)v(\d+(?:\.\d+){1,3})(?:\)|\])", re.I)
RE_NUP_FILENAME_HEX = re.compile(r"\[v(\d{3,10})\]", re.I)
RE_NUP_SQUARE = re.compile(r"\[(?:Game|Update|DLC|Base)\]", re.I)

_nup_db: dict | None = None
_nup_lock = threading.Lock()


def reload_runtime_cache() -> None:
    """Forget config / NUP / icon caches (used when a file changed on disk)."""
    global _config_cache, _nup_db, _icon_cache
    _config_cache = None
    _nup_db = None
    _icon_cache = None


def file_stamp(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _nup_icon_ext(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return None


def _nup_text(payload: bytes) -> str:
    return payload.decode("utf-8", "replace").replace("\x00", "").strip()


def _nup_version_text(value: int) -> str:
    return f"{(value >> 16) & 0xFFFF}.{(value >> 8) & 0xFF}.{value & 0xFF}"


def _nup_name_from_filename(path: Path) -> tuple[str, str, str]:
    """Return (title_id, name, version) guessed from the file name."""
    stem = path.stem
    title_id = ""
    found = RE_NUP_FILENAME_TID.search(stem)
    if found:
        title_id = found.group(1).upper()

    version = ""
    found = RE_NUP_FILENAME_DOTTED.search(stem)
    if found:
        version = found.group(1)
    else:
        found = RE_NUP_FILENAME_HEX.search(stem)
        if found:
            try:
                version = _nup_version_text(int(found.group(1)))
            except ValueError:
                version = ""

    name = RE_NUP_FILENAME_TID.sub(" ", stem)
    name = RE_NUP_FILENAME_DOTTED.sub(" ", name)
    name = RE_NUP_SQUARE.sub(" ", name)
    name = name.replace("_", " ")
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return title_id, name or stem, version


def _nup_read_tlvs(fh, offset: int, size: int) -> dict[int, bytes]:
    """Read a package metadata block (small) and split it into TLVs."""
    if not offset or not size or size > NUP_MAX_META:
        return {}
    try:
        fh.seek(offset)
        data = fh.read(size)
    except (OSError, ValueError):
        return {}

    tlvs: dict[int, bytes] = {}
    position = 0
    while position + 8 <= len(data):
        type_id, _reserved, length = struct.unpack_from("<HHI", data, position)
        position += 8
        if length > len(data) - position:
            break
        payload = data[position : position + length]
        position += length
        tlvs.setdefault(type_id, payload)
    return tlvs


def parse_nup(path: Path) -> dict | None:
    """Read title id, name, version and icon from a .nup without loading it all.

    The container stores a small metadata block per package (base, updates,
    DLC); only those blocks and the icon bytes are read, so even a 25 GB file
    is parsed instantly.
    """
    path = Path(path)
    try:
        stat = path.stat()
        file_size = stat.st_size
        file_time = stat.st_mtime
        with path.open("rb") as fh:
            superblock = fh.read(NUP_SB_SIZE)
            if len(superblock) < NUP_SB_SIZE or superblock[:4] != b"NUP\x01":
                return None
            _magic, _hsize, _head_version, toc_off, toc_size = struct.unpack_from(
                "<4sHHQQ", superblock, 0
            )
            if not toc_off or not toc_size or toc_size > NUP_MAX_TOC:
                return None
            if toc_off + toc_size > file_size:
                return None

            fh.seek(toc_off)
            toc = fh.read(toc_size)
            if len(toc) < 8 or toc[:4] != b"TOC\x01":
                return None
            package_count = struct.unpack_from("<I", toc, 4)[0]

            packages: list[tuple[int, int, dict[int, bytes]]] = []
            for index in range(package_count):
                base = 0x30 + index * 0x40
                if base + 0x30 > len(toc):
                    break
                title_id, _root, _exefs, _romfs, version, _kind, _flags = struct.unpack_from(
                    "<QIIIIBB", toc, base
                )
                meta_off, meta_size = struct.unpack_from("<QI", toc, base + 0x20)
                tlvs = _nup_read_tlvs(fh, meta_off, meta_size)
                packages.append((title_id, version, tlvs))
    except (OSError, struct.error, ValueError):
        return None

    file_title_id, file_name, file_version = _nup_name_from_filename(path)

    title_id_int = 0
    name = publisher = version = ""
    package_version = 0
    icon = b""

    # Base packages may carry title id 0 (Pokemon Pokopia does): the title id
    # is then taken from the update package, while name/icon stay in the base.
    for package_tid, package_ver, tlvs in packages:
        if not title_id_int and package_tid:
            title_id_int = package_tid
        if not name and 0x0005 in tlvs:
            name = _nup_text(tlvs[0x0005])
        if not publisher and 0x0006 in tlvs:
            publisher = _nup_text(tlvs[0x0006])
        if not icon and 0x0003 in tlvs:
            candidate = tlvs[0x0003]
            if _nup_icon_ext(candidate):
                icon = candidate
        if 0x0004 in tlvs:
            text_version = _nup_text(tlvs[0x0004])
            if text_version:
                version = text_version  # an update overrides the base version
        elif package_ver > package_version:
            package_version = package_ver

    if not title_id_int and file_title_id:
        title_id_int = int(file_title_id, 16)
    title_id = f"{title_id_int:016X}"

    if not name:
        name = file_name
    if not version:
        version = file_version
    if not version and package_version:
        version = _nup_version_text(package_version)

    entry: dict = {
        "title_id": title_id,
        "name": name[:120],
        "publisher": publisher[:120],
        "version": version,
        "path": str(path),
        "size": file_size,
        "mtime": file_time,
    }

    ext = _nup_icon_ext(icon)
    if ext and title_id_int:
        try:
            NUP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            icon_path = NUP_CACHE_DIR / f"{title_id}{ext}"
            keep = (
                icon_path.exists()
                and icon_path.stat().st_size == len(icon)
            )
            if not keep:
                icon_path.write_bytes(icon)
            entry["icon_file"] = str(icon_path)
            entry["icon_ext"] = ext
        except OSError as exc:
            log(f"could not write the icon for {name}: {exc}")
    return entry


def load_nup_db() -> dict:
    global _nup_db
    with _nup_lock:
        if _nup_db is None:
            data: dict = {"games_dir": "", "updated": 0, "games": {}}
            try:
                if NUP_DB_PATH.exists():
                    raw = json.loads(NUP_DB_PATH.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        data.update(raw)
            except Exception:
                pass
            if not isinstance(data.get("games"), dict):
                data["games"] = {}
            _nup_db = data
        return _nup_db


def save_nup_db(db: dict | None = None) -> None:
    if db is None:
        db = load_nup_db()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        NUP_DB_PATH.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log(f"could not write {NUP_DB_PATH.name}: {exc}")


def nup_entry(title_id: str) -> dict | None:
    title_id = (title_id or "").upper()
    if not title_id:
        return None
    entry = load_nup_db()["games"].get(title_id)
    return entry if isinstance(entry, dict) else None


def _jpeg_with_comment(data: bytes) -> bytes:
    """Insert a unique JPEG comment so catbox does not return an old URL.

    Catbox deduplicates identical files and reuses the first upload's name
    (which could end in .bin with a generic content type). A valid COM segment
    makes each upload unique while keeping the image untouched.
    """
    if data[:3] != b"\xff\xd8\xff":
        return data
    comment = b"NS2Presence " + uuid.uuid4().hex.encode("ascii")
    segment = b"\xff\xfe" + (len(comment) + 2).to_bytes(2, "big") + comment
    return data[:2] + segment + data[2:]


def upload_icon_to_catbox(icon_path: Path, timeout: float = 60.0) -> str | None:
    """Host an extracted icon publicly so Discord can download it.

    Discord only accepts public https URLs (local files and data: URIs are
    stripped), hence this upload. It is opt-in from the interface.
    """
    icon_path = Path(icon_path)
    try:
        data = icon_path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > 5 * 1024 * 1024:
        return None
    if icon_path.suffix.lower() in (".jpg", ".jpeg"):
        data = _jpeg_with_comment(data)

    boundary = "----NS2Presence" + uuid.uuid4().hex
    content_type = "image/png" if icon_path.suffix.lower() == ".png" else "image/jpeg"
    body = b"".join(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="reqtype"\r\n\r\n'
            "fileupload\r\n".encode(),
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="fileToUpload"; '
                f'filename="{icon_path.name}"\r\nContent-Type: {content_type}\r\n\r\n'
            ).encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        "https://catbox.moe/user/api.php",
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "NS2Presence/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace").strip()
    except Exception as exc:
        log(f"icon upload failed ({icon_path.name}): {exc}")
        return None

    if text.startswith("https://") and " " not in text:
        return text
    log(f"unexpected catbox response: {text[:200]}")
    return None


_scan_lock = threading.Lock()


def scan_nup_library(folder: str | Path, upload: bool | None = None, progress=None) -> dict:
    """Scan every .nup file of a folder, cache metadata and (optionally) icons."""
    cfg = load_config()
    if upload is None:
        upload = bool(cfg.get("upload_icons", True))

    root = Path(str(folder)).expanduser()
    result: dict = {
        "folder": str(root),
        "files": 0,
        "games": [],
        "errors": [],
        "uploaded": 0,
    }
    if not str(folder).strip() or not root.is_dir():
        result["errors"].append(f"Folder not found: {root}")
        return result

    def report(text: str) -> None:
        if progress:
            try:
                progress(text)
            except Exception:
                pass

    with _scan_lock:
        db = load_nup_db()
        games = db["games"]
        try:
            files = sorted(root.rglob("*.nup"))
        except OSError as exc:
            result["errors"].append(f"Cannot read: {exc}")
            return result
        result["files"] = len(files)

        for path in files:
            report(f"Reading {path.name}")
            entry = parse_nup(path)
            if not entry:
                result["errors"].append(f"Unreadable file: {path.name}")
                continue

            title_id = entry["title_id"]
            previous = games.get(title_id, {})
            if previous.get("icon_url"):
                entry["icon_url"] = previous["icon_url"]
                entry["icon_uploaded"] = previous.get("icon_uploaded", 0)
            games[title_id] = entry
            result["games"].append(entry)

            if upload and not entry.get("icon_url") and entry.get("icon_file"):
                report(f"Uploading the icon of {entry['name']} to catbox.moe")
                url = upload_icon_to_catbox(Path(entry["icon_file"]))
                if url:
                    entry["icon_url"] = url
                    entry["icon_uploaded"] = time.time()
                    result["uploaded"] += 1
                    log(f".nup icon uploaded for {entry['name']}: {url}")

        db["games_dir"] = str(root)
        db["updated"] = time.time()
        save_nup_db(db)

    result["games"].sort(key=lambda item: item.get("name", "").lower())
    log(
        f"scan .nup: {result['files']} file(s), {len(result['games'])} game(s), "
        f"{result['uploaded']} icon(s) uploaded"
    )
    return result


def resolve_game_icon(title_id: str, title: str) -> str | None:
    """Return a public image URL for the game (Discord downloads it itself)."""
    cfg = load_config()
    title_id = (title_id or "").upper()

    # 1) user override in config.json / icons.json
    override = cfg.get("icons", {}).get(title_id)
    if not override and ICONS_MAP_PATH.exists():
        try:
            extra = json.loads(ICONS_MAP_PATH.read_text(encoding="utf-8"))
            if isinstance(extra, dict):
                override = extra.get(title_id) or extra.get(title_id.lower())
        except Exception:
            pass
    if override:
        return str(override)

    # 2) the real icon extracted from the user's .nup library (already hosted)
    entry = nup_entry(title_id)
    if entry and entry.get("icon_url"):
        return str(entry["icon_url"])

    if not title_id and not title:
        return None

    cache_key = title_id or ("name:" + _norm_name(title))
    cache = load_icon_cache()
    entry = cache.get(cache_key)
    if entry and time.time() - float(entry.get("checked", 0)) < (
        ICON_SUCCESS_TTL if entry.get("url") else ICON_FAILURE_TTL
    ):
        return entry.get("url") or None

    found = find_icon_on_switchiconshowdown(title)

    if not found and title_id:
        candidates = [f"https://tinfoil.media/ti/{title_id}/256/256/"]
        slug = eden_slug(title) if title else ""
        if slug:
            candidates.append(
                "https://raw.githubusercontent.com/eden-emulator/boxart/"
                f"refs/heads/master/img/{slug}.png"
            )
        for url in candidates:
            if _url_ok(url):
                found = url
                break

    cache[cache_key] = {"url": found, "checked": time.time()}
    save_icon_cache()
    if found:
        log(f"icon found for {title_id or title}: {found}")
    else:
        log(f"no public icon for {title_id} ({title})")
    return found


# --------------------------------------------------------------------------- #
# Discord IPC client
# --------------------------------------------------------------------------- #

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4


def encode_frame(opcode: int, payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return opcode.to_bytes(4, "little") + len(data).to_bytes(4, "little") + data


def decode_frames(buffer: bytes) -> tuple[list[tuple[int, dict]], bytes]:
    """Decode as many complete frames as possible. Returns (frames, rest)."""
    frames: list[tuple[int, dict]] = []
    while len(buffer) >= 8:
        opcode = int.from_bytes(buffer[0:4], "little")
        length = int.from_bytes(buffer[4:8], "little")
        if len(buffer) < 8 + length:
            break
        payload = buffer[8 : 8 + length]
        buffer = buffer[8 + length :]
        try:
            frames.append((opcode, json.loads(payload.decode("utf-8", errors="replace"))))
        except Exception:
            frames.append((opcode, {}))
    return frames, buffer


class DiscordIPC:
    """Minimal Discord RPC client (named pipe + JSON frames)."""

    def __init__(self, client_id: str):
        self.client_id = str(client_id)
        self.pipe = None
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._connected = False
        self._last_error = 0.0

    # -- connection -------------------------------------------------------- #
    def connect(self) -> bool:
        if self._connected:
            return True
        if time.time() - self._last_error < 5:
            return False

        for index in range(10):
            path = rf"\\.\pipe\discord-ipc-{index}"
            try:
                pipe = open(path, "r+b", buffering=0)
            except OSError:
                continue
            try:
                pipe.write(encode_frame(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id}))
            except OSError:
                try:
                    pipe.close()
                except OSError:
                    pass
                continue

            self.pipe = pipe
            self._connected = True
            self._reader = threading.Thread(target=self._read_loop, args=(pipe,), daemon=True)
            self._reader.start()
            log(f"connected to Discord ({path})")
            return True

        self._last_error = time.time()
        return False

    def close(self) -> None:
        self._connected = False
        pipe, self.pipe = self.pipe, None
        if pipe:
            try:
                pipe.close()
            except OSError:
                pass

    # -- reading ----------------------------------------------------------- #
    def _read_loop(self, pipe) -> None:
        buffer = b""
        try:
            handle = msvcrt.get_osfhandle(pipe.fileno())
        except Exception:
            handle = None
        while self._connected and self.pipe is pipe:
            # Never block inside read(): a blocked read would also block
            # close() on Windows when Discord stops. Peek first instead.
            if handle is not None:
                available = wt.DWORD(0)
                try:
                    ok = kernel32.PeekNamedPipe(
                        handle, None, 0, None, ctypes.byref(available), None
                    )
                except Exception:
                    break
                if not ok:
                    break
                if available.value == 0:
                    time.sleep(0.2)
                    continue
                to_read = min(4096, int(available.value))
            else:
                to_read = 4096
            try:
                chunk = pipe.read(to_read)
            except Exception:
                break
            if not chunk:
                break
            buffer += chunk
            frames, buffer = decode_frames(buffer)
            for opcode, payload in frames:
                if opcode == OP_PING:
                    try:
                        pipe.write(encode_frame(OP_PONG, payload))
                    except Exception:
                        pass
                elif opcode == OP_CLOSE:
                    log(f"Discord closed the connection: {payload}")
                    self._connected = False
                    break
                elif opcode == OP_FRAME and payload.get("evt") == "ERROR":
                    log(f"Discord error: {payload.get('data')}")
        if self.pipe is pipe:
            self._connected = False

    # -- commands ---------------------------------------------------------- #
    def _send(self, opcode: int, payload: dict) -> bool:
        with self._lock:
            if not self._connected or not self.pipe:
                return False
            try:
                self.pipe.write(encode_frame(opcode, payload))
                return True
            except Exception as exc:
                log(f"Discord write failed: {exc}")
                self._connected = False
                return False

    def set_activity(self, activity: dict | None) -> bool:
        if not self.connect():
            return False
        payload = {
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        }
        return self._send(OP_FRAME, payload)


# --------------------------------------------------------------------------- #
# Presence formatting
# --------------------------------------------------------------------------- #

def _clip(text: str, size: int = 128) -> str:
    text = str(text).strip()
    return text if len(text) <= size else text[: size - 1] + "…"


def build_activity(game: GameInfo | None, emu: Emulator, started: float) -> dict:
    cfg = load_config()
    emu_label = emu.label if cfg.get("show_version", True) else emu.short_label
    state_playing = str(cfg.get("state_playing", DEFAULT_CONFIG["state_playing"]))
    details_playing = str(cfg.get("details_playing", DEFAULT_CONFIG["details_playing"]))
    state_idle = str(cfg.get("state_idle", DEFAULT_CONFIG["state_idle"]))
    details_idle = str(cfg.get("details_idle", DEFAULT_CONFIG["details_idle"]))

    if game is None:
        activity = {
            "name": "Switch 2",
            "details": _clip(details_idle.format(game="", emulator=emu_label)),
            "state": _clip(state_idle.format(game="", emulator=emu_label)),
            "assets": {
                "large_image": emu.logo,
                "large_text": _clip(emu_label),
            },
            "instance": False,
        }
        if cfg.get("show_timer", True) and started:
            activity["timestamps"] = {"start": int(started)}
        return activity

    icon = resolve_game_icon(game.title_id, game.name) or emu.logo
    activity = {
        "name": _clip(game.name),
        "details": _clip(details_playing.format(game=game.name, emulator=emu_label)),
        "state": _clip(state_playing.format(game=game.name, emulator=emu_label)),
        "assets": {
            "large_image": icon,
            "large_text": _clip(game.name),
            "small_image": emu.logo,
            "small_text": _clip(emu_label),
        },
        "instance": False,
    }
    if cfg.get("show_timer", True):
        activity["timestamps"] = {"start": int(started)}
    return activity


# --------------------------------------------------------------------------- #
# Built-in Discord presence of the emulators (avoid two presences fighting)
# --------------------------------------------------------------------------- #

RE_PRESENCE_LINE = re.compile(r"(?m)^enable_discord_presence=(true|false)\s*$")


def disable_builtin_presence(emulators: list[Emulator]) -> None:
    cfg = load_config()
    if not cfg.get("disable_builtin_presence", True):
        return
    snapshot = process_snapshot()
    running = {path for path in snapshot if not path.startswith("<name>")}

    for emu in emulators:
        if str(emu.exe).lower() in running:
            continue
        for config_dir in emu.config_dirs:
            ini = config_dir / "qt-config.ini"
            if not ini.exists():
                continue
            try:
                text = ini.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if "enable_discord_presence=true" not in text:
                continue
            backup = ini.with_suffix(".ini.bak-presence")
            try:
                if not backup.exists():
                    backup.write_text(text, encoding="utf-8")
                new_text = RE_PRESENCE_LINE.sub("enable_discord_presence=false", text)
                ini.write_text(new_text, encoding="utf-8")
                log(f"built-in presence disabled: {ini}")
            except Exception as exc:
                log(f"could not modify {ini}: {exc}")


# --------------------------------------------------------------------------- #
# Tray icon + autostart
# --------------------------------------------------------------------------- #

STARTUP_NAME = "NS2 Presence.lnk"


def startup_shortcut_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", ""))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_NAME


def startup_enabled() -> bool:
    return startup_shortcut_path().exists()


def _create_startup_shortcut() -> None:
    target = Path(sys.executable)
    if getattr(sys, "frozen", False):
        arguments = ""
    else:
        # Run the script with pythonw.exe so no console window appears.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        target = pythonw
        arguments = f'"{Path(__file__).resolve()}"'

    link = startup_shortcut_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut('{link}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '{arguments}'; "
        f"$s.WorkingDirectory = '{BASE_DIR}'; "
        "$s.WindowStyle = 7; "
        "$s.Description = 'Discord Rich Presence for Switch 2 emulators'; "
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def set_startup(enabled: bool) -> None:
    link = startup_shortcut_path()
    if enabled:
        _create_startup_shortcut()
        log("auto-start with Windows enabled")
    else:
        try:
            if link.exists():
                link.unlink()
            log("auto-start with Windows disabled")
        except OSError as exc:
            log(f"could not remove the startup shortcut: {exc}")


class PresenceState:
    def __init__(self):
        self.status = "Waiting"
        self.icon = None
        self._icon_thread = None


def start_tray(state: PresenceState) -> None:
    try:
        import pystray
        from PIL import Image
    except Exception as exc:
        log(f"notification icon unavailable: {exc}")
        return

    try:
        if ICON_PNG.exists():
            image = Image.open(ICON_PNG)
        else:
            image = Image.new("RGBA", (64, 64), (230, 0, 18, 255))
    except Exception:
        image = Image.new("RGBA", (64, 64), (230, 0, 18, 255))

    def open_folder(_icon=None, _item=None):
        try:
            os.startfile(str(BASE_DIR))
        except Exception:
            pass

    def toggle_startup(icon, _item=None):
        set_startup(not startup_enabled())
        try:
            icon.update_menu()
        except Exception:
            pass

    def quit_app(icon, _item=None):
        state.status = "Stopping"
        try:
            icon.stop()
        except Exception:
            pass
        os._exit(0)

    def status_text(_item=None):
        return state.status

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open interface", lambda *_: open_gui()),
        pystray.MenuItem("Open folder", open_folder),
        pystray.MenuItem("Discord diagnostics", lambda *_: diagnose()),
        pystray.MenuItem(
            "Start with Windows",
            toggle_startup,
            checked=lambda _item: startup_enabled(),
        ),
        pystray.MenuItem("Reconnect Discord", lambda *_: reconnect_hint()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )

    icon = pystray.Icon("ns2presence", image, APP_NAME, menu)
    state.icon = icon
    if state._icon_thread is None:
        state._icon_thread = threading.Thread(target=icon.run, daemon=True)
        state._icon_thread.start()


_reconnect_event = threading.Event()


def reconnect_hint() -> None:
    _reconnect_event.set()


def diagnose(show_box: bool = True) -> str:
    """Check the Discord connection and send a test presence."""
    cfg = load_config()
    client = DiscordIPC(cfg.get("client_id") or DEFAULT_CONFIG["client_id"])
    lines: list[str] = []

    if client.connect():
        time.sleep(1.0)
        if client._connected:
            lines.append("Discord connection: OK")
            test = {
                "name": "Test NS2 Presence",
                "details": "Presence test",
                "state": "NS2 Presence is running",
                "assets": {"large_image": DEFAULT_LOGO, "large_text": "NS2 Presence"},
                "instance": False,
            }
            client.set_activity(test)
            lines.append("Test presence sent (check your Discord profile).")
        else:
            lines.append("Connection refused by Discord (invalid client_id?).")
    else:
        lines.append("Discord is not running (or not installed).")
        lines.append("Start Discord, then try again.")
    client.close()

    text = "\n".join(lines)
    log("diagnostics: " + text.replace("\n", " | "))
    if show_box:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)
        except Exception:
            pass
    return text


# --------------------------------------------------------------------------- #
# Graphical interface (tkinter)
# --------------------------------------------------------------------------- #

_gui_lock = threading.Lock()
_gui_open = False


def open_gui(initial_scan: bool = False) -> None:
    """Open the library window (safe to call from the tray or main thread)."""
    global _gui_open
    with _gui_lock:
        if _gui_open:
            return
        _gui_open = True
    threading.Thread(target=_gui_thread, args=(initial_scan,), daemon=True).start()


def _gui_thread(initial_scan: bool) -> None:
    global _gui_open
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except Exception as exc:
        log(f"graphical interface unavailable: {exc}")
        _gui_open = False
        return

    try:
        root = tk.Tk()
    except Exception as exc:
        log(f"could not open the window: {exc}")
        _gui_open = False
        return

    root.title(APP_NAME)
    root.geometry("900x700")
    root.minsize(780, 600)
    try:
        icon_path = RES_DIR / "assets" / "icon.ico"
        if icon_path.exists():
            root.iconbitmap(str(icon_path))
    except Exception:
        pass

    cfg = load_config()
    folder_var = tk.StringVar(value=str(cfg.get("games_dir") or ""))
    upload_var = tk.BooleanVar(value=bool(cfg.get("upload_icons", True)))
    status_var = tk.StringVar(value="Choose your games folder, then click Scan.")

    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        try:
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        except Exception:
            continue
    style.configure("Treeview", rowheight=44)

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)

    header = ttk.Frame(outer)
    header.pack(fill="x", pady=(0, 10))
    ttk.Label(
        header,
        text="Game library",
        font=("Segoe UI", 14, "bold"),
    ).pack(anchor="w")
    ttk.Label(
        header,
        text="NS2 Presence reads .nup files to show the real game name and icon in Discord.",
        foreground="#555555",
    ).pack(anchor="w", pady=(2, 0))

    folder_row = ttk.Frame(outer)
    folder_row.pack(fill="x", pady=(0, 8))
    ttk.Label(folder_row, text="Games folder:").pack(side="left")
    folder_entry = ttk.Entry(folder_row, textvariable=folder_var)
    folder_entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
    browse_btn = ttk.Button(folder_row, text="Browse…")
    browse_btn.pack(side="left")
    scan_btn = ttk.Button(folder_row, text="Scan")
    scan_btn.pack(side="left", padx=(6, 0))

    options = ttk.Frame(outer)
    options.pack(fill="x", pady=(0, 8))
    upload_check = ttk.Checkbutton(
        options,
        text="Upload extracted icons to catbox.moe so they show in Discord",
        variable=upload_var,
    )
    upload_check.pack(anchor="w")
    ttk.Label(
        options,
        text="Discord cannot read a local file: the icon must be hosted. Only game artwork is uploaded.",
        foreground="#777777",
    ).pack(anchor="w", padx=(22, 0))

    bottom = ttk.Frame(outer)
    bottom.pack(side="bottom", fill="x")
    status_label = ttk.Label(bottom, textvariable=status_var, foreground="#333333")
    status_label.pack(side="left")
    close_btn = ttk.Button(bottom, text="Close")
    close_btn.pack(side="right")
    test_btn = ttk.Button(bottom, text="Test Discord")
    test_btn.pack(side="right", padx=(0, 6))
    open_btn = ttk.Button(bottom, text="Open folder")
    open_btn.pack(side="right", padx=(0, 6))

    log_frame = ttk.LabelFrame(outer, text="Log", padding=6)
    log_frame.pack(side="bottom", fill="x", pady=(10, 8))
    log_box = scrolledtext.ScrolledText(log_frame, height=6, wrap="word", state="disabled")
    log_box.pack(fill="both", expand=True)

    tree_frame = ttk.Frame(outer)
    tree_frame.pack(fill="both", expand=True)
    columns = ("name", "tid", "version", "icon")
    tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", height=10)
    tree.heading("#0", text="")
    tree.heading("name", text="Game")
    tree.heading("tid", text="Title ID")
    tree.heading("version", text="Version")
    tree.heading("icon", text="Icon")
    tree.column("#0", width=88, minwidth=88, anchor="center", stretch=False)
    tree.column("name", width=400, anchor="w")
    tree.column("tid", width=160, anchor="center")
    tree.column("version", width=90, anchor="center")
    tree.column("icon", width=130, anchor="center")
    tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    tree_scroll.pack(side="right", fill="y")

    thumbs: list = []

    def ui_log(message: str) -> None:
        def append() -> None:
            try:
                log_box.configure(state="normal")
                log_box.insert("end", str(message) + "\n")
                log_box.see("end")
                log_box.configure(state="disabled")
            except Exception:
                pass

        try:
            root.after(0, append)
        except Exception:
            pass

    def thumbnail(icon_file: str):
        try:
            from PIL import Image, ImageTk

            image = Image.open(icon_file)
            image.thumbnail((38, 38))
            photo = ImageTk.PhotoImage(image)
            thumbs.append(photo)
            return photo
        except Exception:
            return None

    def add_game(game: dict) -> None:
        icon_file = str(game.get("icon_file") or "")
        image = thumbnail(icon_file) if icon_file else None
        if game.get("icon_url"):
            icon_state = "uploaded"
        elif icon_file:
            icon_state = "local"
        else:
            icon_state = "none"
        tree.insert(
            "",
            "end",
            image=image or "",
            values=(
                game.get("name") or "?",
                game.get("title_id") or "?",
                game.get("version") or "-",
                icon_state,
            ),
        )

    def fill_from_db() -> None:
        games = sorted(
            load_nup_db()["games"].values(),
            key=lambda item: str(item.get("name", "")).lower(),
        )
        for game in games:
            if isinstance(game, dict):
                add_game(game)
        status_var.set(f"{len(games)} game(s) known.")
        if games:
            ui_log(f"{len(games)} game(s) already scanned in the library.")

    def done(result: dict) -> None:
        tree.delete(*tree.get_children())
        thumbs.clear()
        for game in result.get("games", []):
            add_game(game)
        errors = result.get("errors", [])
        for error in errors[:10]:
            ui_log("  ! " + str(error))
        status_var.set(
            f"{len(result.get('games', []))} game(s) • "
            f"{result.get('uploaded', 0)} icon(s) uploaded • "
            f"{len(errors)} error(s)"
        )
        scan_btn.config(state="normal")
        browse_btn.config(state="normal")

    def work(folder: str, upload: bool) -> None:
        try:
            result = scan_nup_library(folder, upload=upload, progress=ui_log)
        except Exception as exc:
            log("error during the .nup scan:\n" + traceback.format_exc())
            result = {"games": [], "errors": [str(exc)], "uploaded": 0}
        try:
            root.after(0, lambda: done(result))
        except Exception:
            pass

    def start_scan(*_args) -> None:
        folder = folder_var.get().strip().strip('"')
        if not folder:
            messagebox.showwarning(APP_NAME, "Choose the folder that contains your .nup games first.")
            return
        if not Path(folder).is_dir():
            messagebox.showerror(APP_NAME, f"Folder not found:\n{folder}")
            return
        current = load_config()
        current["games_dir"] = folder
        current["upload_icons"] = bool(upload_var.get())
        save_config(current)
        scan_btn.config(state="disabled")
        browse_btn.config(state="disabled")
        status_var.set("Scanning .nup files…")
        ui_log(f"Scanning {folder}")
        tree.delete(*tree.get_children())
        thumbs.clear()
        threading.Thread(
            target=work, args=(folder, bool(upload_var.get())), daemon=True
        ).start()

    def browse() -> None:
        initial = folder_var.get().strip()
        start_dir = initial if initial and Path(initial).is_dir() else str(Path.home())
        chosen = filedialog.askdirectory(
            title="Folder containing the .nup games", initialdir=start_dir
        )
        if chosen:
            folder_var.set(chosen.replace("/", "\\"))

    def open_folder() -> None:
        folder = folder_var.get().strip() or str(BASE_DIR)
        try:
            os.startfile(folder)
        except Exception as exc:
            ui_log(f"Cannot open {folder}: {exc}")

    def test_discord() -> None:
        text = diagnose(show_box=False)
        messagebox.showinfo(APP_NAME, text)

    browse_btn.config(command=browse)
    scan_btn.config(command=start_scan)
    folder_entry.bind("<Return>", start_scan)
    open_btn.config(command=open_folder)
    test_btn.config(command=test_discord)
    close_btn.config(command=root.destroy)

    fill_from_db()

    def on_close() -> None:
        global _gui_open
        _gui_open = False
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", on_close)

    if initial_scan and folder_var.get().strip():
        root.after(150, start_scan)

    try:
        root.mainloop()
    finally:
        _gui_open = False


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

def single_instance() -> bool:
    """True when this is the only running instance."""
    kernel32.CreateMutexW.restype = wt.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\NS2PresenceSingleton")
    if not handle:
        return True
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    if already:
        return False
    globals()["_MUTEX_HANDLE"] = handle  # keep the handle alive
    return True


def _nup_startup_sync(folder: str, upload: bool) -> None:
    try:
        result = scan_nup_library(folder, upload=upload)
        if result.get("games"):
            log(f".nup library ready: {len(result['games'])} game(s)")
    except Exception:
        log("error during the .nup scan at startup:\n" + traceback.format_exc())


def run(cfg: dict, state: PresenceState) -> None:
    client = DiscordIPC(cfg.get("client_id") or DEFAULT_CONFIG["client_id"])
    emulators = discover_emulators()
    apply_overrides(emulators, cfg)
    scan_logs_for_titles(emulators)
    disable_builtin_presence(emulators)

    log(f"{len(emulators)} emulator(s) monitored: " + ", ".join(e.label for e in emulators))

    games_dir = str(cfg.get("games_dir") or "").strip()
    if games_dir and Path(games_dir).is_dir():
        threading.Thread(
            target=_nup_startup_sync,
            args=(games_dir, bool(cfg.get("upload_icons", True))),
            daemon=True,
        ).start()
    elif not games_dir:
        log("no games folder configured (open the interface to choose one)")

    current_key = None
    started = 0.0
    last_discovery = time.time()
    last_builtin_check = time.time()
    scan_seconds = max(5, int(cfg.get("scan_seconds", 30)))
    last_stamp = (file_stamp(CONFIG_PATH), file_stamp(NUP_DB_PATH))
    force_refresh = False

    while True:
        try:
            stamp = (file_stamp(CONFIG_PATH), file_stamp(NUP_DB_PATH))
            if stamp != last_stamp:
                last_stamp = stamp
                reload_runtime_cache()
                cfg = load_config()
                scan_seconds = max(5, int(cfg.get("scan_seconds", 30)))
                force_refresh = True
                log("configuration reloaded")

            if time.time() - last_discovery > scan_seconds:
                previous = {str(e.exe).lower(): e.label for e in emulators}
                emulators = discover_emulators()
                apply_overrides(emulators, cfg)
                current = {str(e.exe).lower(): e.label for e in emulators}
                for exe, label in current.items():
                    if exe not in previous:
                        log(f"new emulator detected: {label} ({exe})")
                for exe, label in previous.items():
                    if exe not in current:
                        log(f"emulator gone: {label} ({exe})")
                last_discovery = time.time()

            if time.time() - last_builtin_check > 300:
                disable_builtin_presence(emulators)
                last_builtin_check = time.time()

            snapshot = process_snapshot()
            running: list[Emulator] = []
            for emu in emulators:
                entry = snapshot.get(str(emu.exe).lower())
                if entry:
                    running.append(emu)
                    continue
                # fallback when the path could not be read
                entry = snapshot.get(f"<name>{emu.exe.name.lower()}")
                if entry:
                    running.append(emu)

            if running:
                emu = running[0]
                entry = snapshot.get(str(emu.exe).lower()) or snapshot.get(
                    f"<name>{emu.exe.name.lower()}"
                )
                pid = entry[0] if entry else None

                game = detect_game(emu, pid)
                key = (str(emu.exe).lower(), game.title_id if game else None, game.name if game else None)
                changed = key != current_key
                if changed:
                    current_key = key
                    started = time.time()
                    if game:
                        state.status = f"{game.name} - {emu.label}"
                    else:
                        state.status = f"Menu - {emu.label}"
                    log(f"game detected: {game!r} on {emu.label}")

                if changed or force_refresh or not client._connected:
                    if not client._connected:
                        client.connect()
                    client.set_activity(
                        build_activity(game, emu, started)
                        if (game or cfg.get("idle_presence", True))
                        else None
                    )
                force_refresh = False

                if _reconnect_event.is_set():
                    _reconnect_event.clear()
                    client.close()
                    client.connect()
                    client.set_activity(build_activity(game, emu, started))
            else:
                if current_key is not None:
                    log("no active emulator, presence cleared")
                    client.set_activity(None)
                    current_key = None
                    started = 0.0
                    state.status = "Waiting for an emulator"
                    client.close()
                elif _reconnect_event.is_set():
                    _reconnect_event.clear()
                    client.close()
                force_refresh = False

            time.sleep(max(1, int(cfg.get("poll_seconds", 3))))
        except Exception:
            log("error in the loop:\n" + traceback.format_exc())
            time.sleep(5)


# --------------------------------------------------------------------------- #
# Self test
# --------------------------------------------------------------------------- #

def selftest() -> int:
    cfg = load_config()
    emulators = discover_emulators()
    apply_overrides(emulators, cfg)
    scan_logs_for_titles(emulators)

    print(f"Base: {BASE_DIR}")
    print(f"Emulators found: {len(emulators)}")
    snapshot = process_snapshot()
    for emu in emulators:
        running = str(emu.exe).lower() in snapshot
        print(f"  - {emu.label}")
        print(f"      exe     : {emu.exe}")
        print(f"      brand   : {emu.brand}   version: {emu.version or '(unknown)'}")
        print(f"      active  : {'yes' if running else 'no'}")
        print(f"      logs    : {[str(p) for p in emu.log_files()][:3]}")
        game = detect_game(emu, snapshot.get(str(emu.exe).lower(), (None,))[0])
        print(f"      game    : {game!r}")
        if game:
            print(f"      icon    : {resolve_game_icon(game.title_id, game.name)}")

    print("\nDiscord IPC protocol test:")
    payload = {"cmd": "SET_ACTIVITY", "args": {"pid": 1, "activity": {"details": "Test"}}, "nonce": "x"}
    frame = encode_frame(OP_FRAME, payload)
    frames, rest = decode_frames(frame + b"\x00" * 3)
    print(f"  encoded frame : {len(frame)} bytes")
    print(f"  decoded frame : {frames}")
    print(f"  remaining     : {len(rest)} bytes")
    assert frames and frames[0][1]["cmd"] == "SET_ACTIVITY"
    print("  OK")

    print("\nTitle cache:")
    for tid, name in sorted(load_titles_cache().items()):
        print(f"  {tid} = {name}")

    db = load_nup_db()
    print(f"\n.nup library: {len(db.get('games', {}))} game(s)")
    print(f"  folder  : {db.get('games_dir') or '(not configured)'}")
    for tid, entry in sorted(db.get("games", {}).items()):
        icon = entry.get("icon_url") or entry.get("icon_file") or "none"
        print(f"  {tid} = {entry.get('name')} v{entry.get('version') or '?'} icon={icon}")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    global DEBUG
    raw_args = sys.argv[1:]
    args = [a.lower() for a in raw_args]
    cfg = load_config()
    DEBUG = bool(cfg.get("debug")) or "--debug" in args

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if "--selftest" in args:
        try:
            return selftest()
        except Exception:
            traceback.print_exc()
            return 1

    if "--diagnose" in args:
        print(diagnose(show_box=False))
        return 0

    if "--scan-games" in args:
        folder = ""
        for index, arg in enumerate(raw_args):
            if arg.lower() == "--scan-games" and index + 1 < len(raw_args):
                following = raw_args[index + 1]
                if not following.startswith("--"):
                    folder = following
        folder = folder or str(cfg.get("games_dir") or "")
        upload = bool(cfg.get("upload_icons", True)) and "--no-upload" not in args
        result = scan_nup_library(folder, upload=upload, progress=lambda text: print("  " + text))
        print(f"Folder   : {result['folder']}")
        print(f"Files    : {result['files']}")
        for game in result["games"]:
            icon = game.get("icon_url") or game.get("icon_file") or "none"
            print(
                f"  {game['title_id']}  {game['name']}  "
                f"v{game.get('version') or '?'}  icon={icon}"
            )
        for error in result["errors"]:
            print("  ! " + error)
        return 0

    if "--gui" in args:
        started_here = False
        state = PresenceState()
        if single_instance():
            started_here = True
            if "--no-tray" not in args:
                start_tray(state)
        open_gui(initial_scan="--scan" in args)
        while _gui_open:
            time.sleep(0.25)
        if started_here:
            try:
                run(cfg, state)
            except KeyboardInterrupt:
                pass
            except Exception:
                log("stopped due to an error:\n" + traceback.format_exc())
                return 1
        return 0

    if not single_instance():
        log("NS2 Presence is already running, second instance ignored")
        return 0

    log(f"--- starting {APP_NAME} ---")

    state = PresenceState()
    if "--no-tray" not in args:
        start_tray(state)

    try:
        run(cfg, state)
    except KeyboardInterrupt:
        pass
    except Exception:
        log("stopped due to an error:\n" + traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
