#!/usr/bin/env python3
"""usbvault: keep ~/Documents and ~/Downloads of a Mac, an Ubuntu PC and an
EndeavourOS laptop together on one USB drive named VAULT, without ever
deleting anything.

  init PATH        prepare the USB drive (exFAT, named VAULT)
  first-run        first run on this computer: tidy up, then copy (Mac) or merge (Linux)
  sync             one pass of what the daemon does, by hand
  watch            the daemon: tidy Documents/Downloads every minute, sync when VAULT is in
  install-daemon   run 'watch' in the background (launchd on macOS, systemd on Linux)
  status           this computer, the USB and what is still pending
  verify           re-read every file on the USB and compare checksums
  extras add PATH  also back up a folder or file (extras list / extras remove PATH)
  config           show the settings file

Rules, the same on every computer:
  * new files and folders are copied by name; when a file was edited, the
    newest version wins and the older one is kept, never overwritten
  * files not used for 6 months move to ~/Documents/FreezeSolo
  * files with identical content: the newest copy stays where it is, older
    copies move to ~/Documents/AttackOfTheClones (listed in duplicates.csv)
  * DICOM studies move to ~/Documents/OldWorld and are never copied
  * videos, ISOs, installers and archives over 1 GB are never copied
  * ~/Documents/code and git repositories are left alone
  * logs and a copy of the database live in ~/Documents/TimeIsRelative
  * extra folders/files you list go to VAULT/Extras/<computer>/...; they are
    copied (same exclusions inside folders) but never moved
  * the Mac only sends to the USB; Ubuntu and EndeavourOS sync both ways
  * nothing is ever deleted, and the databases only add and update rows

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import fnmatch
import hashlib
import json
import mimetypes
import os
import plistlib
import re
import shutil
import signal
import socket
import sqlite3
import stat as stat_mod
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no locking or cache hints
    fcntl = None

VAULT_DIR = ".vault"
MARKER = "vault.json"
CATALOG = "catalog.sqlite"
TMP_PREFIX = ".vaulttmp-"
SCRIPT_NAME = "usbvault.py"
CHUNK = 4 * 1024 * 1024
MTIME_TOLERANCE = 2.0  # FAT-family timestamps are coarse
STAMP_FMT = "%Y%m%d-%H%M%S"
SCHEMA_VERSION = "3"
CONFIG_VERSION = 3
HOST = os.environ.get("USBVAULT_HOST") or socket.gethostname().split(".")[0] or "unknown-host"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Paths are written as "keys", relative to your home: "Documents/Facturas/enero.xml".
# The USB uses the same keys, so VAULT/Documents/... mirrors ~/Documents/...
FREEZE_KEY = "Documents/FreezeSolo"            # not used for 6 months
CLONES_KEY = "Documents/AttackOfTheClones"     # older copies of duplicate content
TIME_KEY = "Documents/TimeIsRelative"          # logs, local database, catalogue backup
OLD_KEY = "Documents/OldWorld"                 # DICOM studies, never copied
CSV_KEY = f"{CLONES_KEY}/duplicates.csv"       # generated report, written on each side
NO_SYNC_KEYS = {TIME_KEY, OLD_KEY, CSV_KEY}
# Extra folders/files you list (extras add PATH) go to VAULT/Extras/<computer>/<path
# from your home>; paths outside your home go under .../_root/<absolute path>.
EXTRAS = "Extras"
ROOT_MARK = "_root"

# OS clutter that is never copied.
JUNK_NAMES = {
    ".DS_Store", ".localized", "Icon\r", "Thumbs.db", "desktop.ini",
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", "System Volume Information", "$RECYCLE.BIN", "lost+found",
}
# Downloads still in progress and office lock files: ignored until finished.
PARTIAL_PATTERNS = ["*.crdownload", "*.part", "*.partial", "*.download", "*.opdownload",
                    "*.tmp", "~$*", ".~lock.*#"]
EXCLUDED_FILES = ["*.mkv", "*.mp4", "*.mov", "*.avi", "*.webm", "*.iso",
                  "*.dmg", "*.pkg", "*.mpkg", "*.app",
                  "*.deb", "*.rpm", "*.appimage", "*.snap", "*.flatpak", "*.flatpakref", "*.run",
                  "*.exe", "*.msi"]
EXCLUDED_DIRS = ["*.app", "*.photoslibrary", "Photo Booth Library", "node_modules",
                 ".venv", "venv", "__pycache__", ".cache", ".Trash", ".git"]
ARCHIVE_EXTENSIONS = [".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".tbz", ".tbz2",
                      ".xz", ".txz", ".zst", ".lz", ".lzma", ".cab"]
BAD_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')  # refused by exFAT

LAUNCHD_LABEL = "com.usbvault.watch"
SYSTEMD_UNIT = "usbvault-watch.service"
CONDA_ENV = "usbvault"
EXPECTED_REPO = Path.home() / "Documents" / "code" / "back-to-the-future-unix-base"
CONDA_CANDIDATES = [
    "~/miniconda3/bin/conda", "~/miniforge3/bin/conda", "~/anaconda3/bin/conda", "~/mambaforge/bin/conda",
    "/opt/homebrew/Caskroom/miniconda/base/bin/conda", "/opt/homebrew/Caskroom/miniforge/base/bin/conda",
    "/usr/local/Caskroom/miniconda/base/bin/conda", "/opt/miniconda3/bin/conda",
    "/opt/anaconda/bin/conda", "/opt/conda/bin/conda",
]


class VaultError(Exception):
    pass


class Busy(VaultError):
    pass


class CopyError(Exception):
    pass


# --------------------------------------------------------------------------- ui

class UI:
    def __init__(self):
        self.tty = sys.stderr.isatty()
        self.verbose = False
        self.logf = None
        self.live = False
        self.last_draw = 0.0

    def open_log(self, path: Path):
        self.close_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.logf = open(path, "a", encoding="utf-8")

    def close_log(self):
        if self.logf:
            self.logf.close()
            self.logf = None

    def _write_log(self, msg):
        if self.logf:
            self.logf.write(f"{now_iso()} {msg}\n")
            self.logf.flush()

    def log(self, msg):
        self._write_log(msg)
        if self.verbose:
            self._print(msg)

    def say(self, msg=""):
        if msg:
            self._write_log(msg)
        self._print(msg)

    def warn(self, msg):
        self.say(f"! {msg}")

    def _print(self, msg):
        self.end_progress()
        print(msg, flush=True)

    def progress(self, i, n, done, total, label):
        if not self.tty:
            return
        now = time.monotonic()
        if now - self.last_draw < 0.1 and i < n:
            return
        self.last_draw = now
        width = shutil.get_terminal_size((80, 20)).columns
        head = f"[{i}/{n}] {human(done)} / {human(total)}  "
        room = width - len(head) - 1
        sys.stderr.write("\r\033[K" + (head + (label[-room:] if room > 0 else ""))[: width - 1])
        sys.stderr.flush()
        self.live = True

    def end_progress(self):
        if self.live:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
            self.live = False


ui = UI()


def die(msg):
    raise VaultError(msg)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def tilde(path) -> str:
    path, home = str(path), str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} Type 'yes' to continue: ").strip().lower() == "yes"


def notify(title: str, msg: str):
    """Desktop notification; does nothing where unsupported."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if IS_MAC:
            script = (f"display notification {json.dumps(msg, ensure_ascii=False)} "
                      f"with title {json.dumps(title, ensure_ascii=False)}")
            subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, msg], timeout=10, capture_output=True)


# ------------------------------------------------------------------------ keys

def parent_key(key: str) -> str:
    return key.rsplit("/", 1)[0] if "/" in key else ""


def name_of(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def under(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(prefix + "/")


def is_junk(name: str) -> bool:
    return name in JUNK_NAMES or name.startswith("._") or name.startswith(TMP_PREFIX)


def is_partial(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in PARTIAL_PATTERNS)


def safe_name(name: str) -> str:
    """A name exFAT accepts and that looks the same on macOS and Linux."""
    name = BAD_CHARS.sub("_", unicodedata.normalize("NFC", name))
    return name.rstrip(" .") or "_"


def numbered(name: str, n: int) -> str:
    stem, ext = os.path.splitext(name)
    return f"{stem} ({n}){ext}"


# ----------------------------------------------------------------------- config

def config_path() -> Path:
    env = os.environ.get("USBVAULT_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "usbvault" / "config.json"


def default_folders() -> dict:
    """~/Documents and ~/Downloads (on Linux, xdg-user-dir finds e.g. ~/Documentos)."""
    home = Path.home()
    out = {}
    for name, xdg in (("Documents", "DOCUMENTS"), ("Downloads", "DOWNLOAD")):
        path = home / name
        if IS_LINUX and shutil.which("xdg-user-dir"):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                found = subprocess.run(["xdg-user-dir", xdg], capture_output=True,
                                       text=True, timeout=5).stdout.strip()
                if found and found != str(home):
                    path = Path(found)
        out[name] = tilde(path)
    return out


def default_config() -> dict:
    return {
        "config_version": CONFIG_VERSION,
        "role": "auto",
        "usb_label": "VAULT",
        "folders": default_folders(),
        "exclude_paths": ["~/Documents/code"],
        "skip_git_repos": True,
        "exclude_dirs": EXCLUDED_DIRS,
        "exclude_files": EXCLUDED_FILES,
        "archive_extensions": ARCHIVE_EXTENSIONS,
        "archive_limit_bytes": 1_000_000_000,
        "freeze_after_days": 183,
        "settle_seconds": 30,
        "extra_paths": [],
    }


def extras_prefix() -> str:
    """This computer's folder on the USB for its extra paths."""
    return f"{EXTRAS}/{safe_name(HOST)}"


def path_key(p) -> str:
    p = os.path.abspath(os.path.expanduser(str(p)))
    return p.casefold() if IS_MAC else p  # APFS is case-insensitive


def path_inside(child, parent) -> bool:
    c, par = path_key(child), path_key(parent)
    return c == par or c.startswith(par.rstrip(os.sep) + os.sep)


@dataclass
class Config:
    path: Path
    role: str                 # "source" (only sends) or "replica" (two-way)
    usb_label: str
    folders: dict             # "Documents"/"Downloads" -> local Path
    exclude_paths: set
    skip_git_repos: bool
    exclude_dirs: list
    exclude_files: list
    archive_extensions: tuple
    archive_limit: int
    freeze_days: int
    settle: int
    extras: list = field(default_factory=list)          # extra paths in use (absolute Paths)
    extra_warnings: list = field(default_factory=list)  # listed paths that are ignored, and why

    @staticmethod
    def _key(p: str) -> str:
        return path_key(p)

    def local(self, key: str) -> Path:
        top, _, rest = key.partition("/")
        if top == EXTRAS:
            host, _, rest = rest.partition("/")
            if host != safe_name(HOST):
                raise KeyError(key)  # another computer's extras
            if rest == ROOT_MARK or rest.startswith(ROOT_MARK + "/"):
                return Path("/" + rest[len(ROOT_MARK) + 1:])
            return Path.home() / rest if rest else Path.home()
        if top not in self.folders:
            raise KeyError(key)
        base = self.folders[top]
        return base / rest if rest else base

    def extra_key(self, p: Path) -> str:
        try:
            rel = p.relative_to(Path.home()).as_posix()
        except ValueError:
            rel = f"{ROOT_MARK}/{p.as_posix().lstrip('/')}"
        return f"{extras_prefix()}/{rel}"

    @property
    def extra_keys(self) -> list:
        return [self.extra_key(p) for p in self.extras]

    def is_extra_key(self, key: str) -> bool:
        return any(under(key, k) for k in self.extra_keys)

    def extra_problem(self, p: Path):
        """Why p can't be an extra path, or None when it can."""
        if path_key(p) in (path_key(Path.home()), path_key("/")):
            return "that is your whole home folder (or disk); list specific folders or files"
        for top, root in self.folders.items():
            if path_inside(root, p):
                return f"it contains ~/{top}, which is already backed up; list specific folders inside it instead"
            if path_inside(p, root):
                if path_inside(p, self.local(TIME_KEY)) or path_inside(p, self.local(OLD_KEY)):
                    return "TimeIsRelative and OldWorld are never copied (your rules)"
                if any(path_inside(p, ep) for ep in self.exclude_paths):
                    return None  # e.g. inside ~/Documents/code: allowed, it is not synced otherwise
                d = p if p.is_dir() else p.parent
                while path_inside(d, root) and path_key(d) != path_key(root):
                    if (d / ".git").exists():
                        return None  # inside a git repository: allowed, it is not synced otherwise
                    d = d.parent
                return f"it is inside ~/{top}, which is already backed up"
        return None

    def excluded_path(self, path: str) -> bool:
        return self._key(path) in self.exclude_paths

    def excluded_dir(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in self.exclude_dirs)

    def excluded_file(self, name: str, size: int) -> bool:
        low = name.lower()
        if any(fnmatch.fnmatch(low, p.lower()) for p in self.exclude_files):
            return True
        return size > self.archive_limit and low.endswith(self.archive_extensions)


def write_json(path: Path, data: dict):
    tmp = path.with_name(TMP_PREFIX + path.name)
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_config(announce=True) -> Config:
    path = config_path()
    raw = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            die(f"cannot read config {path}: {e}")
        if raw.get("config_version", 1) < CONFIG_VERSION:
            old = unique_target(path.with_name(f"config.v{raw.get('config_version', 1)}.json"))
            os.rename(path, old)  # kept, not deleted
            if announce:
                ui.say(f"Settings updated to the new rules; the old ones are kept in {tilde(old)}")
            raw = None
    if raw is None:
        raw = default_config()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, raw)
        if announce:
            ui.say(f"Settings file: {tilde(path)}")
    missing = {k: v for k, v in default_config().items() if k not in raw}
    if missing:
        raw.update(missing)
        with contextlib.suppress(OSError):
            write_json(path, raw)
    role = raw.get("role", "auto")
    if role == "auto":
        role = "source" if IS_MAC else "replica"
    if role not in ("source", "replica"):
        die(f"'role' in {path} must be auto, source or replica")
    folders = {k: Path(os.path.expanduser(v)) for k, v in raw["folders"].items()
               if k in ("Documents", "Downloads")}
    if "Documents" not in folders:
        die(f"{path} must list a Documents folder")
    cfg = Config(
        path=path, role=role, usb_label=raw["usb_label"], folders=folders,
        exclude_paths={Config._key(p) for p in raw["exclude_paths"]},
        skip_git_repos=bool(raw["skip_git_repos"]),
        exclude_dirs=list(raw["exclude_dirs"]), exclude_files=list(raw["exclude_files"]),
        archive_extensions=tuple(e.lower() for e in raw["archive_extensions"]),
        archive_limit=int(raw["archive_limit_bytes"]), freeze_days=int(raw["freeze_after_days"]),
        settle=int(raw["settle_seconds"]),
    )
    for item in raw.get("extra_paths", []):
        p = Path(os.path.abspath(os.path.expanduser(item)))
        problem = cfg.extra_problem(p)
        if problem:
            cfg.extra_warnings.append(f"{item}: ignored, {problem}")
        else:
            cfg.extras.append(p)
    return cfg


# --------------------------------------------------------------------- file ops

def unchanged(path, st) -> bool:
    try:
        now = os.stat(path)
    except OSError:
        return False
    return now.st_size == st.st_size and now.st_mtime_ns == st.st_mtime_ns


def same_meta(a, b) -> bool:
    return a.st_size == b.st_size and abs(a.st_mtime - b.st_mtime) <= MTIME_TOLERANCE


def restore_atime(path, st):
    """Put the access time back after this program read a file, so our own
    reading never counts as you using it."""
    with contextlib.suppress(OSError):
        now = os.stat(path)
        if now.st_atime_ns != st.st_atime_ns and now.st_mtime_ns == st.st_mtime_ns:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


@contextlib.contextmanager
def quiet_open(path, st=None, uncached=False):
    """Open for reading without changing the file's access time."""
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        fd = os.open(path, os.O_RDONLY | noatime)
    except PermissionError:
        if not noatime:
            raise
        fd = os.open(path, os.O_RDONLY)  # O_NOATIME needs file ownership
    if uncached:  # read from the device, not from memory
        with contextlib.suppress(OSError, AttributeError):
            if IS_MAC and fcntl:
                fcntl.fcntl(fd, getattr(fcntl, "F_NOCACHE", 48), 1)
            elif hasattr(os, "posix_fadvise"):
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    try:
        with os.fdopen(fd, "rb") as f:
            yield f
    finally:
        if st is not None:
            restore_atime(path, st)


def hash_file(path, st=None, uncached=False) -> str:
    h = hashlib.sha256()
    with quiet_open(path, st, uncached) as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def discard_temp(path: Path):
    """The only file removal in this program: its own half-written temporary
    copies (named .vaulttmp-*). Your files are never deleted."""
    if not path.name.startswith(TMP_PREFIX):
        raise RuntimeError(f"refusing to remove {path}")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def unique_target(p: Path) -> Path:
    n = 2
    candidate = p
    while os.path.lexists(candidate):
        candidate = p.with_name(numbered(p.name, n))
        n += 1
    return candidate


def move_path(src: Path, dst: Path) -> Path:
    """Rename src to dst (or 'dst (2)'...). Never replaces anything."""
    dst = unique_target(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)
    return dst


def copy_verified(src, dst: Path, st, expect=None) -> str:
    """Copy through a temp file, read the copy back and compare SHA-256, then
    rename into place. Never overwrites: dst must not exist."""
    if os.path.lexists(dst):
        raise CopyError(f"{dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f"{TMP_PREFIX}{uuid.uuid4().hex[:12]}"
    h = hashlib.sha256()
    try:
        with quiet_open(src, st) as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        digest = h.hexdigest()
        if not unchanged(src, st) or (expect and digest != expect):
            raise CopyError("file changed while it was being copied; will retry next time")
        if hash_file(tmp, uncached=True) != digest:
            raise CopyError("the copy does not match the original (failing drive?)")
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        if os.path.lexists(dst):
            raise CopyError(f"{dst} appeared while copying")
        os.rename(tmp, dst)
    except BaseException:
        discard_temp(tmp)
        raise
    return digest


def is_dicom(path: str, st, name: str) -> bool:
    low = name.lower()
    if low.endswith((".dcm", ".dicom")) or low == "dicomdir":
        return True
    if st.st_size < 132:
        return False
    try:
        with quiet_open(path, st) as f:
            head = f.read(132)
    except OSError:
        return False
    return head[128:132] == b"DICM"  # DICOM files carry this signature at byte 128


def arrival(st) -> float:
    """When a file arrived: creation time where the OS records it, else mtime."""
    return getattr(st, "st_birthtime", None) or st.st_mtime


def last_used(st) -> float:
    return max(st.st_atime, st.st_mtime, getattr(st, "st_birthtime", 0) or 0)


def spotlight_last_opened(paths: list) -> dict:
    """macOS Finder 'Last opened' dates, one mdls call per 200 files."""
    out = {}
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        try:
            raw = subprocess.run(["mdls", "-raw", "-name", "kMDItemLastUsedDate", *chunk],
                                 capture_output=True, timeout=120).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        values = raw.split(b"\0")
        if len(values) != len(chunk):
            continue
        for p, v in zip(chunk, values):
            with contextlib.suppress(ValueError):
                out[p] = dt.datetime.strptime(v.decode(errors="replace").strip(),
                                              "%Y-%m-%d %H:%M:%S %z").timestamp()
    return out


def file_metadata(st, name: str, **extra) -> str:
    def iso(ts):
        return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    meta = {
        "size": st.st_size, "modified": iso(st.st_mtime), "accessed": iso(st.st_atime),
        "changed": iso(st.st_ctime), "extension": os.path.splitext(name)[1].lower(),
        "mime": mimetypes.guess_type(name)[0], "mode": oct(stat_mod.S_IMODE(st.st_mode)), "host": HOST,
    }
    if getattr(st, "st_birthtime", None):
        meta["created"] = iso(st.st_birthtime)
    meta.update({k: v for k, v in extra.items() if v is not None})
    return json.dumps(meta, ensure_ascii=False)


# -------------------------------------------------------------------- databases

def _no_delete(tables) -> str:
    return "".join(
        f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN "
        f"SELECT RAISE(ABORT, 'usbvault: rows are never deleted, only marked'); END;\n"
        for t in tables)


def upsert(db, table: str, keys: tuple, values: dict, keep=()):
    """INSERT, or UPDATE the given columns when the row exists. Never deletes."""
    cols = list(values)
    sets = [f"{c}=excluded.{c}" for c in cols if c not in keys and c not in keep]
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
           f"ON CONFLICT ({','.join(keys)}) DO " + (f"UPDATE SET {','.join(sets)}" if sets else "NOTHING"))
    db.execute(sql, [values[c] for c in cols])


def _like_prefix(key: str) -> str:
    return key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"


def _init_schema(db, schema: str, tables):
    db.executescript(schema + _no_delete(tables))
    row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not row or row[0] != SCHEMA_VERSION:
        upsert(db, "meta", ("key",), {"key": "schema_version", "value": SCHEMA_VERSION})
        db.commit()


class LocalDB:
    """~/Documents/TimeIsRelative/local.sqlite: what this computer knows, so the
    daemon keeps working while the USB is not plugged in.

    files       every file and folder seen here: state (present, moved, deleted =
                gone from this computer), metadata, and its state at the last sync
    hashes      SHA-256 of local files, reused while size and mtime don't change
    moves       moves made here (FreezeSolo, AttackOfTheClones), to repeat on the USB
    applied     moves made on other computers that were repeated here
    duplicates  duplicates found here
    events      everything that happened here
    """

    TABLES = ("meta", "files", "hashes", "moves", "applied", "duplicates", "events")
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY, is_dir INTEGER, usb_path TEXT, size INTEGER, mtime_ns INTEGER,
        sha256 TEXT, state TEXT, dicom INTEGER, excluded INTEGER,
        synced_size INTEGER, synced_mtime_ns INTEGER, usb_size INTEGER, usb_mtime REAL,
        last_opened REAL, last_opened_checked REAL, metadata TEXT,
        first_seen TEXT, first_seen_ts REAL, updated TEXT, deleted_at TEXT, moved_to TEXT);
    CREATE INDEX IF NOT EXISTS files_usb_path ON files (usb_path);
    CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sha256 TEXT);
    CREATE INDEX IF NOT EXISTS hashes_sha256 ON hashes (sha256);
    CREATE TABLE IF NOT EXISTS moves (
        id TEXT PRIMARY KEY, time TEXT, host TEXT, kind TEXT, src TEXT, dst TEXT,
        usb_src TEXT, usb_dst TEXT, sha256 TEXT, size INTEGER, pushed_at TEXT, usb_result TEXT);
    CREATE TABLE IF NOT EXISTS applied (id TEXT PRIMARY KEY, time TEXT, result TEXT);
    CREATE TABLE IF NOT EXISTS duplicates (
        id TEXT PRIMARY KEY, found TEXT, host TEXT, sha256 TEXT, size INTEGER,
        kept TEXT, clone TEXT, pushed_at TEXT);
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, time TEXT, host TEXT, kind TEXT, path TEXT, detail TEXT, pushed_at TEXT);
    """

    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        _init_schema(self.db, self.SCHEMA, self.TABLES)

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        upsert(self.db, "meta", ("key",), {"key": key, "value": value})

    def rows(self) -> dict:
        return {r["path"]: r for r in self.db.execute("SELECT * FROM files")}

    def put(self, key: str, **values):
        values["path"] = key
        values["updated"] = now_iso()
        if "first_seen" not in values:
            values["first_seen"] = now_iso()
            values["first_seen_ts"] = time.time()
        upsert(self.db, "files", ("path",), values, keep=("first_seen", "first_seen_ts"))

    def cached_hash(self, key, st):
        row = self.db.execute("SELECT size, mtime_ns, sha256 FROM hashes WHERE path=?", (key,)).fetchone()
        return row[2] if row and row[0] == st.st_size and row[1] == st.st_mtime_ns else None

    def put_hash(self, key, st, digest):
        upsert(self.db, "hashes", ("path",), {"path": key, "size": st.st_size,
                                             "mtime_ns": st.st_mtime_ns, "sha256": digest})

    def keys_with_hash(self, digest) -> list:
        return [r[0] for r in self.db.execute("SELECT path FROM hashes WHERE sha256=?", (digest,))]

    def mark_moved(self, old: str, new: str, st=None, digest=None, excluded=False):
        """Record that old became new on this computer (folder contents included).
        For a file (st given) the new path continues as a present file."""
        rows = self.db.execute("SELECT * FROM files WHERE path=? OR path LIKE ? ESCAPE '\\'",
                               (old, _like_prefix(old))).fetchall()
        first = None
        for r in rows:
            self.put(r["path"], state="moved", moved_to=new + r["path"][len(old):])
            if r["path"] == old:
                first = r
        if not rows:
            self.put(old, is_dir=int(st is None), state="moved", moved_to=new)
        if st is not None:
            values = dict(is_dir=0, size=st.st_size, mtime_ns=st.st_mtime_ns, sha256=digest, state="present",
                          excluded=int(excluded), deleted_at=None, moved_to=None)
            if first is not None:
                values.update(first_seen=first["first_seen"], first_seen_ts=first["first_seen_ts"],
                              metadata=first["metadata"], dicom=first["dicom"])
            self.put(new, **values)
            if digest:
                self.put_hash(new, st, digest)

    def add_move(self, kind, src, dst, usb_src, usb_dst, digest, size):
        self.db.execute("INSERT INTO moves VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                        (uuid.uuid4().hex, now_iso(), HOST, kind, src, dst, usb_src, usb_dst, digest, size))

    def pending(self, table):
        return self.db.execute(f"SELECT * FROM {table} WHERE pushed_at IS NULL ORDER BY rowid").fetchall()

    def mark_pushed(self, table, rid, **extra):
        sets = ", ".join(["pushed_at=?"] + [f"{k}=?" for k in extra])
        self.db.execute(f"UPDATE {table} SET {sets} WHERE id=?", (now_iso(), *extra.values(), rid))

    def applied_ids(self) -> set:
        return {r[0] for r in self.db.execute("SELECT id FROM applied")}

    def add_applied(self, mid, result):
        upsert(self.db, "applied", ("id",), {"id": mid, "time": now_iso(), "result": result})

    def add_duplicate(self, digest, size, kept, clone):
        self.db.execute("INSERT INTO duplicates VALUES (?,?,?,?,?,?,?,NULL)",
                        (uuid.uuid4().hex, now_iso(), HOST, digest, size, kept, clone))

    def duplicates(self):
        return self.db.execute("SELECT id, found, host, sha256, size, kept, clone FROM duplicates").fetchall()

    def add_event(self, kind, path, detail: dict):
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,NULL)",
                        (uuid.uuid4().hex, now_iso(), HOST, kind, path, json.dumps(detail, ensure_ascii=False)))

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()


class VaultDB:
    """VAULT/.vault/catalog.sqlite: the catalogue of everything on the USB.

    files       every file on the USB: path, size, mtime, SHA-256, which computer
                sent it, metadata (JSON). Removed files get deleted_at, never DELETE.
    sources     each computer's files and where they are on the USB
    moves       every FreezeSolo / AttackOfTheClones move, from any computer
    duplicates  every duplicate found (also in AttackOfTheClones/duplicates.csv)
    events      everything that happened, from every computer
    """

    TABLES = ("meta", "files", "sources", "moves", "duplicates", "events")
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha256 TEXT, origin TEXT,
        updated TEXT, deleted_at TEXT, metadata TEXT);
    CREATE INDEX IF NOT EXISTS files_sha256 ON files (sha256);
    CREATE TABLE IF NOT EXISTS sources (
        host TEXT NOT NULL, path TEXT NOT NULL, usb_path TEXT, size INTEGER, mtime_ns INTEGER,
        sha256 TEXT, status TEXT, first_seen TEXT, updated TEXT, PRIMARY KEY (host, path));
    CREATE TABLE IF NOT EXISTS moves (
        id TEXT PRIMARY KEY, time TEXT, host TEXT, kind TEXT, src TEXT, dst TEXT,
        sha256 TEXT, size INTEGER, result TEXT, applied_at TEXT);
    CREATE TABLE IF NOT EXISTS duplicates (
        id TEXT PRIMARY KEY, found TEXT, host TEXT, sha256 TEXT, size INTEGER, kept TEXT, clone TEXT);
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, time TEXT, host TEXT, kind TEXT, path TEXT, detail TEXT);
    """

    def __init__(self, location: Path, readonly=False):
        path = location / VAULT_DIR / CATALOG if location.is_dir() else location
        if readonly:
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            self.db = sqlite3.connect(str(path))
            _init_schema(self.db, self.SCHEMA, self.TABLES)
        self.db.row_factory = sqlite3.Row

    def fresh_hash(self, path, st):
        row = self.db.execute("SELECT size, mtime, sha256 FROM files WHERE path=? AND deleted_at IS NULL",
                              (path,)).fetchone()
        if row and row[0] == st.st_size and abs(row[1] - st.st_mtime) <= MTIME_TOLERANCE:
            return row[2]
        return None

    def by_hash(self, digest) -> list:
        return self.db.execute("SELECT path, size, mtime FROM files WHERE sha256=? AND deleted_at IS NULL",
                               (digest,)).fetchall()

    def live_paths(self) -> set:
        return {r[0] for r in self.db.execute("SELECT path FROM files WHERE deleted_at IS NULL")}

    def put_file(self, path, st, digest, metadata, origin=HOST):
        upsert(self.db, "files", ("path",), {"path": path, "size": st.st_size, "mtime": st.st_mtime,
                                            "sha256": digest, "origin": origin, "updated": now_iso(),
                                            "deleted_at": None, "metadata": metadata})

    def mark_deleted(self, path, reason):
        self.db.execute("UPDATE files SET deleted_at=?, updated=? WHERE path=? AND deleted_at IS NULL",
                        (f"{now_iso()} {reason}", now_iso(), path))

    def move_file(self, old, new):
        """Catalogue a rename on the USB (folder contents included)."""
        rows = self.db.execute("SELECT * FROM files WHERE (path=? OR path LIKE ? ESCAPE '\\') "
                               "AND deleted_at IS NULL", (old, _like_prefix(old))).fetchall()
        for r in rows:
            values = dict(r)
            values["path"] = new + r["path"][len(old):]
            values["updated"] = now_iso()
            upsert(self.db, "files", ("path",), values)
            self.mark_deleted(r["path"], f"moved to {values['path']}")

    def put_source(self, key, usb_path, st, digest, status):
        upsert(self.db, "sources", ("host", "path"),
               {"host": HOST, "path": key, "usb_path": usb_path, "size": st.st_size,
                "mtime_ns": st.st_mtime_ns, "sha256": digest, "status": status,
                "first_seen": now_iso(), "updated": now_iso()}, keep=("first_seen",))

    def add_row(self, table, values: dict):
        cols = list(values)
        self.db.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                        "ON CONFLICT (id) DO NOTHING", [values[c] for c in cols])

    def moves_from_others(self):
        return self.db.execute("SELECT * FROM moves WHERE host != ? AND result LIKE 'moved to %' "
                               "ORDER BY rowid", (HOST,)).fetchall()

    def duplicates(self):
        return self.db.execute("SELECT id, found, host, sha256, size, kept, clone FROM duplicates "
                               "ORDER BY found").fetchall()

    def backup_to(self, target: Path):
        """Copy the catalogue (consistent snapshot); the previous backup is superseded."""
        tmp = target.with_name(TMP_PREFIX + target.name)
        discard_temp(tmp)
        dst = sqlite3.connect(str(tmp))
        try:
            self.db.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, target)

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()


# ------------------------------------------------------------------------ vault

def marker_path(root: Path) -> Path:
    return root / VAULT_DIR / MARKER


def is_vault(path: Path) -> bool:
    with contextlib.suppress(OSError):
        return marker_path(path).is_file()
    return False


def read_marker(root: Path) -> dict:
    return json.loads(marker_path(root).read_text(encoding="utf-8"))


def record_run(root: Path, entry: dict):
    data = read_marker(root)
    data["runs"] = (data.get("runs", []) + [{"time": now_iso(), "host": HOST, **entry}])[-50:]
    write_json(marker_path(root), data)


def mount_roots() -> list:
    env = os.environ.get("USBVAULT_MOUNT_ROOTS")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p]
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return [Path("/Volumes"), Path("/run/media") / user, Path("/media") / user, Path("/media"), Path("/mnt")]


def find_vault(cfg: Config, explicit=None):
    """(path, None) when the VAULT drive is plugged in, else (None, why)."""
    if explicit:
        p = Path(explicit).expanduser()
        return (p.resolve(), None) if is_vault(p) else (None, f"no vault at {p}")
    seen = []
    for base in mount_roots():
        try:
            kids = list(base.iterdir())
        except OSError:
            continue
        for p in kids:
            if p.name.casefold() == cfg.usb_label.casefold():
                if is_vault(p):
                    return p.resolve(), None
                seen.append(p)
    if seen:
        return None, f"found {seen[0]} but it is not set up yet: run '{SCRIPT_NAME} init {seen[0]}'"
    return None, f"the USB drive named {cfg.usb_label} is not plugged in"


def require_vault(cfg, explicit) -> Path:
    usb, why = find_vault(cfg, explicit)
    if usb is None:
        die(why)
    return usb


@contextlib.contextmanager
def file_lock(path: Path, what: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")  # append mode: taking the lock writes nothing
    try:
        if fcntl:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy(f"{what} is busy with another usbvault run") from None
            except OSError:
                pass  # filesystem without lock support
        yield
    finally:
        f.close()


def fs_type(path: Path) -> str:
    try:
        if IS_MAC:
            out = subprocess.run(["diskutil", "info", "-plist", str(path)],
                                 capture_output=True, timeout=15).stdout
            info = plistlib.loads(out)
            return str(info.get("FilesystemType") or info.get("FilesystemName") or "unknown").lower()
        if shutil.which("findmnt"):
            out = subprocess.run(["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)],
                                 capture_output=True, text=True, timeout=15).stdout
            return out.strip().lower() or "unknown"
    except Exception:
        pass
    return "unknown"


FS_ADVICE = {
    "msdos": "FAT32 cannot hold files over 4 GB.", "vfat": "FAT32 cannot hold files over 4 GB.",
    "apfs": "Linux cannot use APFS drives.", "hfs": "Linux cannot reliably write HFS+ drives.",
    "ext4": "macOS cannot read ext4 drives.", "btrfs": "macOS cannot read btrfs drives.",
    "ntfs": "macOS can only read NTFS.", "ntfs3": "macOS can only read NTFS.",
    "fuseblk": "This looks like NTFS; macOS can only read it.",
}


# ----------------------------------------------------------------------- scans

@dataclass
class Entry:
    key: str
    path: str
    st: object
    is_dir: bool
    usb_key: str
    excluded: bool = False  # stays on this computer only (video, installer, big archive, DICOM)
    extra: bool = False     # one of your extra paths: copied, never moved
    explicit: bool = False  # a file you listed by name: always copied


@dataclass
class Report:
    errors: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    renamed: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)  # extra paths that don't exist right now
    scan_failed: bool = False

    def error(self, where, err):
        msg = f"{where}: {getattr(err, 'strerror', None) or err}"
        self.errors.append(msg)
        ui.log(f"ERROR {msg}")


def _walk(cfg: Config, report: Report, out: dict, root: str, key: str, ukey: str, extra: bool):
    """Add everything under root to out. In Documents/Downloads, ~/Documents/code
    and git repositories are left alone; inside an extra folder you listed, only
    the excluded folder names (node_modules, .git, ...) are skipped."""
    stack = [(root, key, ukey)]
    while stack:
        path, key, ukey = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as e:
            report.error(path, e)
            report.scan_failed = True
            continue
        used = set()
        for e in entries:
            name = e.name
            if is_junk(name) or is_partial(name):
                continue
            child = f"{key}/{name}"
            if child in NO_SYNC_KEYS:
                continue
            try:
                if e.is_symlink():
                    report.skipped.append(f"{child} (symlink)")
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
                if is_dir:
                    if cfg.excluded_dir(name) or (not extra and cfg.excluded_path(e.path)):
                        report.skipped.append(f"{child} (excluded folder)")
                        continue
                    if not extra and cfg.skip_git_repos and os.path.lexists(os.path.join(e.path, ".git")):
                        report.skipped.append(f"{child} (git repository)")
                        continue
                    if is_vault(Path(e.path)):
                        continue  # never back up the USB into itself
                elif not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError as err:
                report.error(e.path, err)
                continue
            excluded = not is_dir and cfg.excluded_file(name, st.st_size)
            uname = safe_name(name)
            if not excluded:
                base, n = uname, 2
                while uname.casefold() in used:  # exFAT is case-insensitive
                    uname = numbered(base, n)
                    n += 1
                used.add(uname.casefold())
                if uname != unicodedata.normalize("NFC", name):
                    report.renamed.append(f"{child} -> {uname}")
            entry = Entry(child, e.path, st, is_dir, f"{ukey}/{uname}", excluded, extra)
            out[child] = entry
            if is_dir:
                stack.append((e.path, child, entry.usb_key))


def scan_local(cfg: Config, report: Report) -> dict:
    """Every file and folder this program manages on this computer: ~/Documents,
    ~/Downloads and your extra paths, keyed like 'Documents/Facturas/enero.xml'
    or 'Extras/<computer>/Pictures/Scans/a.jpg'."""
    out = {}
    for top in cfg.folders:
        root = cfg.local(top)
        if root.is_dir():
            _walk(cfg, report, out, str(root), top, top, extra=False)
    for p in cfg.extras:
        key = cfg.extra_key(p)
        ukey = "/".join(safe_name(part) for part in key.split("/"))
        try:
            st = os.stat(p, follow_symlinks=False)
        except OSError:
            report.unavailable.append(key)
            continue
        if stat_mod.S_ISDIR(st.st_mode):
            out[key] = Entry(key, str(p), st, True, ukey, extra=True)
            _walk(cfg, report, out, str(p), key, ukey, extra=True)
        elif stat_mod.S_ISREG(st.st_mode):
            out[key] = Entry(key, str(p), st, False, ukey, extra=True, explicit=True)
        else:
            report.skipped.append(f"{tilde(p)} (not a regular file or folder)")
    return out


def scan_usb(usb: Path, cfg: Config, extras="own") -> dict:
    """Files (stat) and folders (None) under VAULT/Documents, VAULT/Downloads and
    VAULT/Extras/<this computer> (extras="all": every computer's extras)."""
    out = {}
    roots = list(cfg.folders)
    if extras == "own":
        roots.append(extras_prefix())
    elif extras == "all":
        roots.append(EXTRAS)
    for top in roots:
        root = usb / top
        if not root.is_dir():
            continue
        stack = [(str(root), top)]
        while stack:
            path, key = stack.pop()
            try:
                with os.scandir(path) as it:
                    entries = list(it)
            except OSError:
                continue
            for e in entries:
                child = f"{key}/{e.name}"
                if is_junk(e.name) or is_partial(e.name) or child in NO_SYNC_KEYS:
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if e.name == ".git":
                            continue
                        out[child] = None
                        stack.append((e.path, child))
                    elif e.is_file(follow_symlinks=False):
                        out[child] = e.stat(follow_symlinks=False)
                except OSError:
                    continue
    return out


def usb_tracked(cfg: Config, path: str) -> bool:
    """Whether this computer's sync is responsible for a USB catalogue path."""
    return path.split("/", 1)[0] in cfg.folders or under(path, extras_prefix())


def mark_dicom_excluded(entries: dict, rows: dict):
    """DICOM files found inside extra folders are never copied (in Documents and
    Downloads they move to OldWorld instead)."""
    for k, e in entries.items():
        r = rows.get(k)
        if not e.is_dir and not e.explicit and r is not None and r["dicom"] == 1:
            e.excluded = True


# ------------------------------------------------------------------ the session

class Session:
    """One run on this computer: local database, log file, local lock."""

    def __init__(self, cfg: Config, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.time_dir = cfg.local(TIME_KEY)
        self.time_dir.mkdir(parents=True, exist_ok=True)
        self._lock = file_lock(self.time_dir / ".lock", "this computer")
        self._lock.__enter__()
        try:
            self.ldb = LocalDB(self.time_dir / "local.sqlite")
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        ui.open_log(self.time_dir / "logs" / f"{dt.date.today()}-{HOST}.log")
        self.stamp = dt.datetime.now().strftime(STAMP_FMT)

    def event(self, what, path, **detail):
        if self.dry_run:
            return
        self.ldb.add_event(what, path, detail)
        ui.log(f"{what:<11} {path}" + (f"  {json.dumps(detail, ensure_ascii=False)}" if detail else ""))

    @property
    def backup_path(self) -> Path:
        return self.time_dir / "vault-catalog-backup.sqlite"

    def close(self):
        try:
            self.ldb.close()
        finally:
            ui.close_log()
            self._lock.__exit__(None, None, None)


def refresh_backup(sess: Session, usb: Path):
    """Copy the USB catalogue to TimeIsRelative when it changed."""
    if not backup_is_stale(sess, usb):
        return
    vdb = VaultDB(usb)
    try:
        vdb.backup_to(sess.backup_path)
    finally:
        vdb.close()


def usb_catalog(sess: Session):
    """Read-only view of the USB catalogue as of the last sync (works offline)."""
    if sess.backup_path.exists():
        with contextlib.suppress(sqlite3.Error):
            return VaultDB(sess.backup_path, readonly=True)
    return None


def write_duplicates_csv(path: Path, rows):
    """duplicates.csv: one line per duplicate (opens in LibreOffice, Numbers, Excel).
    Generated from the database; each new version supersedes the previous one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(TMP_PREFIX + path.name)
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["found", "computer", "size", "kept (newest copy)", "older copy moved to"])
        for r in sorted(rows, key=lambda r: r["found"]):
            w.writerow([r["found"].replace("T", " "), r["host"], human(r["size"] or 0), r["kept"], r["clone"]])
    os.replace(tmp, path)


def local_duplicates_csv(sess: Session):
    rows = {r["id"]: r for r in sess.ldb.duplicates()}
    cat = usb_catalog(sess)
    if cat:
        try:
            rows.update({r["id"]: r for r in cat.duplicates()})
        except sqlite3.Error:
            pass
        finally:
            cat.close()
    if rows:
        write_duplicates_csv(sess.cfg.local(CSV_KEY), rows.values())


# --------------------------------------------------------------- local tidy-up

@dataclass
class Tidy:
    oldworld: list = field(default_factory=list)   # (key, destination, files, bytes)
    freeze: list = field(default_factory=list)     # (key, destination, bytes)
    clones: list = field(default_factory=list)     # (older copy, destination, kept, where, bytes)
    new: int = 0
    changed: int = 0
    missing: int = 0
    excluded: int = 0
    excluded_bytes: int = 0


def dicom_units(dicom_keys: set, entries: dict) -> list:
    """Which folders (or loose files) move to OldWorld. The whole folder directly
    under Documents/Downloads moves, so studies stay intact, unless that folder
    holds more other files than DICOM files; then only the folders that
    directly contain DICOM files move."""
    units, by_top = set(), defaultdict(set)
    for k in dicom_keys:
        parts = k.split("/")
        if len(parts) == 2:
            units.add(k)
        elif under(k, FREEZE_KEY) or under(k, CLONES_KEY):
            units.add(parent_key(k))
        else:
            by_top["/".join(parts[:2])].add(k)
    for top, ks in by_top.items():
        others = sum(1 for k, e in entries.items() if not e.is_dir and under(k, top) and k not in ks)
        if len(ks) >= others:
            units.add(top)
        else:
            units.update(parent_key(k) for k in ks)
    return sorted(u for u in units if not any(u != o and under(u, o) for o in units))


def local_phase(sess: Session, initial: bool, report: Report) -> Tidy:
    """Everything that happens on this computer, USB or not: notice new, changed
    and missing files, then the OldWorld, FreezeSolo and AttackOfTheClones moves."""
    cfg, ldb, apply = sess.cfg, sess.ldb, not sess.dry_run
    now = time.time()
    tidy = Tidy()
    entries = scan_local(cfg, report)
    rows = ldb.rows()

    def settled(e):
        return e.st.st_mtime <= now - cfg.settle

    def forget(prefix):
        for k in [k for k in entries if under(k, prefix)]:
            entries.pop(k)

    def actual_key(dst_key, new_path: Path):
        return f"{parent_key(dst_key)}/{new_path.name}"

    def usb_source(k):
        row = rows.get(k)
        return row["usb_path"] if row is not None and row["synced_size"] is not None else None

    # Missing files: marked deleted (logical); nothing is removed anywhere.
    def still_watched(key):
        if not under(key, EXTRAS):
            return True
        return cfg.is_extra_key(key) and not any(under(key, u) for u in report.unavailable)

    if not report.scan_failed:
        for key, row in rows.items():
            if row["state"] == "present" and key not in entries and still_watched(key):
                tidy.missing += 1
                if apply:
                    ldb.put(key, state="deleted", deleted_at=now_iso())
                    sess.event("missing", key, note="no longer on this computer; marked as deleted")

    fresh = []
    for key, e in entries.items():
        row = rows.get(key)
        if e.is_dir:
            if apply and (row is None or row["state"] != "present"):
                ldb.put(key, is_dir=1, usb_path=e.usb_key, state="present", deleted_at=None, moved_to=None)
            continue
        if e.excluded:
            tidy.excluded += 1
            tidy.excluded_bytes += e.st.st_size
        if initial or row is None or row["state"] != "present" or row["size"] != e.st.st_size \
                or row["mtime_ns"] != e.st.st_mtime_ns:
            if not settled(e):
                continue  # still being written; next time
            fresh.append(key)
            if row is None or row["state"] != "present":
                tidy.new += 1
            else:
                tidy.changed += 1

    # 1. DICOM -> OldWorld (never copied)
    dicom_keys = {k for k, r in rows.items() if r["dicom"] == 1 and k in entries and not entries[k].extra}
    flags = {}
    for k in fresh:
        e = entries[k]
        if not e.excluded and not e.explicit:
            flags[k] = is_dicom(e.path, e.st, name_of(k))
            if flags[k] and e.extra:
                e.excluded = True  # DICOM inside an extra folder: not copied, not moved
            elif flags[k]:
                dicom_keys.add(k)
    for unit in dicom_units(dicom_keys, entries):
        inside = [e for k, e in entries.items() if under(k, unit) and not e.is_dir]
        if not inside or not all(settled(e) for e in inside):
            continue
        dst = f"{OLD_KEY}/{unit}"
        size = sum(e.st.st_size for e in inside)
        if apply:
            try:
                new = move_path(cfg.local(unit), cfg.local(dst))
            except OSError as err:
                report.error(unit, err)
                continue
            dst = actual_key(dst, new)
            ldb.mark_moved(unit, dst)
            sess.event("oldworld", unit, moved_to=dst, files=len(inside), bytes=size)
        tidy.oldworld.append((unit, dst, len(inside), size))
        forget(unit)

    # 2. Not used for 6 months -> FreezeSolo (files only; folders stay)
    cutoff = now - cfg.freeze_days * 86400
    init_ts = float(ldb.meta("initialized_ts") or 0)
    candidates = []
    for k, e in entries.items():
        if e.is_dir or e.extra or under(k, FREEZE_KEY) or under(k, CLONES_KEY) or not settled(e):
            continue
        row = rows.get(k)
        if init_ts and (row is None or (row["first_seen_ts"] or 0) > max(init_ts, cutoff)):
            continue  # arrived after setup less than 6 months ago (e.g. unzipped old files)
        if last_used(e.st) < cutoff:
            candidates.append(k)
    if IS_MAC and candidates:  # Finder's "Last opened" also counts as use
        recheck = [k for k in candidates
                   if not (rows.get(k) is not None and rows[k]["last_opened_checked"]
                           and rows[k]["last_opened_checked"] >= now - 86400)]
        opened = spotlight_last_opened([entries[k].path for k in recheck])
        checked = {}
        for k in recheck:
            checked[k] = opened.get(entries[k].path)
            if apply and k in rows:
                ldb.put(k, last_opened=checked[k], last_opened_checked=now)
        def opened_recently(k):
            ts = checked[k] if k in checked else rows[k]["last_opened"]
            return bool(ts and ts >= cutoff)
        candidates = [k for k in candidates if not opened_recently(k)]
    for k in candidates:
        e = entries[k]
        dst = f"{FREEZE_KEY}/{k}"
        if apply:
            try:
                new = move_path(Path(e.path), cfg.local(dst))
            except OSError as err:
                report.error(k, err)
                continue
            dst = actual_key(dst, new)
            digest = ldb.cached_hash(k, e.st)
            usb_src = usb_source(k)
            ldb.add_move("freeze", k, dst, usb_src, f"{FREEZE_KEY}/{usb_src}" if usb_src else None,
                         digest, e.st.st_size)
            ldb.mark_moved(k, dst, e.st, digest, e.excluded)
            sess.event("freeze", k, moved_to=dst,
                       last_used=dt.datetime.fromtimestamp(last_used(e.st)).isoformat(timespec="seconds"))
        tidy.freeze.append((k, dst, e.st.st_size))
        forget(k)

    # 3. Identical content: the newest copy stays, older copies -> AttackOfTheClones
    hashes = {}
    to_hash = [k for k in fresh if k in entries and not entries[k].excluded and not entries[k].extra
               and not under(k, CLONES_KEY) and entries[k].st.st_size > 0]
    total = sum(entries[k].st.st_size for k in to_hash)
    done = 0
    for i, k in enumerate(to_hash, 1):
        e = entries[k]
        done += e.st.st_size
        ui.progress(i, len(to_hash), done, total, f"checking {k}")
        digest = ldb.cached_hash(k, e.st)
        if digest is None:
            try:
                digest = hash_file(e.path, e.st)
            except OSError as err:
                report.error(k, err)
                continue
            if not unchanged(e.path, e.st):
                continue
            ldb.put_hash(k, e.st, digest)  # a cache, kept even in a preview
        hashes[k] = digest
    ui.end_progress()

    cat = usb_catalog(sess)
    pending_usb = {m["usb_src"] for m in ldb.pending("moves") if m["usb_src"]}
    local_usb_keys = {e.usb_key for e in entries.values()}
    by_hash = defaultdict(list)
    for k, digest in hashes.items():
        by_hash[digest].append(k)
    for digest, new_keys in by_hash.items():
        group = []  # (rank, where, key, size); the highest rank is the copy that stays
        for k in new_keys:
            group.append(((0 if initial else 1, arrival(entries[k].st)), "local", k, entries[k].st.st_size))
        for k in ldb.keys_with_hash(digest):
            if k in new_keys or k not in entries or under(k, CLONES_KEY) or entries[k].excluded \
                    or entries[k].extra:
                continue
            if ldb.cached_hash(k, entries[k].st) == digest:
                group.append(((0, arrival(entries[k].st)), "local", k, entries[k].st.st_size))
        if cat:
            with contextlib.suppress(sqlite3.Error):
                for r in cat.by_hash(digest):
                    p = r["path"]
                    if p.split("/", 1)[0] not in cfg.folders or p in local_usb_keys or p in pending_usb \
                            or under(p, CLONES_KEY):
                        continue
                    group.append(((0, r["mtime"]), "usb", p, r["size"]))
        if len(group) < 2:
            continue
        group.sort(key=lambda g: g[0], reverse=True)
        _, keep_where, kept, size = group[0]
        for _, where, k, _ in group[1:]:
            dst = f"{CLONES_KEY}/{k}"
            if apply:
                if where == "local":
                    e = entries[k]
                    try:
                        new = move_path(Path(e.path), cfg.local(dst))
                    except OSError as err:
                        report.error(k, err)
                        continue
                    dst = actual_key(dst, new)
                    usb_src = usb_source(k)
                    ldb.add_move("clone", k, dst, usb_src, f"{CLONES_KEY}/{usb_src}" if usb_src else None,
                                 digest, e.st.st_size)
                    ldb.mark_moved(k, dst, e.st, digest)
                else:  # the older copy is only on the USB: it moves there at the next sync
                    ldb.add_move("clone", None, None, k, dst, digest, None)
                    pending_usb.add(k)
                ldb.add_duplicate(digest, size, kept, dst)
                sess.event("duplicate", k, kept=kept, older_copy_moved_to=dst,
                           where="this computer" if where == "local" else "USB (at the next sync)")
            tidy.clones.append((k, dst, kept, where, entries[k].st.st_size if where == "local" else 0))
            if where == "local":
                forget(k)
    if cat:
        cat.close()

    # 4. Remember what was seen.
    if apply:
        for k in fresh:
            e = entries.get(k)
            if e is None:
                continue
            ldb.put(k, is_dir=0, usb_path=e.usb_key, size=e.st.st_size, mtime_ns=e.st.st_mtime_ns,
                    sha256=hashes.get(k), state="present", dicom=int(bool(flags.get(k))),
                    excluded=int(e.excluded), deleted_at=None, moved_to=None,
                    metadata=file_metadata(e.st, name_of(k), sha256=hashes.get(k)))
            if rows.get(k) is None:
                sess.event("new", k, size=e.st.st_size, excluded=bool(e.excluded))
        if tidy.clones:
            local_duplicates_csv(sess)
        ldb.commit()
    return tidy


# -------------------------------------------------------------------- the sync

@dataclass
class Action:
    kind: str       # push, push_replace, push_conflict, pull, pull_replace, resolve, record, mkdir_usb, mkdir_local
    key: str        # path on this computer
    usb_key: str    # path on the USB
    size: int = 0


@dataclass
class SyncResult:
    pushed: int = 0
    pushed_bytes: int = 0
    pulled: int = 0
    pulled_bytes: int = 0
    versions: int = 0
    usb_moves: int = 0
    local_moves: int = 0
    errors: list = field(default_factory=list)

    @property
    def changed(self):
        return bool(self.pushed or self.pulled or self.versions or self.usb_moves or self.local_moves)


def plan_sync(cfg: Config, entries: dict, usb_tree: dict, rows: dict, now: float) -> list:
    """Three-way comparison of this computer, the USB, and both as they were at
    the last sync (rows). The name decides what is new; when a file changed,
    the newest version wins and the other one is kept as an old version."""
    actions = []
    replica = cfg.role == "replica"
    mine = {e.usb_key: e for e in entries.values() if not e.excluded}
    taken = {e.usb_key for e in entries.values()}
    for ukey, e in mine.items():
        R = rows.get(e.key)
        U = usb_tree.get(ukey, "missing")
        synced = R is not None and R["synced_size"] is not None
        if e.is_dir:
            if U == "missing" and not synced:
                actions.append(Action("mkdir_usb", e.key, ukey))
            elif U is None and not synced:
                actions.append(Action("record", e.key, ukey))
            continue
        L = e.st
        if L.st_mtime > now - cfg.settle:
            continue  # still being written
        l_changed = not synced or R["synced_size"] != L.st_size or R["synced_mtime_ns"] != L.st_mtime_ns
        if U == "missing":
            if synced and not l_changed:
                continue  # removed from the USB by hand: respected, not sent again
            actions.append(Action("push", e.key, ukey, L.st_size))
            continue
        if U is None:  # a folder on the USB has this file's name
            actions.append(Action("push_conflict", e.key, ukey, L.st_size))
            continue
        if same_meta(L, U):
            if l_changed or R["usb_size"] != U.st_size:
                actions.append(Action("record", e.key, ukey))
            continue
        u_changed = not synced or R["usb_size"] != U.st_size \
            or abs((R["usb_mtime"] or 0) - U.st_mtime) > MTIME_TOLERANCE
        if l_changed and not u_changed:
            actions.append(Action("push_replace", e.key, ukey, L.st_size))
        elif u_changed and not l_changed:
            if replica:
                actions.append(Action("pull_replace", e.key, ukey, U.st_size))
        else:
            actions.append(Action("resolve", e.key, ukey, max(L.st_size, U.st_size)))
    if replica:
        for ukey, U in sorted(usb_tree.items(), key=lambda kv: kv[0].count("/")):
            if ukey in taken:
                continue
            R = rows.get(ukey)
            if R is not None and (R["state"] == "moved" or (R["state"] == "deleted" and R["synced_size"] is not None)):
                continue  # moved or deleted on this computer on purpose: not brought back
            if under(ukey, EXTRAS) and not cfg.is_extra_key(ukey):
                continue  # only the extra paths this computer still lists come back to it
            if U is None:
                actions.append(Action("mkdir_local", ukey, ukey))
            elif not cfg.excluded_file(name_of(ukey), U.st_size):
                actions.append(Action("pull", ukey, ukey, U.st_size))
    return actions


def push_local_moves(sess: Session, usb: Path, vdb: VaultDB, res: SyncResult):
    """Repeat on the USB the moves made on this computer."""
    for m in sess.ldb.pending("moves"):
        if not m["usb_src"]:
            result = "was not on the USB"
        else:
            src = usb / m["usb_src"]
            try:
                st = src.stat()
            except FileNotFoundError:
                st = None
            if st is None:
                result = "not on the USB"
            elif not stat_mod.S_ISREG(st.st_mode) or (m["size"] is not None and st.st_size != m["size"]):
                result = "the USB copy is different; left where it is"
            else:
                try:
                    new = move_path(src, usb / m["usb_dst"])
                    new_key = new.relative_to(usb).as_posix()
                    vdb.move_file(m["usb_src"], new_key)
                    result = f"moved to {new_key}"
                    res.usb_moves += 1
                except OSError as err:
                    result = f"failed: {err.strerror}"
        vdb.add_row("moves", {"id": m["id"], "time": m["time"], "host": m["host"], "kind": m["kind"],
                              "src": m["usb_src"],
                              "dst": result[len("moved to "):] if result.startswith("moved to ") else m["usb_dst"],
                              "sha256": m["sha256"], "size": m["size"], "result": result,
                              "applied_at": now_iso()})
        sess.ldb.mark_pushed("moves", m["id"], usb_result=result)
        sess.event("usb-move", m["usb_src"] or m["src"] or "", kind=m["kind"], result=result)


def apply_remote_moves(sess: Session, vdb: VaultDB, res: SyncResult):
    """Repeat here the FreezeSolo / AttackOfTheClones moves other computers made."""
    done = sess.ldb.applied_ids()
    for m in vdb.moves_from_others():
        if m["id"] in done:
            continue
        result = "not on this computer"
        try:
            src = sess.cfg.local(m["src"])
            st = src.stat()
        except (OSError, KeyError):
            st = None
        if st is not None and stat_mod.S_ISREG(st.st_mode):
            if m["size"] is not None and st.st_size != m["size"]:
                result = "different here; left where it is"
            else:
                try:
                    new = move_path(src, sess.cfg.local(m["dst"]))
                    new_key = f"{parent_key(m['dst'])}/{new.name}"
                    sess.ldb.mark_moved(m["src"], new_key, new.stat(), m["sha256"])
                    result = f"moved to {new_key}"
                    res.local_moves += 1
                except OSError as err:
                    result = f"failed: {err.strerror}"
        sess.ldb.add_applied(m["id"], result)
        sess.event("remote-move", m["src"], kind=m["kind"], from_host=m["host"], result=result)


def sync_phase(sess: Session, usb: Path, report: Report) -> SyncResult:
    cfg, ldb = sess.cfg, sess.ldb
    res = SyncResult()
    with file_lock(usb / VAULT_DIR / "lock", "the USB"):
        vdb = VaultDB(usb)
        try:
            push_local_moves(sess, usb, vdb, res)
            for d in ldb.pending("duplicates"):
                vdb.add_row("duplicates", {k: d[k] for k in ("id", "found", "host", "sha256", "size", "kept", "clone")})
                ldb.mark_pushed("duplicates", d["id"])
            if cfg.role == "replica":
                apply_remote_moves(sess, vdb, res)
            ldb.commit()
            vdb.commit()

            entries = scan_local(cfg, Report())
            rows = ldb.rows()
            mark_dicom_excluded(entries, rows)
            usb_tree = scan_usb(usb, cfg)
            for p in vdb.live_paths() - set(usb_tree):
                if usb_tracked(cfg, p):
                    vdb.mark_deleted(p, "no longer on the USB")
            actions = plan_sync(cfg, entries, usb_tree, rows, time.time())
            push_bytes = sum(a.size for a in actions if a.kind.startswith(("push", "resolve")))
            pull_bytes = sum(a.size for a in actions if a.kind.startswith("pull"))
            if push_bytes > shutil.disk_usage(usb).free * 0.98:
                die(f"not enough space on the USB: need {human(push_bytes)}")
            if pull_bytes > shutil.disk_usage(cfg.local("Documents")).free * 0.95:
                die(f"not enough space on this computer for what is on the USB: need {human(pull_bytes)}")
            run_actions(sess, usb, vdb, actions, entries, usb_tree, res)

            for e in ldb.pending("events"):
                vdb.add_row("events", {k: e[k] for k in ("id", "time", "host", "kind", "path", "detail")})
                ldb.mark_pushed("events", e["id"])
            ldb.commit()
            vdb.commit()
            dups = vdb.duplicates()
            for target in (usb / CSV_KEY, cfg.local(CSV_KEY)):
                if dups and csv_rows(target) != len(dups):  # only when there is something new
                    write_duplicates_csv(target, dups)
            if res.changed:
                export_catalog(vdb, sess.time_dir / "catalog.jsonl")
                record_run(usb, {"pushed": res.pushed, "pulled": res.pulled,
                                 "bytes": res.pushed_bytes + res.pulled_bytes,
                                 "moves": res.usb_moves + res.local_moves, "errors": len(res.errors)})
            vdb.commit()
            if backup_is_stale(sess, usb):
                vdb.backup_to(sess.backup_path)
        finally:
            vdb.close()
    report.errors += res.errors
    return res


def csv_rows(path: Path) -> int:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return sum(1 for _ in f) - 1
    except OSError:
        return -1


def backup_is_stale(sess: Session, usb: Path) -> bool:
    try:
        return sess.backup_path.stat().st_mtime_ns < (usb / VAULT_DIR / CATALOG).stat().st_mtime_ns
    except OSError:
        return True


def run_actions(sess, usb, vdb, actions, entries, usb_tree, res: SyncResult):
    cfg, ldb = sess.cfg, sess.ldb
    total = sum(a.size for a in actions)
    done = 0
    for i, a in enumerate(actions, 1):
        done += a.size
        ui.progress(i, len(actions), done, total, a.key)
        local, remote = cfg.local(a.key), usb / a.usb_key
        e = entries.get(a.key)
        try:
            kind = a.kind
            if kind == "resolve":
                kind = resolve(sess, a, e, usb_tree[a.usb_key], local, remote, vdb)
            if kind in ("mkdir_usb", "mkdir_local"):
                (remote if kind == "mkdir_usb" else local).mkdir(parents=True, exist_ok=True)
                ldb.put(a.key, is_dir=1, usb_path=a.usb_key, state="present", synced_size=0, synced_mtime_ns=0)
            elif kind == "record":
                if e.is_dir:
                    ldb.put(a.key, is_dir=1, usb_path=a.usb_key, state="present", synced_size=0, synced_mtime_ns=0)
                else:
                    record_synced(sess, a.key, a.usb_key, e.st, remote.stat())
            elif kind in ("push", "push_replace", "push_conflict"):
                if kind == "push_replace":
                    keep_usb_version(sess, usb, vdb, a.usb_key, res)
                target = remote
                if kind == "push_conflict":
                    stem, ext = os.path.splitext(remote.name)
                    target = unique_target(remote.with_name(f"{stem} (conflict {HOST} {dt.date.today()}){ext}"))
                digest = copy_verified(e.path, target, e.st, expect=ldb.cached_hash(a.key, e.st))
                tkey = target.relative_to(usb).as_posix()
                tst = target.stat()
                vdb.put_file(tkey, tst, digest, file_metadata(e.st, name_of(e.key), sha256=digest))
                vdb.put_source(a.key, tkey, e.st, digest, "stored")
                ldb.put_hash(a.key, e.st, digest)
                record_synced(sess, a.key, a.usb_key, e.st, tst if target == remote else usb_tree.get(a.usb_key))
                res.pushed += 1
                res.pushed_bytes += e.st.st_size
                sess.event("sent", a.key, to=tkey, size=e.st.st_size)
            elif kind in ("pull", "pull_replace"):
                if kind == "pull_replace":
                    keep_local_version(sess, a.key, res)
                ust = usb_tree[a.usb_key]
                digest = copy_verified(remote, local, ust, expect=vdb.fresh_hash(a.usb_key, ust))
                lst = local.stat()
                ldb.put_hash(a.key, lst, digest)
                ldb.put(a.key, is_dir=0, usb_path=a.usb_key, size=lst.st_size, mtime_ns=lst.st_mtime_ns,
                        sha256=digest, state="present", excluded=0, deleted_at=None, moved_to=None,
                        metadata=file_metadata(lst, name_of(a.key), sha256=digest))
                record_synced(sess, a.key, a.usb_key, lst, ust)
                vdb.put_source(a.key, a.usb_key, lst, digest, "received")
                res.pulled += 1
                res.pulled_bytes += ust.st_size
                sess.event("received", a.key, size=ust.st_size)
        except (OSError, CopyError) as err:
            msg = f"{a.key}: {getattr(err, 'strerror', None) or err}"
            res.errors.append(msg)
            ui.log(f"ERROR {msg}")
        if i % 200 == 0:
            ldb.commit()
            vdb.commit()
    ui.end_progress()


def resolve(sess, a: Action, e: Entry, U, local: Path, remote: Path, vdb: VaultDB) -> str:
    """Both sides changed (or first sync): same content -> just record; else the
    newest wins. Returns the action to carry out."""
    if e.st.st_size == U.st_size:
        lh = sess.ldb.cached_hash(a.key, e.st) or hash_file(local, e.st)
        uh = vdb.fresh_hash(a.usb_key, U) or hash_file(remote, uncached=True)
        if lh == uh:
            sess.ldb.put_hash(a.key, e.st, lh)
            return "record"
    if e.st.st_mtime >= U.st_mtime:
        return "push_replace"
    return "pull_replace" if sess.cfg.role == "replica" else "push_conflict"


def record_synced(sess: Session, key, usb_key, lst, ust):
    sess.ldb.put(key, is_dir=0, usb_path=usb_key, size=lst.st_size, mtime_ns=lst.st_mtime_ns, state="present",
                 synced_size=lst.st_size, synced_mtime_ns=lst.st_mtime_ns,
                 usb_size=ust.st_size if ust else None, usb_mtime=ust.st_mtime if ust else None)


def keep_usb_version(sess, usb: Path, vdb: VaultDB, usb_key: str, res: SyncResult):
    """Move the USB's older version aside instead of overwriting it."""
    new = move_path(usb / usb_key, usb / VAULT_DIR / "versions" / sess.stamp / usb_key)
    vdb.move_file(usb_key, new.relative_to(usb).as_posix())
    res.versions += 1
    sess.event("version", usb_key, where="USB", kept_in=new.relative_to(usb).as_posix())


def keep_local_version(sess, key: str, res: SyncResult):
    """Move this computer's older version aside instead of overwriting it."""
    new = move_path(sess.cfg.local(key), sess.time_dir / "versions" / sess.stamp / key)
    res.versions += 1
    sess.event("version", key, where="this computer", kept_in=tilde(new))


def export_catalog(vdb: VaultDB, path: Path):
    """catalog.jsonl: the USB catalogue as one JSON object per line."""
    tmp = path.with_name(TMP_PREFIX + path.name)
    with open(tmp, "w", encoding="utf-8") as f:
        for r in vdb.db.execute("SELECT * FROM files WHERE deleted_at IS NULL ORDER BY path"):
            row = dict(r)
            with contextlib.suppress(TypeError, ValueError):
                row["metadata"] = json.loads(row["metadata"] or "{}")
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------- a cycle

def describe(tidy: Tidy, res) -> str:
    parts = []
    if tidy.oldworld:
        parts.append(f"{len(tidy.oldworld)} DICOM folder(s) -> OldWorld")
    if tidy.freeze:
        parts.append(f"{len(tidy.freeze)} unused file(s) -> FreezeSolo")
    if tidy.clones:
        parts.append(f"{len(tidy.clones)} older duplicate(s) -> AttackOfTheClones")
    if res:
        if res.pushed:
            parts.append(f"{res.pushed} file(s) to the USB ({human(res.pushed_bytes)})")
        if res.pulled:
            parts.append(f"{res.pulled} file(s) from the USB ({human(res.pulled_bytes)})")
        if res.versions:
            parts.append(f"{res.versions} older version(s) kept")
        if res.usb_moves or res.local_moves:
            parts.append(f"{res.usb_moves + res.local_moves} move(s) repeated")
        if res.errors:
            parts.append(f"{len(res.errors)} error(s)")
    return ", ".join(parts)


def run_cycle(cfg: Config, explicit_vault=None):
    """What the daemon does every minute. Returns (summary, state)."""
    sess = Session(cfg)
    try:
        if not sess.ldb.meta("initialized_at"):
            return "", f"waiting: run '{SCRIPT_NAME} first-run' on this computer first"
        usb, why = find_vault(cfg, explicit_vault)
        if usb:
            with contextlib.suppress(Busy):
                with file_lock(usb / VAULT_DIR / "lock", "the USB"):
                    refresh_backup(sess, usb)
        report = Report()
        tidy = local_phase(sess, initial=False, report=report)
        res = sync_phase(sess, usb, report) if usb else None
        summary = describe(tidy, res)
        if summary:
            ui.log(f"pass: {summary}")
            for err in report.errors[:10]:
                ui.say(f"    error {err}")
        state = f"USB {cfg.usb_label} connected at {usb}" if usb else f"offline ({why}); logging and tidying only"
        return summary, state
    finally:
        sess.close()


# -------------------------------------------------------------------- commands

def install_self(root: Path):
    """Keep a copy of this script on the USB so any machine can read the drive."""
    me = Path(__file__).resolve()
    target = root / SCRIPT_NAME
    with contextlib.suppress(OSError):
        if target.exists() and target.read_bytes() == me.read_bytes():
            return
        tmp = root / (TMP_PREFIX + SCRIPT_NAME)
        shutil.copyfile(me, tmp)
        os.replace(tmp, target)  # the older copy of the script is superseded


def cmd_init(args):
    root = Path(args.path).expanduser().resolve()
    cfg = load_config()
    if not root.is_dir():
        die(f"{root} is not a folder. Plug in the USB and pass its mount point "
            f"(macOS: /Volumes/{cfg.usb_label}, Ubuntu: /media/$USER/{cfg.usb_label}, "
            f"EndeavourOS: /run/media/$USER/{cfg.usb_label})")
    if root.name.casefold() != cfg.usb_label.casefold():
        ui.warn(f"the drive is called '{root.name}', but the daemon looks for one named "
                f"'{cfg.usb_label}'. Rename the drive (Disk Utility / Disks) before relying on the daemon.")
    fs = fs_type(root)
    if fs in FS_ADVICE:
        ui.warn(f"this drive is formatted as {fs}. {FS_ADVICE[fs]} Reformat it as exFAT for macOS + Linux.")
        if not args.yes and not confirm("Continue anyway?"):
            return 1
    if is_vault(root):
        ui.say(f"{root} is already set up.")
    else:
        (root / VAULT_DIR).mkdir(exist_ok=True)
        write_json(marker_path(root), {"id": uuid.uuid4().hex, "created": now_iso(), "created_on": HOST, "runs": []})
        ui.say(f"Set up {root} (filesystem: {fs}).")
    for name in cfg.folders:
        (root / name).mkdir(exist_ok=True)
    VaultDB(root).close()
    install_self(root)
    write_text_atomic(root / "README.txt", USB_README.format(script=SCRIPT_NAME))
    ui.say(f"Next, on this computer:  python {SCRIPT_NAME} first-run --dry-run")
    return 0


def write_text_atomic(path: Path, text: str):
    tmp = path.with_name(TMP_PREFIX + path.name)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def print_tidy(tidy: Tidy, preview: bool):
    verb = "would move" if preview else "moved"
    if tidy.oldworld:
        ui.say(f"DICOM -> ~/{OLD_KEY} (never copied), {verb}:")
        for k, _, n, size in tidy.oldworld:
            ui.say(f"    {k}  ({n:,} files, {human(size)})")
    if tidy.freeze:
        size = sum(s for _, _, s in tidy.freeze)
        ui.say(f"Not used for 6 months -> ~/{FREEZE_KEY}, {verb} {len(tidy.freeze):,} file(s), {human(size)}:")
        for k, _, _ in tidy.freeze[: (len(tidy.freeze) if ui.verbose else 10)]:
            ui.say(f"    {k}")
        if len(tidy.freeze) > 10 and not ui.verbose:
            ui.say(f"    ... and {len(tidy.freeze) - 10} more (-v lists all)")
    if tidy.clones:
        ui.say(f"Duplicates: the newest copy stays, {verb} {len(tidy.clones):,} older copy(ies) "
               f"to ~/{CLONES_KEY}:")
        for k, _, kept, where, _ in tidy.clones[: (len(tidy.clones) if ui.verbose else 10)]:
            ui.say(f"    {k}{' (on the USB)' if where == 'usb' else ''}   (kept: {kept})")
        if len(tidy.clones) > 10 and not ui.verbose:
            ui.say(f"    ... and {len(tidy.clones) - 10} more (-v lists all)")
    if tidy.excluded:
        ui.say(f"Never copied (videos, ISOs, installers, archives over 1 GB): {tidy.excluded:,} file(s), "
               f"{human(tidy.excluded_bytes)}; they stay where they are.")


def print_skips(report: Report):
    if report.skipped:
        ui.say(f"Left alone (code, git repositories, excluded folders, symlinks): {len(report.skipped)} item(s)")
        for s in report.skipped[: (len(report.skipped) if ui.verbose else 8)]:
            ui.say(f"    {s}")
        if len(report.skipped) > 8 and not ui.verbose:
            ui.say(f"    ... and {len(report.skipped) - 8} more (-v lists all)")
    if report.renamed:
        ui.say(f"{len(report.renamed)} name(s) use characters exFAT refuses; on the USB they are stored as:")
        for s in report.renamed[:5]:
            ui.say(f"    {s}")


def cmd_first_run(args):
    cfg = load_config()
    usb = require_vault(cfg, args.vault)
    sess = Session(cfg, dry_run=True)
    try:
        if sess.ldb.meta("initialized_at") and not args.again:
            ui.say(f"This computer was set up on {sess.ldb.meta('initialized_at')}. "
                   f"The daemon (or '{SCRIPT_NAME} sync') takes it from here.")
            return 0
        with file_lock(usb / VAULT_DIR / "lock", "the USB"):
            refresh_backup(sess, usb)
        ui.say(f"{'Copy this Mac to' if cfg.role == 'source' else 'Merge this computer with'} {usb}   "
               f"(computer: {HOST}, role: {cfg.role})")
        ui.say("Checking every file in Documents and Downloads (contents are read once to find duplicates)...")
        for p in cfg.extras:
            ui.say(f"Extra path: {describe_extra(cfg, p)}")
        for w in cfg.extra_warnings:
            ui.warn(w)
        report = Report()
        tidy = local_phase(sess, initial=True, report=report)
        print_skips(report)
        print_tidy(tidy, preview=True)
        local_clones = [(k, size) for k, _, _, w, size in tidy.clones if w == "local"]
        moving = [u for u, *_ in tidy.oldworld] + [k for k, *_ in tidy.freeze] + [k for k, _ in local_clones]
        entries = {k: e for k, e in scan_local(cfg, Report()).items() if not any(under(k, m) for m in moving)}
        actions = plan_sync(cfg, entries, scan_usb(usb, cfg), {}, time.time() + cfg.settle)
        send = [a for a in actions if a.kind.startswith(("push", "resolve"))]
        get = [a for a in actions if a.kind == "pull"]
        moved_bytes = sum(s for _, _, s in tidy.freeze) + sum(s for _, s in local_clones)
        ui.say(f"Then copy about {len(send) + len(tidy.freeze) + len(local_clones):,} file(s) "
               f"({human(sum(a.size for a in send) + moved_bytes)}) to the USB"
               + (f", and {len(get):,} file(s) ({human(sum(a.size for a in get))}) from the USB to this computer"
                  if cfg.role == "replica" else "") + ".")
        ui.say("Nothing is deleted: files are only moved into the folders above, and copied.")
        if args.dry_run:
            ui.say("\nPreview only: nothing was moved or copied.")
            return 0
        if not args.yes and not confirm("\nProceed?"):
            ui.say("Cancelled. Nothing was moved or copied.")
            return 1
    finally:
        sess.close()

    sess = Session(cfg)
    try:
        started = time.monotonic()
        report = Report()
        tidy = local_phase(sess, initial=True, report=report)
        sess.ldb.set_meta("initialized_at", now_iso())
        sess.ldb.set_meta("initialized_ts", str(time.time()))
        sess.ldb.commit()
        install_self(usb)
        res = sync_phase(sess, usb, report)
        elapsed = int(time.monotonic() - started)
        ui.say(f"\nDone in {elapsed // 60}m {elapsed % 60}s: {describe(tidy, res) or 'nothing to do'}.")
        for err in report.errors[:10]:
            ui.warn(err)
        if IS_MAC and any("Operation not permitted" in err for err in report.errors):
            ui.say("    macOS is blocking access: allow your terminal in System Settings > "
                   "Privacy & Security > Files and Folders (or Full Disk Access).")
        ui.say(f"Logs and a copy of the database: ~/{TIME_KEY}")
        ui.say("The daemon takes it from here (install it with ./setup.sh if you haven't).")
        return 1 if report.errors else 0
    finally:
        sess.close()


def cmd_sync(args):
    cfg = load_config(announce=False)
    summary, state = run_cycle(cfg, args.vault)
    ui.say(state)
    ui.say(summary or "Nothing new.")
    return 0


def cmd_watch(args):
    cfg = load_config(announce=False)
    with contextlib.suppress(OSError, AttributeError):
        os.nice(10)
    stopping = []

    def on_term(signum, frame):
        stopping.append(signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_term)
    ui.say(f"{now_iso()} usbvault watch started on {HOST} ({cfg.role}), every {args.interval}s")
    for w in cfg.extra_warnings:
        ui.warn(w)
    last_state = None
    try:
        while not stopping:
            try:
                cfg = load_config(announce=False)  # picks up 'extras add/remove' without a restart
                summary, state = run_cycle(cfg, args.vault)
                if summary:
                    ui.say(f"{now_iso()} {summary}")
                    notify("usbvault", summary)
            except Busy:
                state = last_state  # another usbvault run is working; try next time
            except VaultError as e:
                state = f"error: {e}"
            except Exception as e:  # keep the daemon alive, e.g. USB pulled out mid-copy
                state = f"error: {e!r}"
            if state != last_state:
                ui.say(f"{now_iso()} {state}")
                last_state = state
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    ui.say(f"{now_iso()} usbvault watch stopped")
    return 0


def cmd_status(args):
    cfg = load_config(announce=False)
    ui.say(f"This computer: {HOST}, role {cfg.role} "
           f"({'sends to the USB' if cfg.role == 'source' else 'two-way sync with the USB'})")
    local_db = cfg.local(TIME_KEY) / "local.sqlite"
    if not local_db.exists():
        ui.say(f"  not set up yet: run '{SCRIPT_NAME} first-run'")
    else:
        ldb = LocalDB(local_db)
        try:
            ui.say(f"  set up: {ldb.meta('initialized_at') or f'not yet (run {SCRIPT_NAME} first-run)'}")
            counts = dict(ldb.db.execute("SELECT state, COUNT(*) FROM files WHERE is_dir=0 "
                                         "GROUP BY state").fetchall())
            ui.say("  files: " + (", ".join(f"{n:,} {s}" for s, n in sorted(counts.items())) or "none yet"))
            for table, what in (("moves", "move(s)"), ("duplicates", "duplicate record(s)"), ("events", "event(s)")):
                n = len(ldb.pending(table))
                if n:
                    ui.say(f"  waiting for the USB: {n:,} {what}")
        finally:
            ldb.close()
    for key in (FREEZE_KEY, CLONES_KEY, OLD_KEY):
        p = cfg.local(key)
        if p.exists():
            n = sum(1 for f in p.rglob("*") if f.is_file())
            ui.say(f"  ~/{key}: {n:,} file(s)")
    if cfg.extras:
        ui.say("  extra paths backed up:")
        for p in cfg.extras:
            ui.say(f"    {describe_extra(cfg, p)}")
    for w in cfg.extra_warnings:
        ui.warn(w)
    usb, why = find_vault(cfg, args.vault)
    if not usb:
        ui.say(f"USB: {why}")
        return 0
    usage = shutil.disk_usage(usb)
    ui.say(f"USB {usb} ({fs_type(usb)}): {human(usage.used)} used of {human(usage.total)}, "
           f"{human(usage.free)} free")
    vdb = VaultDB(usb, readonly=True)
    try:
        n, size = vdb.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files "
                                 "WHERE deleted_at IS NULL").fetchone()
        ui.say(f"  catalogue: {n:,} files, {human(size)}; duplicates found: {len(vdb.duplicates()):,}")
        for host, n in vdb.db.execute("SELECT host, COUNT(*) FROM sources GROUP BY host"):
            ui.say(f"  {host}: {n:,} file(s) tracked")
    finally:
        vdb.close()
    for r in read_marker(usb).get("runs", [])[-5:]:
        ui.say(f"  {r['time']}  {r['host']:<16} sent {r.get('pushed', 0):,}, received {r.get('pulled', 0):,}, "
               f"moves {r.get('moves', 0):,}, errors {r.get('errors', 0)}")
    return 0


def cmd_verify(args):
    cfg = load_config(announce=False)
    usb = require_vault(cfg, args.vault)
    with file_lock(usb / VAULT_DIR / "lock", "the USB"):
        tree = {k: st for k, st in scan_usb(usb, cfg, extras="all").items() if st is not None}
        vdb = VaultDB(usb)
        corrupt, added, done = [], 0, 0
        total = sum(st.st_size for st in tree.values())
        ui.say(f"Reading every file on {usb} ({len(tree):,} files, {human(total)})...")
        try:
            for i, (k, st) in enumerate(sorted(tree.items()), 1):
                done += st.st_size
                ui.progress(i, len(tree), done, total, k)
                try:
                    digest = hash_file(usb / k, uncached=True)
                except OSError as e:
                    corrupt.append(f"{k} (unreadable: {e.strerror})")
                    continue
                known = vdb.fresh_hash(k, st)
                if known is not None and known != digest:
                    corrupt.append(k)
                elif known is None:
                    vdb.put_file(k, st, digest, file_metadata(st, name_of(k)), origin="verify")
                    added += 1
            for p in vdb.live_paths() - set(tree):
                if p.split("/", 1)[0] in cfg.folders or under(p, EXTRAS):
                    vdb.mark_deleted(p, "no longer on the USB")
        except KeyboardInterrupt:
            ui.warn("interrupted")
        finally:
            ui.end_progress()
            vdb.close()
    ui.say(f"Checked {len(tree):,} files; {added:,} were new or edited directly on the USB.")
    if corrupt:
        ui.warn(f"{len(corrupt)} file(s) no longer match their checksum (the drive may be failing). "
                "Copy them back from Ubuntu or EndeavourOS:")
        for k in corrupt:
            ui.say(f"    {k}")
        return 1
    ui.say("All good: every file matches its checksum.")
    return 0


def describe_extra(cfg: Config, p: Path) -> str:
    kind = "folder" if p.is_dir() else "file" if p.exists() else "not there right now"
    return f"{tilde(p)}  ({kind})  ->  USB: {cfg.extra_key(p)}"


def cmd_extras(args):
    """List, add or remove extra folders/files to back up besides Documents and Downloads."""
    cfg = load_config(announce=False)
    raw = json.loads(cfg.path.read_text(encoding="utf-8"))
    listed = raw.setdefault("extra_paths", [])
    if args.action == "list":
        if not listed:
            ui.say("No extra paths. Add some with:  python usbvault.py extras add ~/Pictures/Scans ~/.zshrc")
        for p in cfg.extras:
            ui.say(f"  {describe_extra(cfg, p)}")
        for w in cfg.extra_warnings:
            ui.warn(w)
        return 0
    if not args.paths:
        die(f"give one or more paths, e.g.  python usbvault.py extras {args.action} ~/Pictures/Scans")
    changed = False
    for item in args.paths:
        p = Path(os.path.abspath(os.path.expanduser(item)))
        stored = tilde(p)
        present = [x for x in listed if path_key(x) == path_key(p)]
        if args.action == "add":
            problem = cfg.extra_problem(p)
            if problem:
                ui.warn(f"{stored} not added: {problem}")
                continue
            if present:
                ui.say(f"{stored} is already listed.")
                continue
            listed.append(stored)
            changed = True
            if not p.exists():
                ui.warn(f"{stored} doesn't exist yet; it is backed up as soon as it does.")
            ui.say(f"Added {stored}  ->  USB: {cfg.extra_key(p)}")
        else:
            if not present:
                ui.warn(f"{stored} is not in the list.")
                continue
            for x in present:
                listed.remove(x)  # removes the setting only; files and USB copies stay
            changed = True
            ui.say(f"Removed {stored} from the list. Nothing was deleted: its copy stays on the USB "
                   f"at {cfg.extra_key(p)}, and the files stay where they are.")
    if changed:
        write_json(cfg.path, raw)
        ui.say("The daemon picks this up on its next pass (within a minute).")
    return 0


def cmd_config(args):
    cfg = load_config()
    ui.say(f"Settings: {cfg.path}   (this computer's role: {cfg.role})\n")
    ui.say(cfg.path.read_text(encoding="utf-8"))
    return 0


USB_README = """This drive is a usbvault: ~/Documents and ~/Downloads of your computers.

  Documents/FreezeSolo         files not used for 6 months
  Documents/AttackOfTheClones  older copies of duplicate files (+ duplicates.csv)
  .vault/catalog.sqlite        the catalogue (SQLite; rows are never deleted)
  .vault/versions              older versions kept when a file was edited

The program normally runs from ~/Documents/code/back-to-the-future-unix-base on
each computer. This copy lets you inspect the drive anywhere:

  python3 {script} status
  python3 {script} verify

Lost this USB? Format a new one as exFAT named VAULT, then on Ubuntu or
EndeavourOS run:  python usbvault.py init /media/$USER/VAULT   then   python usbvault.py sync

Always eject the drive before unplugging it.
"""


# -------------------------------------------------------------- install-daemon

def linux_distro() -> str:
    info = {}
    with contextlib.suppress(OSError):
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v.strip().strip('"')
    ids = f"{info.get('ID', '')} {info.get('ID_LIKE', '')}".split()
    for known in ("endeavouros", "ubuntu", "arch", "debian"):
        if known in ids:
            return known
    return ids[0] if ids else "linux"


def find_conda():
    """The real conda executable ('conda' in a terminal is often a shell function)."""
    for c in [os.environ.get("CONDA_EXE"), shutil.which("conda"), *CONDA_CANDIDATES]:
        if c and os.path.isfile(os.path.expanduser(c)) and os.access(os.path.expanduser(c), os.X_OK):
            return os.path.expanduser(c)
    return None


def daemon_python(args) -> str:
    """Python of the 'usbvault' conda environment, which the daemon runs with."""
    if getattr(args, "python", None):
        p = os.path.abspath(os.path.expanduser(args.python))
        if not os.access(p, os.X_OK):
            die(f"--python {p} is not an executable")
        return p
    if Path(sys.prefix).name == CONDA_ENV:
        return sys.executable
    conda = find_conda()
    if conda:
        with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
            out = subprocess.run([conda, "env", "list", "--json"], capture_output=True,
                                 text=True, timeout=60).stdout
            for prefix in json.loads(out).get("envs", []):
                python = Path(prefix) / "bin" / "python"
                if Path(prefix).name == CONDA_ENV and python.exists():
                    return str(python)
    die(f"the '{CONDA_ENV}' conda environment was not found. Run ./setup.sh from "
        f"{tilde(EXPECTED_REPO)}, or pass --python /path/to/python")


def daemon_script() -> Path:
    script = Path(__file__).resolve()
    if is_vault(script.parent):
        die("this is the copy on the USB; a daemon cannot run from a drive that gets unplugged. "
            f"Run install-daemon from the repo copy in {tilde(EXPECTED_REPO)}")
    if script.parent != Path(os.path.realpath(EXPECTED_REPO)):
        ui.warn(f"running from {tilde(script.parent)}, not {tilde(EXPECTED_REPO)}. The daemon will use "
                "this location; if you move the folder, run install-daemon again.")
    return script


def daemon_command(args) -> list:
    cmd = [daemon_python(args), str(daemon_script()), "watch", "--interval", str(args.interval)]
    if args.vault:
        cmd += ["--vault", str(Path(args.vault).expanduser())]
    return cmd


def retire_file(path: Path):
    """Stop using a service file by renaming it (kept, not deleted)."""
    if path.exists():
        new = unique_target(path.with_name(path.name + ".disabled"))
        os.rename(path, new)
        ui.say(f"Daemon stopped; its file is kept as {tilde(new)}")


def install_launchd(args) -> int:
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    target = f"gui/{os.getuid()}"
    if args.remove:
        subprocess.run(["launchctl", "bootout", f"{target}/{LAUNCHD_LABEL}"], capture_output=True)
        retire_file(plist)
        return 0
    log = Path.home() / "Library" / "Logs" / "usbvault-watch.log"
    cmd = daemon_command(args)
    data = {
        "Label": LAUNCHD_LABEL, "ProgramArguments": cmd, "WorkingDirectory": str(Path(cmd[1]).parent),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "ProcessType": "Background",
        "LowPriorityIO": True, "Nice": 10, "StandardOutPath": str(log), "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    if args.dry_run:
        ui.say(f"Would write {plist}:\n")
        ui.say(plistlib.dumps(data).decode())
        return 0
    plist.parent.mkdir(parents=True, exist_ok=True)
    tmp = plist.with_name(TMP_PREFIX + plist.name)
    tmp.write_bytes(plistlib.dumps(data))
    os.replace(tmp, plist)
    subprocess.run(["launchctl", "bootout", f"{target}/{LAUNCHD_LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", target, str(plist)], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"launchctl could not start the daemon: {r.stderr.strip()}")
    ui.say(f"Installed the macOS daemon ({plist}); it starts at login and runs every {args.interval}s.")
    ui.say(f"Its output: {log}   Event log: ~/{TIME_KEY}/logs")
    ui.say("\nOne manual step: open System Settings > Privacy & Security > Full Disk Access,")
    ui.say("press +, press Cmd+Shift+G and add this program:")
    ui.say(f"    {os.path.realpath(cmd[0])}")
    ui.say(f"Then restart the daemon:  launchctl kickstart -k {target}/{LAUNCHD_LABEL}")
    return 0


def install_systemd(args) -> int:
    distro = linux_distro()
    units = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"
    unit = units / SYSTEMD_UNIT
    if args.remove:
        subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], check=False)
        retire_file(unit)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return 0
    cmd = daemon_command(args)
    text = (
        "[Unit]\n"
        f"Description=usbvault: keep Documents and Downloads in sync with the USB (conda env {CONDA_ENV})\n\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={Path(cmd[1]).parent}\n"
        f"ExecStart={' '.join(json.dumps(c) for c in cmd)}\n"
        "Restart=always\nRestartSec=30\nNice=10\nIOSchedulingClass=idle\n"
        "Environment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    if args.dry_run:
        ui.say(f"Would write {unit} ({distro}):\n")
        ui.say(text)
        return 0
    units.mkdir(parents=True, exist_ok=True)
    write_text_atomic(unit, text)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    r = subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], capture_output=True, text=True)
    if r.returncode != 0:
        die(f"systemctl could not start the daemon: {r.stderr.strip()}")
    ui.say(f"Installed the {distro} daemon ({unit}); it runs while you are logged in, every {args.interval}s.")
    ui.say(f"Output: journalctl --user -u {SYSTEMD_UNIT} -f    Event log: ~/{TIME_KEY}/logs")
    user = os.environ.get("USER", "$USER")
    if distro in ("endeavouros", "arch"):
        ui.say(f"\nEndeavourOS mounts USB drives at /run/media/{user}/VAULT only if your desktop auto-mounts")
        ui.say("them. KDE: System Settings > Disks & Cameras > Device Auto-Mount. XFCE: Removable Drives")
        ui.say("and Media > 'Mount removable drives when hot-plugged'.")
        if not shutil.which("fsck.exfat"):
            ui.say("Also install the exFAT tools:  sudo pacman -S exfatprogs")
    elif distro in ("ubuntu", "debian"):
        ui.say(f"\nUbuntu mounts the USB automatically at /media/{user}/VAULT.")
        if not shutil.which("fsck.exfat"):
            ui.say("Install the exFAT tools:  sudo apt install exfatprogs")
    return 0


def cmd_install_daemon(args):
    if IS_MAC:
        return install_launchd(args)
    if IS_LINUX and (args.dry_run or shutil.which("systemctl")):
        return install_systemd(args)
    die("install-daemon supports macOS (launchd) and Linux with systemd")


# ------------------------------------------------------------------------- cli

def build_parser():
    p = argparse.ArgumentParser(prog=SCRIPT_NAME, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, vault=True):
        if vault:
            sp.add_argument("--vault", "--usb", help="USB path (default: the plugged-in drive named VAULT)")
        sp.add_argument("-v", "--verbose", action="store_true", help="print every file")
        return sp

    sp = common(sub.add_parser("init", help="prepare the USB drive"), vault=False)
    sp.add_argument("path")
    sp.add_argument("--yes", action="store_true")

    sp = common(sub.add_parser("first-run", help="first run on this computer"))
    sp.add_argument("-n", "--dry-run", action="store_true", help="show everything it would do, change nothing")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--again", action="store_true", help="run it again on a computer already set up")

    common(sub.add_parser("sync", help="one pass of what the daemon does"))

    sp = common(sub.add_parser("watch", help="the daemon"))
    sp.add_argument("--interval", type=int, default=60, help="seconds between passes (default 60)")
    sp.add_argument("--once", action="store_true", help="one pass, then exit")

    sp = common(sub.add_parser("install-daemon", help="run 'watch' in the background on this OS"))
    sp.add_argument("--interval", type=int, default=60)
    sp.add_argument("-n", "--dry-run", action="store_true", help="print the service file, install nothing")
    sp.add_argument("--remove", action="store_true", help="stop the daemon")
    sp.add_argument("--python", help=f"interpreter (default: the '{CONDA_ENV}' conda env)")

    common(sub.add_parser("status", help="this computer and the USB"))
    common(sub.add_parser("verify", help="re-read every file on the USB"))
    sp = common(sub.add_parser("extras", help="extra folders/files to back up besides Documents and Downloads"),
                vault=False)
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("paths", nargs="*", help="folders or files, e.g. ~/Pictures/Scans ~/.zshrc")

    common(sub.add_parser("config", help="show the settings file"), vault=False)
    return p


def main(argv=None):
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is required")
    args = build_parser().parse_args(argv)
    ui.verbose = getattr(args, "verbose", False)
    handlers = {
        "init": cmd_init, "first-run": cmd_first_run, "sync": cmd_sync, "watch": cmd_watch,
        "install-daemon": cmd_install_daemon, "status": cmd_status, "verify": cmd_verify,
        "config": cmd_config, "extras": cmd_extras,
    }
    try:
        return handlers[args.cmd](args)
    except VaultError as e:
        ui.end_progress()
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        ui.close_log()


if __name__ == "__main__":
    sys.exit(main())
