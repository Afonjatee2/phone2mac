#!/usr/bin/env python3
"""
Phone2Mac - a reliable two-way clipboard + file bridge between your phone and your Mac.

Runs a small HTTP server on the Mac (Python standard library only, no installs).
Your phone opens the web page (or uses an iOS Shortcut) over your local network:

  * Phone -> Mac: text, links, photos, videos, any file. Text lands straight on
    the Mac clipboard; files are saved to ~/Phone2Mac and put on the clipboard
    too, so Cmd+V pastes them into Finder, Mail, Messages, Slack, ...
  * Mac -> Phone: everything you copy on the Mac (text, links, screenshots,
    files copied in Finder) shows up on the phone instantly, one tap to copy.

Run:  python3 phone2mac.py          (or ./install.sh to run it at login)
Pair: open http://localhost:8765/pair on the Mac and scan the QR code.
"""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import zlib
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.1.0"
DEFAULT_PORT = 8765
MAX_ITEMS = 100                 # history length
INLINE_TEXT_LIMIT = 200_000     # texts larger than this are truncated in the feed
MAX_TEXT = 5_000_000            # ignore clipboard texts larger than this (5 MB)
MAX_UPLOAD = 20 * 1024 ** 3     # 20 GB hard cap per request
CHUNK = 256 * 1024
LONG_POLL_SECONDS = 25
APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"

for _ext, _type in {
    ".heic": "image/heic", ".heif": "image/heif", ".webp": "image/webp",
    ".mov": "video/quicktime", ".m4v": "video/x-m4v", ".mp4": "video/mp4",
    ".m4a": "audio/mp4", ".md": "text/markdown", ".dng": "image/x-adobe-dng",
}.items():
    mimetypes.add_type(_type, _ext)

LINK_RE = re.compile(r"\s*((?:https?://|www\.)\S+)\s*", re.I)
# Clipboard types that password managers etc. use to say "don't record me"
# (see nspasteboard.org) plus our own marker so we never echo our own writes.


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# History store
# --------------------------------------------------------------------------

def new_id() -> str:
    return secrets.token_hex(6)


class Store:
    """Thread-safe clipboard history persisted to JSON. Newest item first.

    The newest item always mirrors what is currently on the Mac clipboard
    (phone sends put themselves on the Mac clipboard, Mac copies are captured).
    """

    def __init__(self, data_dir: Path):
        self.files_dir = data_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "history.json"
        self.cond = threading.Condition()
        self.items: list[dict] = []
        # Start from wall-clock ms so clients notice a restart as a change.
        self.version = int(time.time() * 1000)
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text("utf-8"))
            self.items = [i for i in data.get("items", []) if isinstance(i, dict) and "id" in i]
        except FileNotFoundError:
            pass
        except Exception as e:  # corrupt file: keep a copy, start fresh
            log("history unreadable, starting fresh:", e)
            try:
                shutil.copy(self.path, self.path.with_suffix(".corrupt.json"))
            except Exception:
                pass

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": self.items}, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.path)

    def _changed(self):
        self.version += 1
        try:
            self._save()
        except Exception as e:
            log("could not save history:", e)
        self.cond.notify_all()

    def _discard(self, item: dict):
        if item.get("owned") and item.get("path"):
            p = Path(item["path"])
            try:
                if self.files_dir in p.parents:
                    shutil.rmtree(p.parent if p.parent != self.files_dir else p, ignore_errors=True)
            except Exception:
                pass

    def add(self, item: dict) -> dict:
        with self.cond:
            self.items.insert(0, item)
            while len(self.items) > MAX_ITEMS:
                self._discard(self.items.pop())
            self._changed()
        return item

    def add_many(self, items: list[dict]):
        with self.cond:
            for item in items:
                self.items.insert(0, item)
            while len(self.items) > MAX_ITEMS:
                self._discard(self.items.pop())
            self._changed()

    def get(self, item_id: str) -> dict | None:
        with self.cond:
            return next((i for i in self.items if i["id"] == item_id), None)

    def top(self) -> dict | None:
        with self.cond:
            return self.items[0] if self.items else None

    def find_recent(self, pred, window: int = 15) -> dict | None:
        with self.cond:
            return next((i for i in self.items[:window] if pred(i)), None)

    def bump(self, item_id: str) -> dict | None:
        """Move an item to the top (it is now what's on the Mac clipboard)."""
        with self.cond:
            for idx, item in enumerate(self.items):
                if item["id"] == item_id:
                    self.items.pop(idx)
                    item["created"] = time.time()
                    self.items.insert(0, item)
                    self._changed()
                    return item
        return None

    def delete(self, item_id: str) -> bool:
        with self.cond:
            for idx, item in enumerate(self.items):
                if item["id"] == item_id:
                    self._discard(self.items.pop(idx))
                    self._changed()
                    return True
        return False

    def clear(self):
        with self.cond:
            for item in self.items:
                self._discard(item)
            self.items = []
            self._changed()

    def snapshot(self, since: int | None = None, timeout: float = 0) -> tuple[int, list[dict]]:
        with self.cond:
            if since is not None and timeout > 0:
                self.cond.wait_for(lambda: self.version != since, timeout=timeout)
            return self.version, [dict(i) for i in self.items]


# --------------------------------------------------------------------------
# Clipboard backends
# --------------------------------------------------------------------------

WATCH_JS = r"""
ObjC.import('AppKit');
ObjC.import('Foundation');

var SKIP = ['com.phone2mac.marker', 'org.nspasteboard.ConcealedType',
            'org.nspasteboard.TransientType', 'org.nspasteboard.AutoGeneratedType',
            'com.agilebits.onepassword', 'de.petermaurer.TransientPasteboardType'];
var IMAGE_TYPES = ['public.png', 'public.tiff', 'public.jpeg', 'public.heic'];

function out(obj) {
  var s = JSON.stringify(obj) + "\n";
  $.NSFileHandle.fileHandleWithStandardOutput.writeData($(s).dataUsingEncoding($.NSUTF8StringEncoding));
}

function isNil(o) { return o === undefined || o === null || (typeof o.isNil === 'function' && o.isNil()); }

function capture(pb, dir) {
  var types = ObjC.deepUnwrap(pb.types) || [];
  for (var i = 0; i < SKIP.length; i++) {
    if (types.indexOf(SKIP[i]) >= 0) return {kind: 'skip', reason: SKIP[i]};
  }
  if (types.indexOf('com.apple.is-remote-clipboard') >= 0) return {kind: 'remote'};
  if (types.indexOf('public.file-url') >= 0) {
    var paths = [];
    var items = pb.pasteboardItems;
    for (var j = 0; j < items.count; j++) {
      var s = items.objectAtIndex(j).stringForType('public.file-url');
      if (isNil(s)) continue;
      var u = $.NSURL.URLWithString(s);
      if (!isNil(u) && u.isFileURL) paths.push(ObjC.unwrap(u.path));
    }
    if (paths.length) return {kind: 'files', paths: paths};
  }
  var text = ObjC.unwrap(pb.stringForType('public.utf8-plain-text'));
  var hasImage = false;
  for (var k = 0; k < IMAGE_TYPES.length; k++) if (types.indexOf(IMAGE_TYPES[k]) >= 0) hasImage = true;
  if (hasImage && (!text || /^\s*https?:\/\/\S+\s*$/.test(text))) {
    var data = pb.dataForType('public.png');
    if (isNil(data)) {
      data = null;
      for (var m = 1; m < IMAGE_TYPES.length && !data; m++) {
        var raw = pb.dataForType(IMAGE_TYPES[m]);
        if (isNil(raw)) continue;
        var rep = $.NSBitmapImageRep.imageRepWithData(raw);
        if (!isNil(rep)) data = rep.representationUsingTypeProperties(4 /* PNG */, $.NSDictionary.dictionary);
        if (isNil(data)) data = null;
      }
    }
    if (data) {
      var p = dir + '/clip-' + Date.now() + '.png';
      if (data.writeToFileAtomically(p, true)) return {kind: 'image', path: p};
    }
  }
  if (text) return {kind: 'text', text: text};
  return {kind: 'skip', reason: 'unsupported', types: types.slice(0, 15)};
}

function run(argv) {
  var dir = argv[0];
  var pb = $.NSPasteboard.generalPasteboard;
  var last = argv.length > 1 && argv[1] !== '' ? parseInt(argv[1], 10) : pb.changeCount;
  out({event: 'ready', change: pb.changeCount});
  var ticks = 0, handled = 0;
  while (true) {
    var c = pb.changeCount;
    if (c !== last) {
      last = c;
      try { var r = capture(pb, dir); r.event = 'change'; r.change = c; out(r); }
      catch (e) { out({event: 'error', change: c, message: String(e)}); }
      // Exit now and then so the parent restarts us: JXA never drains its
      // autorelease pool inside this loop, so this keeps memory flat.
      if (++handled >= 150) { out({event: 'recycle', change: c}); return ''; }
    }
    if (++ticks >= 80) { ticks = 0; out({event: 'tick', change: c}); }
    $.NSThread.sleepForTimeInterval(0.35);
  }
}
"""

SET_JS = r"""
ObjC.import('AppKit');
ObjC.import('Foundation');

function isNil(o) { return o === undefined || o === null || (typeof o.isNil === 'function' && o.isNil()); }

function run(argv) {
  var mode = argv[0];
  var pb = $.NSPasteboard.generalPasteboard;
  var arr = $.NSMutableArray.alloc.init;
  var it;
  if (mode === 'text') {
    var s = $.NSString.stringWithContentsOfFileEncodingError(argv[1], $.NSUTF8StringEncoding, null);
    if (isNil(s)) throw new Error('could not read text file');
    it = $.NSPasteboardItem.alloc.init;
    it.setStringForType(s, 'public.utf8-plain-text');
    it.setStringForType($(''), 'com.phone2mac.marker');
    arr.addObject(it);
  } else if (mode === 'image') {
    // argv: image <path> <uti>
    var path = argv[1];
    it = $.NSPasteboardItem.alloc.init;
    it.setStringForType($.NSURL.fileURLWithPath(path).absoluteString, 'public.file-url');
    var raw = $.NSData.dataWithContentsOfFile(path);
    if (!isNil(raw) && argv[2]) it.setDataForType(raw, argv[2]);
    var img = $.NSImage.alloc.initWithContentsOfFile(path);
    if (!isNil(img)) {
      var tiff = img.TIFFRepresentation;
      if (!isNil(tiff)) {
        it.setDataForType(tiff, 'public.tiff');
        if (argv[2] !== 'public.png') {
          var rep = $.NSBitmapImageRep.imageRepWithData(tiff);
          if (!isNil(rep)) {
            var png = rep.representationUsingTypeProperties(4, $.NSDictionary.dictionary);
            if (!isNil(png)) it.setDataForType(png, 'public.png');
          }
        }
      }
    }
    it.setStringForType($(''), 'com.phone2mac.marker');
    arr.addObject(it);
  } else if (mode === 'files') {
    for (var i = 1; i < argv.length; i++) {
      it = $.NSPasteboardItem.alloc.init;
      it.setStringForType($.NSURL.fileURLWithPath(argv[i]).absoluteString, 'public.file-url');
      it.setStringForType($(''), 'com.phone2mac.marker');
      arr.addObject(it);
    }
  } else {
    throw new Error('unknown mode ' + mode);
  }
  pb.clearContents;
  return pb.writeObjects(arr) ? 'ok' : 'fail';
}
"""

IMAGE_UTIS = {
    "image/png": "public.png", "image/jpeg": "public.jpeg", "image/heic": "public.heic",
    "image/heif": "public.heif", "image/gif": "com.compuserve.gif", "image/tiff": "public.tiff",
    "image/webp": "org.webmproject.webp",
}


def applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MemoryClipboard:
    """Stand-in clipboard for non-Mac machines (tests, trying the UI on Linux)."""
    name = "memory"

    def __init__(self):
        self.writes: list[tuple] = []

    def set_text(self, text: str) -> bool:
        self.writes.append(("text", text))
        return True

    def set_files(self, paths: list[str], mime: str | None = None) -> bool:
        self.writes.append(("files", list(paths)))
        return True

    def start_watcher(self, on_event):
        pass

    def notify(self, title: str, message: str):
        pass

    def reveal(self, path: str):
        pass


class MacClipboard:
    name = "macos"

    def __init__(self, data_dir: Path, notifications: bool = True):
        self.notifications = notifications
        sd = data_dir / "scripts"
        sd.mkdir(parents=True, exist_ok=True)
        self.watch_js = sd / "watch.js"
        self.set_js = sd / "set.js"
        self.watch_js.write_text(WATCH_JS, "utf-8")
        self.set_js.write_text(SET_JS, "utf-8")
        self.clip_dir = data_dir / "files" / "_clip"
        self.clip_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = data_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.last_change = ""
        self.write_lock = threading.Lock()

    def _set(self, args: list[str]) -> bool:
        with self.write_lock:
            try:
                r = subprocess.run(["osascript", "-l", "JavaScript", str(self.set_js), *args],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and "ok" in r.stdout:
                    return True
                log("clipboard write failed:", r.stderr.strip() or r.stdout.strip())
            except Exception as e:
                log("clipboard write error:", e)
            return False

    def set_text(self, text: str) -> bool:
        fd, tmp = tempfile.mkstemp(dir=self.tmp_dir, suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            if self._set(["text", tmp]):
                return True
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        # Fallback: pbcopy (won't carry our marker, but text de-dupe catches the echo).
        try:
            env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), env=env, timeout=10, check=True)
            return True
        except Exception as e:
            log("pbcopy failed:", e)
            return False

    def set_files(self, paths: list[str], mime: str | None = None) -> bool:
        if not paths:
            return False
        if len(paths) == 1 and mime in IMAGE_UTIS:
            return self._set(["image", paths[0], IMAGE_UTIS[mime]])
        return self._set(["files", *paths])

    def start_watcher(self, on_event):
        threading.Thread(target=self._watch_loop, args=(on_event,), daemon=True, name="clip-watch").start()

    def _watch_loop(self, on_event):
        backoff = 1.0
        while True:
            started = time.time()
            try:
                proc = subprocess.Popen(
                    ["osascript", "-l", "JavaScript", str(self.watch_js), str(self.clip_dir), str(self.last_change)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            except Exception as e:
                log("cannot start clipboard watcher:", e)
                time.sleep(10)
                continue
            last_line = [time.time()]

            def watchdog():  # kill a hung watcher (it prints a tick every ~30s)
                while proc.poll() is None:
                    if time.time() - last_line[0] > 120:
                        log("clipboard watcher stalled; restarting")
                        proc.kill()
                        return
                    time.sleep(5)

            threading.Thread(target=watchdog, daemon=True).start()
            for line in proc.stdout:
                last_line[0] = time.time()
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                if "change" in evt:
                    self.last_change = str(evt["change"])
                try:
                    on_event(evt)
                except Exception:
                    log("error handling clipboard event:\n" + traceback.format_exc())
            proc.wait()
            err = (proc.stderr.read() or "").strip() if proc.stderr else ""
            if err:
                log("clipboard watcher exited:", err[-500:])
            if time.time() - started > 60:
                backoff = 1.0
            else:
                backoff = min(backoff * 2, 30) if err else 0.2
            time.sleep(backoff)

    def notify(self, title: str, message: str):
        if not self.notifications:
            return
        try:
            subprocess.Popen(["osascript", "-e",
                              f"display notification {applescript_str(message)} with title {applescript_str(title)}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def reveal(self, path: str):
        subprocess.Popen(["open", "-R", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def safe_name(name: str | None, default: str = "") -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f:]", "", name).strip().lstrip(".").strip()
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[: 180 - len(ext)] + ext
    return name or default


def unique_path(folder: Path, name: str) -> Path:
    p = folder / name
    if not p.exists():
        return p
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        p = folder / f"{stem} ({n}){ext}"
        if not p.exists():
            return p
        n += 1


def sniff(head: bytes) -> tuple[str, str] | None:
    if head.startswith(b"\x89PNG"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head[:4] in (b"GIF8",):
        return "image/gif", ".gif"
    if head.startswith(b"%PDF"):
        return "application/pdf", ".pdf"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic", ".heic"
        if brand == b"qt  ":
            return "video/quicktime", ".mov"
        if brand.startswith(b"M4A"):
            return "audio/mp4", ".m4a"
        return "video/mp4", ".mp4"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip", ".zip"
    return None


RICH_TEXT_TYPES = ("text/rtf", "application/rtf", "text/richtext", "application/x-rtf", "text/html")


def rich_to_text(path: str, fmt: str) -> str | None:
    """Plain text out of an RTF/HTML body (what iOS sends when you copy styled text)."""
    if sys.platform == "darwin" and shutil.which("textutil"):
        try:
            r = subprocess.run(["textutil", "-format", fmt, "-convert", "txt", "-encoding", "UTF-8",
                                "-stdout", path], capture_output=True, timeout=20)
            if r.returncode == 0:
                return r.stdout.decode("utf-8", "replace").rstrip("\n")
        except Exception as e:
            log("textutil failed:", e)
    raw = Path(path).read_bytes().decode("utf-8", "replace")
    if fmt == "html":
        from html.parser import HTMLParser

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.out, self.skip = [], 0

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "head"):
                    self.skip += 1
                elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
                    self.out.append("\n")

            def handle_endtag(self, tag):
                if tag in ("script", "style", "head"):
                    self.skip = max(0, self.skip - 1)

            def handle_data(self, data):
                if not self.skip:
                    self.out.append(data)

        p = P()
        p.feed(raw)
        return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()
    # crude RTF fallback (only used off-Mac)
    raw = re.sub(r"\{\\\*[^{}]*\}", "", raw)
    raw = re.sub(r"\\par[d]?\b ?", "\n", raw)
    raw = re.sub(r"\\'[0-9a-f]{2}|\\[a-z]+-?\d* ?|[{}]", "", raw)
    return raw.strip()


def kind_for_mime(mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "file"


def text_kind(text: str) -> str:
    return "link" if len(text) < 4096 and LINK_RE.fullmatch(text) else "text"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def device_from_headers(headers) -> str:
    d = re.sub(r"[^\w .\-]", "", headers.get("X-Device", ""))[:24].strip()
    if d:
        return d
    ua = headers.get("User-Agent", "")
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        return "Android"
    if any(k in ua for k in ("Shortcuts", "WorkflowKit", "CFNetwork", "Darwin")):
        return "iPhone"
    return "Phone"


def get_local_hostname() -> str:
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip() + ".local"
        except Exception:
            pass
    h = socket.gethostname()
    return h if "." in h else h + ".local"


def get_lan_ips() -> list[str]:
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.254.254.254", 1))  # no packet is sent
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def make_icon_png(size: int = 180) -> bytes:
    """Draw the app icon (two arrows on a gradient) without any image library."""
    s = float(size)
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            t = (x + y) / (2 * s)
            r, g, b = int(79 + (147 - 79) * t), int(70 + (51 - 70) * t), int(229 + (234 - 229) * t)
            fx, fy = x + 0.5, y + 0.5
            white = (
                (0.24 * s <= fx <= 0.62 * s and 0.355 * s <= fy <= 0.445 * s)
                or (0.60 * s <= fx <= 0.78 * s and abs(fy - 0.40 * s) <= (0.78 * s - fx) * 0.8)
                or (0.38 * s <= fx <= 0.76 * s and 0.555 * s <= fy <= 0.645 * s)
                or (0.22 * s <= fx <= 0.40 * s and abs(fy - 0.60 * s) <= (fx - 0.22 * s) * 0.8)
            )
            rows += bytes((255, 255, 255)) if white else bytes((r, g, b))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------
# iOS Shortcuts, generated ready-to-import (signed on the Mac with `shortcuts sign`)
# --------------------------------------------------------------------------

SHORTCUTS = {"send": "Send to Mac", "get": "Get from Mac", "share": "Share to Mac"}
# Share-sheet input types. Safari pages/articles are deliberately left out so
# iOS hands us their URL instead.
SHARE_INPUT_CLASSES = [
    "WFAppStoreAppContentItem", "WFContactContentItem", "WFDateContentItem", "WFEmailAddressContentItem",
    "WFGenericFileContentItem", "WFImageContentItem", "WFiTunesProductContentItem", "WFLocationContentItem",
    "WFDCMapsLinkContentItem", "WFAVAssetContentItem", "WFPDFContentItem", "WFPhoneNumberContentItem",
    "WFRichTextContentItem", "WFStringContentItem", "WFURLContentItem",
]


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4()).upper()


def _tstr(text: str, attachments: dict | None = None) -> dict:
    return {"Value": {"string": text, "attachmentsByRange": attachments or {}}, "WFSerializationType": "WFTextTokenString"}


def _var_in_text(var: dict) -> dict:
    return _tstr("￼", {"{0, 1}": var})


def _attach(var: dict) -> dict:
    return {"Value": var, "WFSerializationType": "WFTextTokenAttachment"}


def _dict_field(pairs: list[tuple[str, dict]]) -> dict:
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0, "WFKey": _tstr(k), "WFValue": v} for k, v in pairs]},
        "WFSerializationType": "WFDictionaryFieldValue"}


def _action(ident: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": "is.workflow.actions." + ident, "WFWorkflowActionParameters": params}


def build_shortcut(kind: str, base: str, token: str) -> dict:
    """Return the Shortcuts workflow plist (as a dict) for 'send' or 'get'."""
    q = urllib.parse.quote(token, safe="")
    common = {
        "WFWorkflowClientVersion": "2605.0.5", "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900", "WFWorkflowImportQuestions": [],
        "WFWorkflowOutputContentItemClasses": [], "WFQuickActionSurfaces": [],
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 2071128575, "WFWorkflowIconGlyphNumber": 61440},
    }
    if kind == "get":
        dl = _uuid()
        actions = [
            _action("downloadurl", UUID=dl, WFURL=f"{base}/api/latest?k={q}", WFHTTPMethod="GET"),
            _action("setclipboard", WFInput=_attach({"Type": "ActionOutput", "OutputName": "Contents of URL", "OutputUUID": dl})),
            _action("notification", WFNotificationActionTitle="Phone2Mac", WFNotificationActionBody="Copied from Mac",
                    WFNotificationActionSound=False),
        ]
        return dict(common, WFWorkflowActions=actions, WFWorkflowTypes=[], WFWorkflowInputContentItemClasses=[],
                    WFWorkflowHasShortcutInputVariables=False)

    if kind == "send":
        # Deliberately minimal (for Back Tap): Get Clipboard -> POST it as the raw body.
        # The Mac works out whether it's text, rich text, a photo or a file.
        clip = _uuid()
        actions = [
            _action("getclipboard", UUID=clip),
            _action("downloadurl", UUID=_uuid(), WFURL=f"{base}/api/send?k={q}&reply=text", WFHTTPMethod="POST",
                    WFHTTPBodyType="File", ShowHeaders=True,
                    WFRequestVariable=_attach({"Type": "ActionOutput", "OutputName": "Clipboard", "OutputUUID": clip}),
                    WFHTTPHeaders=_dict_field([("X-Device", _tstr("iPhone"))])),
            _action("notification", WFNotificationActionTitle="Phone2Mac",
                    WFNotificationActionBody="Sent to Mac, press ⌘V", WFNotificationActionSound=False),
        ]
        return dict(common, WFWorkflowActions=actions, WFWorkflowTypes=[], WFWorkflowInputContentItemClasses=[],
                    WFWorkflowHasShortcutInputVariables=False)

    if kind != "share":
        raise ValueError(kind)
    item = {"Type": "Variable", "VariableName": "Repeat Item"}
    batch = _var_in_text({"Type": "CurrentDate"})
    send_url = f"{base}/api/send?k={q}&reply=text"
    loop, if_url, if_text, type_uuid = _uuid(), _uuid(), _uuid(), _uuid()
    type_var = {"Type": "ActionOutput", "OutputName": "Type", "OutputUUID": type_uuid}

    def post_text():
        return _action("downloadurl", UUID=_uuid(), WFURL=send_url, WFHTTPMethod="POST", WFHTTPBodyType="JSON",
                       ShowHeaders=True, WFHTTPHeaders=_dict_field([("X-Device", _tstr("iPhone"))]),
                       WFJSONValues=_dict_field([("text", _var_in_text(item))]))

    def cond(group, contains):
        return _action("conditional", GroupingIdentifier=group, WFControlFlowMode=0, WFCondition=99,
                       WFConditionalActionString=contains,
                       WFInput={"Type": "Variable", "Variable": _attach(type_var)})

    actions = [
        _action("repeat.each", GroupingIdentifier=loop, WFControlFlowMode=0, WFInput=_attach({"Type": "ExtensionInput"})),
        _action("getitemtype", UUID=type_uuid, WFInput=_attach(item)),
        cond(if_url, "URL"),
        post_text(),
        _action("conditional", GroupingIdentifier=if_url, WFControlFlowMode=1),
        cond(if_text, "Text"),
        post_text(),
        _action("conditional", GroupingIdentifier=if_text, WFControlFlowMode=1),
        _action("downloadurl", UUID=_uuid(), WFURL=send_url, WFHTTPMethod="POST", WFHTTPBodyType="File",
                WFRequestVariable=_attach(item), ShowHeaders=True,
                WFHTTPHeaders=_dict_field([
                    ("X-Filename", _var_in_text(dict(item, Aggrandizements=[
                        {"Type": "WFPropertyVariableAggrandizement", "PropertyName": "Name"}]))),
                    ("X-Batch", batch),
                    ("X-Device", _tstr("iPhone"))])),
        _action("conditional", GroupingIdentifier=if_text, WFControlFlowMode=2),
        _action("conditional", GroupingIdentifier=if_url, WFControlFlowMode=2),
        _action("repeat.each", GroupingIdentifier=loop, WFControlFlowMode=2),
        _action("notification", WFNotificationActionTitle="Phone2Mac",
                WFNotificationActionBody="Sent to Mac, press ⌘V", WFNotificationActionSound=False),
    ]
    return dict(common, WFWorkflowActions=actions, WFWorkflowTypes=["ActionExtension"],
                WFWorkflowInputContentItemClasses=SHARE_INPUT_CLASSES, WFWorkflowHasShortcutInputVariables=True,
                WFWorkflowNoInputBehavior={"Name": "WFWorkflowNoInputBehaviorGetClipboard", "Parameters": {}})


def signed_shortcut(app: "App", kind: str, base: str) -> Path:
    """Build + sign a shortcut file (macOS only). Cached until the address or key changes."""
    import hashlib
    import plistlib
    if sys.platform != "darwin" or not shutil.which("shortcuts"):
        raise RuntimeError("Shortcut files can only be generated on a Mac (macOS 12 or later).")
    key = hashlib.sha256(f"{VERSION}|{kind}|{base}|{app.token}".encode()).hexdigest()[:16]
    out_dir = app.data_dir / "shortcuts" / key
    out = out_dir / f"{SHORTCUTS[kind]}.shortcut"
    if out.exists():
        return out
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    unsigned = out_dir / "unsigned.shortcut"
    unsigned.write_bytes(plistlib.dumps(build_shortcut(kind, base, app.token), fmt=plistlib.FMT_BINARY))
    r = subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", str(unsigned), "--output", str(out)],
                       capture_output=True, text=True, timeout=120)
    unsigned.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
        raise RuntimeError("Signing failed (are you signed in to iCloud on this Mac?): "
                           + (r.stderr or r.stdout).strip()[-300:])
    return out


# --------------------------------------------------------------------------
# Request body readers (Content-Length, chunked, multipart streaming)
# --------------------------------------------------------------------------

class BodyReader:
    def __init__(self, rfile, headers):
        self.rfile = rfile
        self.chunked = "chunked" in headers.get("Transfer-Encoding", "").lower()
        self.remaining = 0 if self.chunked else int(headers.get("Content-Length") or 0)
        self.chunk_left = 0
        self.done = False
        self.total = 0

    def read(self, n: int = CHUNK) -> bytes:
        if self.chunked:
            if self.done:
                return b""
            if self.chunk_left == 0:
                line = self.rfile.readline(1024)
                if not line:
                    raise ConnectionError("unexpected end of chunked body")
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(1024)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    self.done = True
                    return b""
                self.chunk_left = size
            data = self.rfile.read(min(n, self.chunk_left))
            if not data:
                raise ConnectionError("unexpected end of chunked body")
            self.chunk_left -= len(data)
            if self.chunk_left == 0:
                self.rfile.readline(16)
        else:
            if self.remaining <= 0:
                return b""
            data = self.rfile.read(min(n, self.remaining))
            if not data:
                raise ConnectionError("client disconnected")
            self.remaining -= len(data)
        self.total += len(data)
        if self.total > MAX_UPLOAD:
            raise ValueError("upload too large")
        return data

    def read_all(self, limit: int) -> bytes:
        buf = bytearray()
        while True:
            d = self.read()
            if not d:
                return bytes(buf)
            buf += d
            if len(buf) > limit:
                raise ValueError("body too large")

    def drain(self):
        try:
            while self.read():
                pass
        except Exception:
            pass


def parse_part_headers(raw: bytes) -> Message:
    m = Message()
    for line in raw.decode("utf-8", "replace").split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            m[k.strip()] = v.strip()
    return m


def parse_multipart(reader: BodyReader, boundary: bytes, on_part):
    """Stream a multipart/form-data body. on_part(headers) -> sink with write()/close()."""
    delim = b"\r\n--" + boundary
    buf = b"\r\n"
    keep = len(delim) + 4

    def fill() -> bool:
        nonlocal buf
        d = reader.read()
        if not d:
            return False
        buf += d
        return True

    while True:  # skip preamble
        i = buf.find(delim)
        if i >= 0:
            buf = buf[i + len(delim):]
            break
        buf = buf[-keep:]
        if not fill():
            raise ValueError("multipart boundary not found")
    while True:
        while len(buf) < 2:
            if not fill():
                raise ValueError("truncated multipart body")
        if buf[:2] == b"--":
            break
        buf = buf.lstrip(b" \t")
        if buf[:2] == b"\r\n":
            buf = buf[2:]
        while (j := buf.find(b"\r\n\r\n")) < 0:
            if len(buf) > 65536 or not fill():
                raise ValueError("bad multipart headers")
        headers = parse_part_headers(buf[:j])
        buf = buf[j + 4:]
        sink = on_part(headers)
        try:
            while True:
                i = buf.find(delim)
                if i >= 0:
                    sink.write(buf[:i])
                    buf = buf[i + len(delim):]
                    break
                safe = len(buf) - keep
                if safe > 0:
                    sink.write(buf[:safe])
                    buf = buf[safe:]
                if not fill():
                    raise ValueError("truncated multipart body")
        finally:
            sink.close()
    reader.drain()


class MemorySink:
    def __init__(self, limit: int):
        self.buf = bytearray()
        self.limit = limit

    def write(self, b: bytes):
        self.buf += b
        if len(self.buf) > self.limit:
            raise ValueError("text field too large")

    def close(self):
        pass


class FileSink:
    def __init__(self, folder: Path):
        fd, self.path = tempfile.mkstemp(dir=folder, prefix=".upload-")
        self.f = os.fdopen(fd, "wb")
        self.size = 0
        self.head = b""

    def write(self, b: bytes):
        if len(self.head) < 64:
            self.head += b[: 64 - len(self.head)]
        self.f.write(b)
        self.size += len(b)

    def close(self):
        if not self.f.closed:
            self.f.close()


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

class App:
    def __init__(self, data_dir: Path, inbox: Path, port: int, clipboard, verbose: bool = False):
        self.data_dir = data_dir
        self.inbox = inbox
        self.port = port
        self.verbose = verbose
        data_dir.mkdir(parents=True, exist_ok=True)
        inbox.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = data_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        for leftover in self.tmp_dir.glob(".upload-*"):
            leftover.unlink(missing_ok=True)
        self.store = Store(data_dir)
        self.clip = clipboard
        self.token = self._load_token()
        self.hostname = get_local_hostname()
        self.batches: dict[str, tuple[float, list[str]]] = {}
        self.batch_lock = threading.Lock()
        self._icon = None
        self.last_write = None  # (time, set_fn, args, label): what we last put on the Mac clipboard
        self.guard_lock = threading.Lock()

    # ---- writing the Mac clipboard (with a guard against Universal Clipboard) ----
    def write_clipboard(self, label: str, fn, *args) -> bool:
        ok = fn(*args)
        with self.guard_lock:
            self.last_write = (time.time(), fn, args, label) if ok else None
        if ok and sys.platform == "darwin" and fn == self.clip.set_text:
            threading.Thread(target=self._verify_text, args=(args[0],), daemon=True).start()
        return ok

    def _verify_text(self, text: str):
        time.sleep(4)
        try:
            env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
            now = subprocess.run(["pbpaste"], capture_output=True, env=env, timeout=5).stdout.decode("utf-8", "replace")
        except Exception as e:
            log("could not verify clipboard:", e)
            return
        if now.replace("\r\n", "\n") == text:
            log("verified: text is on the Mac clipboard")
        else:
            log("WARNING: the Mac clipboard was changed by something else within 4s. It now holds:",
                repr(now[:60]))

    def on_remote_clipboard(self):
        with self.guard_lock:
            lw = self.last_write
        if lw and time.time() - lw[0] < 45:
            log(f"Apple Universal Clipboard overwrote the clipboard {time.time() - lw[0]:.1f}s after "
                f"Phone2Mac set it; putting {lw[3]} back")
            time.sleep(0.3)
            lw[1](*lw[2])
        else:
            log("Apple Universal Clipboard changed the Mac clipboard (not captured)")

    # ---- token -----------------------------------------------------------
    def _load_token(self) -> str:
        p = self.data_dir / "token"
        try:
            t = p.read_text().strip()
            if len(t) >= 16:
                return t
        except FileNotFoundError:
            pass
        t = secrets.token_urlsafe(18)
        p.write_text(t)
        os.chmod(p, 0o600)
        return t

    def reset_token(self):
        (self.data_dir / "token").unlink(missing_ok=True)
        self.token = self._load_token()

    def check_token(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())

    # ---- URLs ------------------------------------------------------------
    def base_urls(self) -> list[str]:
        urls = [f"http://{self.hostname}:{self.port}"]
        urls += [f"http://{ip}:{self.port}" for ip in get_lan_ips()]
        return urls

    def icon(self) -> bytes:
        if self._icon is None:
            self._icon = make_icon_png(180)
        return self._icon

    # ---- clipboard events from the Mac ---------------------------------------
    def on_clip_event(self, evt: dict):
        if evt.get("event") == "error":
            log("clipboard read error:", evt.get("message"))
            return
        if evt.get("event") != "change":
            return
        kind = evt.get("kind")
        if kind == "remote":
            threading.Thread(target=self.on_remote_clipboard, daemon=True).start()
            return
        if kind == "text":
            text = evt.get("text") or ""
            if not text.strip() or len(text) > MAX_TEXT:
                return
            dup = self.store.find_recent(lambda i: i.get("text") == text)
            if dup:
                self.store.bump(dup["id"])
                return
            self.store.add(self._item("mac", text_kind(text), "Mac", text=text))
            if self.verbose:
                log("captured Mac text", len(text), "chars")
        elif kind == "image":
            src = Path(evt.get("path", ""))
            if not src.exists():
                return
            item = self._item("mac", "image", "Mac", mime="image/png",
                              name=time.strftime("Clipboard %Y-%m-%d at %H.%M.%S.png"), owned=True)
            dest_dir = self.store.files_dir / item["id"]
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / item["name"]
            shutil.move(str(src), dest)
            item.update(path=str(dest), size=dest.stat().st_size)
            self.store.add(item)
        elif kind == "files":
            new = []
            for p in reversed(evt.get("paths") or []):
                path = Path(p)
                if not path.exists():
                    continue
                dup = self.store.find_recent(lambda i: i.get("path") == str(path))
                if dup:
                    self.store.bump(dup["id"])
                    continue
                if path.is_dir():
                    new.append(self._item("mac", "folder", "Mac", name=path.name, path=str(path),
                                          mime="application/zip", size=None))
                else:
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    new.append(self._item("mac", kind_for_mime(mime), "Mac", name=path.name, path=str(path),
                                          mime=mime, size=path.stat().st_size))
            if new:
                self.store.add_many(new)

    @staticmethod
    def _item(source: str, kind: str, device: str, **kw) -> dict:
        item = {"id": new_id(), "source": source, "device": device, "kind": kind, "created": time.time()}
        item.update(kw)
        return item

    # ---- receiving from the phone ----------------------------------------------
    def receive_text(self, text: str, device: str) -> dict | None:
        text = text.replace("\r\n", "\n")
        if not text.strip():
            return None
        kind = text_kind(text)
        if kind == "link":
            text = text.strip()
        ok = self.write_clipboard("the phone's text", self.clip.set_text, text)
        dup = self.store.find_recent(lambda i: i.get("text") == text, window=5)
        item = self.store.bump(dup["id"]) if dup else self.store.add(self._item("phone", kind, device, text=text))
        preview = text if len(text) < 80 else text[:77] + "..."
        self.clip.notify(f"From {device}" + ("" if ok else " (clipboard failed)"),
                         f"{'Link' if kind == 'link' else 'Text'} copied, press Cmd+V: {preview}")
        log(f"received {kind} from {device} ({len(text)} chars)")
        return item

    def finalize_upload(self, sink: FileSink, filename: str, ctype: str, device: str) -> dict:
        sniffed = sniff(sink.head)
        filename = safe_name(urllib.parse.unquote(filename or ""))
        guessed = mimetypes.guess_type(filename)[0] if filename else None
        mime = ctype if ctype and ctype not in ("application/octet-stream", "binary/octet-stream") else None
        mime = mime or guessed or (sniffed[0] if sniffed else "application/octet-stream")
        kind = kind_for_mime(mime)
        ext = (mimetypes.guess_extension(mime) or (sniffed[1] if sniffed else "")) if mime else ""
        if ext == ".jpe":
            ext = ".jpg"
        if not filename:
            label = {"image": "Photo", "video": "Video", "audio": "Audio"}.get(kind, "File")
            filename = time.strftime(f"{label} %Y-%m-%d at %H.%M.%S") + ext
        elif not os.path.splitext(filename)[1] and ext:
            filename += ext
        dest = unique_path(self.inbox, filename)
        shutil.move(sink.path, dest)
        return self._item("phone", kind, device, name=dest.name, path=str(dest), size=sink.size, mime=mime)

    def put_files_on_clipboard(self, items: list[dict], batch: str | None, device: str):
        paths = [i["path"] for i in items]
        if batch:
            with self.batch_lock:
                now = time.time()
                for k in [k for k, (t, _) in self.batches.items() if now - t > 600]:
                    del self.batches[k]
                _, prev = self.batches.get(batch, (now, []))
                paths = prev + paths
                self.batches[batch] = (now, paths)
        mime = items[0].get("mime") if len(paths) == 1 else None
        ok = self.write_clipboard(f"{len(paths)} file(s)", self.clip.set_files, paths, mime)
        if len(paths) == 1:
            msg = f"{items[0]['name']} ({human_size(items[0]['size'] or 0)}) saved to {self.inbox.name}"
        else:
            msg = f"{len(paths)} files saved to {self.inbox.name}"
        self.clip.notify(f"From {device}" + ("" if ok else " (clipboard failed)"),
                         msg + (", press Cmd+V to paste" if ok else ""))

    # ---- public view of items ------------------------------------------------
    @staticmethod
    def public(item: dict) -> dict:
        d = {k: item.get(k) for k in ("id", "source", "device", "kind", "created", "name", "size", "mime")}
        if "text" in item:
            t = item["text"]
            d["length"] = len(t)
            if len(t) > INLINE_TEXT_LIMIT:
                d["text"] = t[:INLINE_TEXT_LIMIT]
                d["truncated"] = True
            else:
                d["text"] = t
        elif item.get("path"):
            d["exists"] = os.path.exists(item["path"])
        return d


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Phone2Mac/" + VERSION

        def log_message(self, fmt, *args):
            if app.verbose:
                log(self.client_address[0], fmt % args)

        # ---- plumbing ------------------------------------------------------
        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def do_OPTIONS(self):
            self._send(204, b"", "text/plain")

        def is_local(self) -> bool:
            ip = self.client_address[0]
            if ip.startswith("::ffff:"):
                ip = ip[7:]
            try:
                return ipaddress.ip_address(ip.split("%")[0]).is_loopback
            except ValueError:
                return False

        def local_host_header(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("localhost", "127.0.0.1", "::1")

        def q(self, key: str, default: str | None = None) -> str | None:
            v = self.query.get(key)
            return v[0] if v else default

        def token(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return self.headers.get("X-Token") or self.q("k")

        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, obj, status: int = 200):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def send_text(self, text: str, status: int = 200):
            self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

        def error(self, status: int, message: str):
            self.close_connection = True
            self.send_json({"ok": False, "error": message}, status)

        def _dispatch(self, method: str):
            reader = None
            try:
                parsed = urllib.parse.urlsplit(self.path)
                self.query = urllib.parse.parse_qs(parsed.query)
                path = parsed.path.rstrip("/") or "/"
                reader = BodyReader(self.rfile, self.headers) if method in ("POST", "DELETE") else None
                self.route(method, path, reader)
            except (BrokenPipeError, ConnectionResetError, ConnectionError):
                self.close_connection = True
            except Exception as e:
                log("request error:", self.command, self.path.split("?")[0], "\n" + traceback.format_exc())
                try:
                    self.error(500, str(e))
                except Exception:
                    pass
            finally:
                if reader is not None and not self.close_connection:
                    reader.drain()

        # ---- routes -------------------------------------------------------
        def route(self, method: str, path: str, reader: BodyReader | None):
            if method in ("GET", "HEAD"):
                if path in ("/", "/index.html"):
                    return self.serve_static("index.html", "text/html; charset=utf-8")
                if path in ("/icon.png", "/apple-touch-icon.png", "/favicon.ico"):
                    return self._send(200, app.icon(), "image/png", {"Cache-Control": "max-age=86400"})
                if path == "/api/ping":
                    return self.send_json({"ok": True, "version": VERSION, "host": app.hostname})
                if path == "/pair":
                    if not (self.is_local() and self.local_host_header()):
                        return self.error(403, "Open this page on the Mac itself: http://localhost:%d/pair" % app.port)
                    return self._send(200, pair_page(app).encode("utf-8"), "text/html; charset=utf-8")

            if not app.check_token(self.token()):
                log(f"rejected {self.command} {path} from {self.client_address[0]}: missing or wrong pairing key")
                return self.error(401, "Not paired. On your Mac open http://localhost:%d/pair and scan the QR code." % app.port)

            if method in ("GET", "HEAD"):
                if path == "/api/items":
                    since = self.q("v")
                    wait = LONG_POLL_SECONDS if self.q("wait") and since else 0
                    version, items = app.store.snapshot(int(since) if since and since.isdigit() else None, wait)
                    return self.send_json({"version": version, "viewer": "mac" if self.is_local() else "phone",
                                           "host": app.hostname, "inbox": str(app.inbox),
                                           "items": [app.public(i) for i in items]})
                if path == "/api/latest":
                    item = app.store.top()
                    if not item:
                        return self.send_text("Nothing copied yet", 404)
                    return self.serve_item(item, inline=False)
                m = re.fullmatch(r"/api/shortcut/(send|get|share)(?:\.shortcut)?", path)
                if m:
                    host = self.q("host") or ""
                    if not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:.%]+\](:\d{1,5})?", host):
                        host = ""
                    base = f"http://{host}" if host else app.base_urls()[0]
                    try:
                        f = signed_shortcut(app, m.group(1), base)
                    except Exception as e:
                        return self.error(501, str(e))
                    return self.send_file(str(f), "application/octet-stream", f.name, inline=False)
                m = re.fullmatch(r"/api/(file|text)/([0-9a-f]+)(?:/.*)?", path)
                if m:
                    item = app.store.get(m.group(2))
                    if not item:
                        return self.error(404, "That item is no longer in the history")
                    return self.serve_item(item, inline=not self.q("dl"))
            elif method == "POST":
                if path == "/api/send":
                    return self.api_send(reader)
                m = re.fullmatch(r"/api/shortcut/(send|get|share)/open", path)
                if m:  # import into the Mac's Shortcuts app (iCloud then syncs it to the iPhone)
                    if not self.is_local():
                        return self.error(403, "Only available on the Mac")
                    try:
                        f = signed_shortcut(app, m.group(1), app.base_urls()[0])
                    except Exception as e:
                        return self.error(501, str(e))
                    subprocess.Popen(["open", str(f)])
                    return self.send_json({"ok": True})
                if path == "/api/clear":
                    app.store.clear()
                    return self.send_json({"ok": True})
                m = re.fullmatch(r"/api/(copy|delete|reveal)/([0-9a-f]+)", path)
                if m:
                    action, item = m.group(1), app.store.get(m.group(2))
                    if not item:
                        return self.error(404, "That item is no longer in the history")
                    if action == "delete":
                        app.store.delete(item["id"])
                        return self.send_json({"ok": True})
                    if action == "reveal":
                        if not self.is_local():
                            return self.error(403, "Only available on the Mac")
                        app.clip.reveal(item["path"])
                        return self.send_json({"ok": True})
                    return self.api_copy(item)
            elif method == "DELETE":
                m = re.fullmatch(r"/api/items/([0-9a-f]+)", path)
                if m and app.store.delete(m.group(1)):
                    return self.send_json({"ok": True})
            return self.error(404, "Not found")

        def serve_static(self, name: str, ctype: str):
            try:
                body = (WEB_DIR / name).read_bytes()
            except FileNotFoundError:
                return self.error(500, f"Missing {WEB_DIR / name}. Reinstall Phone2Mac.")
            self._send(200, body, ctype)

        # ---- copy an existing item onto the Mac clipboard --------------------
        def api_copy(self, item: dict):
            if "text" in item:
                ok = app.write_clipboard("the text", app.clip.set_text, item["text"])
            else:
                if not item.get("path") or not os.path.exists(item["path"]):
                    return self.error(410, "The file no longer exists on the Mac")
                ok = app.write_clipboard("the file", app.clip.set_files, [item["path"]], item.get("mime"))
            app.store.bump(item["id"])
            if not ok:
                return self.error(500, "Could not write to the Mac clipboard")
            return self.send_json({"ok": True, "message": "On your Mac clipboard"})

        # ---- receive anything from the phone ---------------------------------
        def api_send(self, reader: BodyReader):
            device = device_from_headers(self.headers)
            ua = self.headers.get("User-Agent", "")
            via = "Shortcut" if ("reply" in self.query or "CFNetwork" in ua or "Shortcuts" in ua) else "web page"
            log(f"send request from {device} via {via} ({self.client_address[0]})")
            raw_ct = self.headers.get("Content-Type") or "application/octet-stream"
            m = Message()
            m["Content-Type"] = raw_ct
            ctype = m.get_content_type()
            batch = self.headers.get("X-Batch") or self.q("batch")
            fname = self.headers.get("X-Filename") or self.q("name") or ""
            texts: list[str] = []
            files: list[dict] = []

            if ctype == "multipart/form-data":
                boundary = m.get_param("boundary")
                if not boundary:
                    return self.error(400, "multipart without boundary")
                sinks: list[tuple] = []

                def on_part(h: Message):
                    part_name = h.get_filename()
                    part_ct = h.get_content_type() if h.get("Content-Type") else ""
                    if part_name is None and (not part_ct or part_ct.startswith("text/")):
                        s = MemorySink(MAX_TEXT)
                        sinks.append(("text", s, None, None))
                    else:
                        s = FileSink(app.tmp_dir)
                        sinks.append(("file", s, part_name or "", part_ct))
                    return s

                try:
                    parse_multipart(reader, boundary.encode("latin-1"), on_part)
                except Exception:
                    for kind, s, _, _ in sinks:
                        if kind == "file":
                            Path(s.path).unlink(missing_ok=True)
                    raise
                for kind, s, name, part_ct in sinks:
                    if kind == "text":
                        texts.append(bytes(s.buf).decode("utf-8", "replace"))
                    elif s.size == 0 and not name:
                        Path(s.path).unlink(missing_ok=True)
                    else:
                        files.append(app.finalize_upload(s, name, part_ct, device))
            elif ctype == "application/json":
                data = json.loads(reader.read_all(MAX_TEXT) or b"{}")
                if isinstance(data, str):
                    texts.append(data)
                elif isinstance(data, dict):
                    v = data.get("text") or data.get("url") or data.get("content")
                    if isinstance(v, str):
                        texts.append(v)
            elif ctype == "application/x-www-form-urlencoded":
                form = urllib.parse.parse_qs(reader.read_all(MAX_TEXT).decode("utf-8", "replace"))
                v = (form.get("text") or form.get("url") or next(iter(form.values()), [""]))
                texts.append(v[0])
            else:
                sink = FileSink(app.tmp_dir)
                try:
                    while True:
                        d = reader.read()
                        if not d:
                            break
                        sink.write(d)
                except Exception:
                    sink.close()
                    Path(sink.path).unlink(missing_ok=True)
                    raise
                sink.close()
                text = self.as_text(sink, ctype, urllib.parse.unquote(fname))
                if text is not None:
                    Path(sink.path).unlink(missing_ok=True)
                    texts.append(text)
                elif sink.size == 0:
                    Path(sink.path).unlink(missing_ok=True)
                else:
                    files.append(app.finalize_upload(sink, fname, ctype, device))

            items = []
            if files:
                app.store.add_many(files)
                app.put_files_on_clipboard(files, batch, device)
                items += files
                for f in files:
                    log(f"received {f['kind']} from {device}: {f['name']} ({human_size(f['size'] or 0)})")
                for t in texts:  # a caption alongside files: keep in history, don't clobber clipboard
                    if t.strip():
                        items.append(app.store.add(app._item("phone", text_kind(t), device, text=t)))
            else:
                for t in texts:
                    it = app.receive_text(t, device)
                    if it:
                        items.append(it)
            if not items:
                return self.error(400, "Nothing to send (empty body)")
            if self.q("reply") == "text" or "Shortcuts" in self.headers.get("User-Agent", ""):
                return self.send_text("Sent to Mac")
            return self.send_json({"ok": True, "items": [app.public(i) for i in items]})

        @staticmethod
        def as_text(sink: FileSink, ctype: str, fname: str) -> str | None:
            """Decide whether a raw upload is really just text (e.g. from iOS Shortcuts)."""
            if sink.size == 0 or sink.size > MAX_TEXT:
                return None
            ext = os.path.splitext(fname)[1].lower() if fname else ""
            head = sink.head.lstrip().lower()
            octet = ctype in ("application/octet-stream", "binary/octet-stream", "")
            if ctype in RICH_TEXT_TYPES or (octet and not ext and head.startswith((b"{\\rtf", b"<!doctype html", b"<html", b"<meta"))):
                if ext and ext not in (".txt",):
                    return None  # a real .rtf/.html document: keep it as a file
                fmt = "html" if ("html" in ctype or head.startswith((b"<!doctype", b"<html", b"<meta"))) else "rtf"
                return rich_to_text(sink.path, fmt)
            if ctype.startswith("text/"):
                if ext not in ("", ".txt") and ctype != "text/uri-list":
                    return None
            elif ctype in ("application/octet-stream", "binary/octet-stream", ""):
                # Shortcuts may label text "Text" with no extension: still text.
                if os.path.splitext(fname)[1] or sniff(sink.head):
                    return None
            else:
                return None
            data = Path(sink.path).read_bytes()
            if b"\x00" in data:
                return None
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if ctype == "text/uri-list":
                lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
                text = "\n".join(lines)
            return text

        # ---- send an item to the client --------------------------------------
        def serve_item(self, item: dict, inline: bool):
            if "text" in item:
                return self.send_text(item["text"])
            path = item.get("path")
            if not path or not os.path.exists(path):
                return self.error(410, "The file no longer exists on the Mac")
            name, mime = item.get("name") or os.path.basename(path), item.get("mime") or "application/octet-stream"
            if os.path.isdir(path):
                path, name, mime = self.zip_folder(item), name + ".zip", "application/zip"
            self.send_file(path, mime, name, inline)

        def zip_folder(self, item: dict) -> str:
            zdir = app.store.files_dir / "_zips"
            zdir.mkdir(exist_ok=True)
            base = zdir / item["id"]
            if not base.with_suffix(".zip").exists():
                src = Path(item["path"])
                shutil.make_archive(str(base), "zip", root_dir=src.parent, base_dir=src.name)
            return str(base.with_suffix(".zip"))

        def send_file(self, path: str, mime: str, name: str, inline: bool):
            size = os.path.getsize(path)
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range")
            if rng and size:
                m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:
                        start = max(0, size - int(m.group(2)))
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
            length = max(0, end - start + 1)
            ascii_name = name.encode("ascii", "ignore").decode().replace('"', "") or "file"
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("Content-Disposition",
                             f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; "
                             f"filename*=UTF-8''{urllib.parse.quote(name)}")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    data = f.read(min(CHUNK, left))
                    if not data:
                        break
                    self.wfile.write(data)
                    left -= len(data)

    return Handler


def pair_page(app: App) -> str:
    urls = app.base_urls()
    token = app.token
    links = [f"{u}/?k={token}" for u in urls]
    data = json.dumps({"links": links, "bases": urls, "token": token, "inbox": str(app.inbox)})
    return PAIR_HTML.replace("__DATA__", data.replace("</", "<\\/"))


PAIR_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair Phone2Mac</title>
<link rel="icon" href="/icon.png">
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>
<style>
:root{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--accent:#5b4be1;--code:#f2f2f6}
@media (prefers-color-scheme:dark){:root{--bg:#111114;--card:#1c1c20;--fg:#f5f5f7;--muted:#a1a1a6;--line:#2c2c31;--accent:#8b7dff;--code:#26262b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:28px;margin:0 0 4px}h2{font-size:17px;margin:0 0 12px}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin-top:18px}
.pair{display:grid;grid-template-columns:260px 1fr;gap:24px;align-items:center}
@media (max-width:640px){.pair{grid-template-columns:1fr}}
#qr{background:#fff;border-radius:12px;padding:10px;width:260px;height:260px;display:flex;align-items:center;justify-content:center}
#qr svg{width:100%;height:100%}
code,.code{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);border-radius:6px;padding:2px 6px;word-break:break-all}
.row{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}.row .code{flex:1;padding:8px 10px;min-width:0}
button{font:inherit;border:0;border-radius:8px;padding:7px 12px;background:var(--accent);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
select{font:inherit;padding:6px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
ol{padding-left:20px}li{margin:6px 0}
</style></head><body><main>
<h1>Pair your phone</h1>
<div class="muted">Phone and Mac need to be on the same Wi-Fi network. Keep this server running and it stays paired.</div>

<div class="card pair">
  <div id="qr">Loading QR code…</div>
  <div>
    <h2>1. Scan with your phone's camera</h2>
    <div class="muted">Then tap <b>Share → Add to Home Screen</b> so it's one tap away. If the QR link doesn't open, try the other address:</div>
    <div class="row"><select id="which"></select></div>
    <div class="row"><span class="code" id="link"></span><button data-copy="link">Copy</button></div>
    <div class="muted" style="font-size:13px">Files you send from the phone are saved in <code id="inbox"></code></div>
  </div>
</div>

<div class="card">
  <h2>2. iPhone: add the one-tap Shortcuts (recommended)</h2>
  <div class="muted">These let you send and fetch the clipboard without opening anything, by tapping the back of your phone. They come pre-filled with your address and key, so there's nothing to type. “Share to Mac” is an optional extra for the Share Sheet (several photos at once, etc.).</div>
  <h3 style="font-size:15px;margin:18px 0 6px">Option A: from this Mac (needs Shortcuts iCloud sync)</h3>
  <div class="row">
    <button id="openSend">Add “Send to Mac”</button>
    <button id="openGet">Add “Get from Mac”</button>
    <button id="openShare" class="ghost">Optional: “Share to Mac”</button>
    <span class="muted" id="openMsg" style="font-size:13px"></span>
  </div>
  <div class="muted" style="font-size:13px">The Shortcuts app opens. Click <b>Add Shortcut</b> for each one, and they appear on your iPhone within a minute (if Settings → [your name] → iCloud → Shortcuts is on).</div>
  <h3 style="font-size:15px;margin:18px 0 6px">Option B: on the iPhone</h3>
  <div class="muted">Open the Phone2Mac page on the iPhone (the one you paired above). At the bottom, tap <b>Add “Send to Mac”</b> and then <b>Add “Get from Mac”</b>. If Safari asks, tap <b>Download</b>. Then open the download (the ⬇︎ icon in the address bar) and tap <b>Add Shortcut</b>.</div>
  <h3 style="font-size:15px;margin:18px 0 6px">Then: Back Tap</h3>
  <div class="muted">On the iPhone go to <b>Settings → Accessibility → Touch → Back Tap</b> (at the very bottom). Set <b>Double Tap</b> to “Get from Mac” and <b>Triple Tap</b> to “Send to Mac” (they're listed under <i>Shortcuts</i>, scroll down). The first time each one runs, iPhone asks to allow connecting to devices on your local network. Tap <b>Allow</b>.</div>
  <details style="margin-top:16px"><summary class="muted" style="cursor:pointer">Build them by hand instead</summary>
  <div class="muted" style="font-size:14px;margin-top:8px">Menu names move a little between iOS versions. If something's named differently, look for the closest match.</div>
  <h3 style="font-size:15px;margin:14px 0 6px">“Get from Mac” (the simple one)</h3>
  <ol>
    <li>Shortcuts app → <b>+</b> (top right). Tap the name at the top and rename it “Get from Mac”.</li>
    <li>In the search bar at the bottom type <b>Get Contents of URL</b> and tap it. Tap the blue <b>URL</b> placeholder and paste:</li>
  </ol>
  <div class="row"><span class="code" id="geturl"></span><button data-copy="geturl">Copy</button></div>
  <ol start="3">
    <li>Search for <b>Copy to Clipboard</b> and add it. It uses “Contents of URL” automatically. Tap <b>Done</b>.</li>
  </ol>
  <h3 style="font-size:15px;margin:14px 0 6px">“Send to Mac”</h3>
  <ol>
    <li>New shortcut, named “Send to Mac”. Add <b>Get Clipboard</b>.</li>
    <li>Add <b>Get Contents of URL</b>, paste this URL:</li>
  </ol>
  <div class="row"><span class="code" id="sendurl"></span><button data-copy="sendurl">Copy</button></div>
  <ol start="3">
    <li>Tap the <b>›</b> arrow on that action to expand it. Method: <b>POST</b>. Request Body: <b>File</b>. Tap the File field and choose <b>Clipboard</b>.</li>
    <li>Tap <b>Done</b>. (This hand-built version sends whatever is on your clipboard. The ready-made one above also works from the Share Sheet.)</li>
  </ol>
  </details>
</div>

<div class="card">
  <h2>Pairing key</h2>
  <div class="row"><span class="code" id="token"></span><button data-copy="token">Copy</button></div>
  <div class="muted" style="font-size:13px">Anything without this key is refused. To revoke all paired devices run <code>python3 phone2mac.py --reset-token</code> and restart.</div>
</div>
</main>
<script>
const D = __DATA__;
const sel = document.getElementById('which');
D.links.forEach((l, i) => { const o = document.createElement('option'); o.value = i; o.textContent = D.bases[i].replace('http://', '') + (i === 0 ? ' (recommended, survives IP changes)' : ''); sel.appendChild(o); });
function render() {
  const i = +sel.value || 0, link = D.links[i], base = D.bases[i];
  document.getElementById('link').textContent = link;
  document.getElementById('sendurl').textContent = base + '/api/send?k=' + D.token;
  document.getElementById('geturl').textContent = base + '/api/latest?k=' + D.token;
  const box = document.getElementById('qr');
  if (window.qrcode) { const q = qrcode(0, 'M'); q.addData(link); q.make(); box.innerHTML = q.createSvgTag({cellSize: 6, margin: 2, scalable: true}); }
  else box.textContent = 'QR code needs internet on the Mac. Type the link below into your phone instead.';
}
sel.onchange = render;
document.getElementById('token').textContent = D.token;
document.getElementById('inbox').textContent = D.inbox;
document.querySelectorAll('[data-copy]').forEach(b => b.onclick = () => {
  navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent);
  b.textContent = 'Copied'; setTimeout(() => b.textContent = 'Copy', 1200);
});
function openShortcut(kind) {
  const m = document.getElementById('openMsg');
  m.textContent = 'Preparing… (signing with Apple can take ~10s)';
  fetch('/api/shortcut/' + kind + '/open?k=' + encodeURIComponent(D.token), {method: 'POST'})
    .then(r => r.json()).then(d => { m.textContent = d.ok ? 'Opened in Shortcuts, click “Add Shortcut”.' : d.error; })
    .catch(e => { m.textContent = String(e); });
}
document.getElementById('openSend').onclick = () => openShortcut('send');
document.getElementById('openGet').onclick = () => openShortcut('get');
document.getElementById('openShare').onclick = () => openShortcut('share');
window.addEventListener('load', render); render();
</script></body></html>
"""


class DualStackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(app: App, host: str) -> ThreadingHTTPServer:
    handler = make_handler(app)
    if host in ("", "::", "0.0.0.0") and socket.has_ipv6:
        try:
            class V6(DualStackServer):
                address_family = socket.AF_INET6

                def server_bind(self):
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    super().server_bind()
            return V6(("::", app.port), handler)
        except OSError:
            pass
    return DualStackServer((host or "0.0.0.0", app.port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Phone2Mac: reliable phone <-> Mac clipboard and file sharing")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PHONE2MAC_PORT", DEFAULT_PORT)))
    ap.add_argument("--host", default="", help="interface to listen on (default: all)")
    ap.add_argument("--data-dir", type=Path, default=Path(os.environ.get("PHONE2MAC_DATA", "~/.phone2mac")).expanduser())
    ap.add_argument("--inbox", type=Path, default=Path(os.environ.get("PHONE2MAC_INBOX", "~/Phone2Mac")).expanduser(),
                    help="where files from the phone are saved (default ~/Phone2Mac)")
    ap.add_argument("--no-notify", action="store_true", help="no macOS notifications")
    ap.add_argument("--memory-clipboard", action="store_true", help="don't touch the real clipboard (testing)")
    ap.add_argument("--reset-token", action="store_true", help="unpair all devices (new pairing key)")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    use_mac = sys.platform == "darwin" and not args.memory_clipboard
    clip = MacClipboard(args.data_dir, notifications=not args.no_notify) if use_mac else MemoryClipboard()
    app = App(args.data_dir, args.inbox, args.port, clip, verbose=args.verbose)
    if args.reset_token:
        app.reset_token()
        print("New pairing key created. Re-pair your phone at http://localhost:%d/pair" % args.port)
        return
    if not use_mac:
        log("NOTE: not on macOS (or --memory-clipboard): using an in-memory clipboard")

    try:
        server = make_server(app, args.host)
    except OSError as e:
        log(f"cannot listen on port {args.port}: {e}. Is Phone2Mac already running?")
        sys.exit(1)
    clip.start_watcher(app.on_clip_event)
    log(f"Phone2Mac {VERSION} running. Pair your phone: http://localhost:{args.port}/pair")
    for u in app.base_urls():
        log("  reachable at", u)
    log("  files from your phone go to", app.inbox)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
