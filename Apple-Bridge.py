import os
import io
import re
import json
import hashlib
import subprocess
import tempfile
from collections import OrderedDict
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import asyncio
import paramiko
import time
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAVE_DND = True
except Exception:
    HAVE_DND = False

try:
    from PIL import Image, ImageOps, ImageTk
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

# Paths and constants
BIN_DIR = r"E:\STORAGE\PROGRAMAS\PORTABLES\apple-bridge\libimobile-suite-latest_w64"
IDEVICE_ID = os.path.join(BIN_DIR, "idevice_id.exe")
REMOTE_DIR = "/var/mobile/Media/DCIM/100APPLE"
UPLOAD_DIR = "/var/mobile/Media/DCIM/100APPLE"
PHOTODATA_DIR = "/var/mobile/Media/PhotoData"
SSH_PORT = 2222
PREVIEW_SIZE = (400, 400)
PREVIEW_CACHE_MAX = 30
IMG_PREVIEW_LIMIT = 100 * 1024 * 1024
VID_PREVIEW_LIMIT = 400 * 1024 * 1024
QUICK_HASH_BLOCK = 1024 * 1024

PHOTO_EXTS = ('.jpg', '.jpeg', '.png', '.heic', '.heif', '.gif', '.bmp',
              '.tif', '.tiff', '.webp')
VIDEO_EXTS = ('.mp4', '.mov', '.m4v', '.avi', '.mkv', '.3gp',
              '.mpg', '.mpeg', '.wmv', '.flv', '.webm', '.ts', '.m2ts')
ALL_EXTS = PHOTO_EXTS + VIDEO_EXTS

IOS6_VIDEO_CODECS = ('h264',)
IOS6_AUDIO_CODECS = ('aac', 'mp3')
IOS6_CONTAINERS = ('.mp4', '.mov', '.m4v')

try:
    RESAMPLE = Image.Resampling.LANCZOS
except Exception:
    RESAMPLE = Image.LANCZOS


def _app_data_dir():
    try:
        base = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        base = os.getcwd()
    d = os.path.join(base, 'Data')
    os.makedirs(d, exist_ok=True)
    return d

INDEX_PATH = os.path.join(_app_data_dir(), 'upload_index.json')


# --------------------------------------------------------------------------- helpers
def clean_drop_path(p):
    p = p.strip().strip('"').strip("'")
    if p.startswith('file://'):
        from urllib.parse import unquote, urlparse
        p = unquote(urlparse(p).path)
        if os.name == 'nt' and len(p) > 2 and p[0] == '/' and p[2] == ':':
            p = p[1:]
    return p


_WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$', re.I)


def _safe_str(s):
    try:
        return str(s).encode('utf-8', 'replace').decode('utf-8')
    except Exception:
        return repr(s)


def validate_remote_name(name):
    if not name or name in ('.', '..'):
        return False, "empty/reserved"
    if '/' in name or '\x00' in name:
        return False, "contains '/' or NUL"
    try:
        name.encode('utf-8')
    except UnicodeEncodeError:
        return False, "invalid UTF-8 (surrogates)"
    return True, "OK"


def safe_local_name(name, max_len=180):
    if not name or name in ('.', '..'):
        return "_unnamed_"
    name = _safe_str(name)
    name = _WIN_ILLEGAL.sub('_', name)
    name = name.rstrip(' .') or '_'
    stem, ext = os.path.splitext(name)
    if _WIN_RESERVED.match(stem):
        stem = '_' + stem
    ext = ext[:16]
    if len(stem) + len(ext) > max_len:
        stem = stem[:max_len - len(ext)]
    return stem + ext


def unique_local_path(out_dir, name):
    base, ext = os.path.splitext(name)
    candidate = name
    n = 1
    while os.path.exists(os.path.join(out_dir, candidate)):
        n += 1
        candidate = f"{base}_{n}{ext}"
    return os.path.join(out_dir, candidate)


def _parse_drop(data, tk_root):
    try:
        return list(tk_root.tk.splitlist(data))
    except Exception:
        out, buf, in_brace = [], '', False
        for ch in data:
            if ch == '{':
                in_brace = True; continue
            if ch == '}':
                in_brace = False; continue
            if ch == ' ' and not in_brace:
                if buf:
                    out.append(buf); buf = ''
                continue
            buf += ch
        if buf:
            out.append(buf)
        return out or [data]


def quick_hash(path, block=QUICK_HASH_BLOCK):
    """Fast identity hash: SHA256 of (size + first block + last block)."""
    size = os.path.getsize(path)
    h = hashlib.sha256()
    h.update(str(size).encode('ascii'))
    with open(path, 'rb') as f:
        if size <= block * 2:
            h.update(f.read())
        else:
            h.update(f.read(block))
            f.seek(-block, 2)
            h.update(f.read(block))
    return h.hexdigest()


class UploadIndex:
    """Persistent map: content-hash -> remote filename (JSON on disk)."""
    def __init__(self, path):
        self.path = path
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                d = json.load(f)
                if isinstance(d, dict) and isinstance(d.get('by_hash'), dict):
                    return d
        except Exception:
            pass
        return {'by_hash': {}}

    def save(self):
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try: os.unlink(tmp)
            except Exception: pass

    def lookup(self, content_hash, remote_set):
        rec = self.data['by_hash'].get(content_hash)
        if not rec:
            return None
        rn = rec.get('remote_name')
        return rn if rn and rn in remote_set else None

    def add(self, content_hash, remote_name, size, reencoded):
        self.data['by_hash'][content_hash] = {
            'remote_name': remote_name,
            'size': size,
            'reencoded': bool(reencoded),
            'uploaded_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        self.save()


def prepare_image(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.jpg', '.jpeg'):
        with open(path, 'rb') as f:
            return f.read(), '.JPG'
    if ext == '.png':
        with open(path, 'rb') as f:
            return f.read(), '.PNG'
    if not HAVE_PIL:
        raise RuntimeError(f"{ext} not supported by iOS 6 and Pillow is not installed")
    img = Image.open(path).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=92)
    return buf.getvalue(), '.JPG'


def probe_video(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "ffprobe failed").strip()[-300:])
    return json.loads(r.stdout)


def check_playable(probe, container_ext):
    streams = probe.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if not video:
        return False, "no video stream"
    if container_ext not in IOS6_CONTAINERS:
        return False, f"container '{container_ext}' not supported"
    vcodec = (video.get('codec_name') or '').lower()
    if vcodec not in IOS6_VIDEO_CODECS:
        return False, f"video codec '{vcodec}' not supported"
    profile = (video.get('profile') or '').lower()
    level = int(video.get('level') or 0)
    w = int(video.get('width') or 0)
    h = int(video.get('height') or 0)
    if not any(p in profile for p in ('baseline', 'main', 'high')):
        return False, f"H.264 profile '{profile}' not supported"
    if level > 41:
        return False, f"H.264 level {level/10:.1f} > 4.1"
    if max(w, h) > 1920:
        return False, f"resolution {w}x{h} exceeds 1080p"
    if audio:
        acodec = (audio.get('codec_name') or '').lower()
        if acodec not in IOS6_AUDIO_CODECS:
            return False, f"audio codec '{acodec}' not supported"
    return True, "OK"


def reencode_video(input_path, output_path, log_cb):
    try:
        probe = probe_video(input_path)
    except Exception as e:
        log_cb(f"  probe failed: {e}")
        return False
    duration = 0.0
    try:
        duration = float(probe.get('format', {}).get('duration') or 0)
    except Exception:
        pass
    has_audio = any(s.get('codec_type') == 'audio' for s in probe.get('streams', []))

    cmd = ['ffmpeg', '-y', '-i', input_path,
           '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
           '-profile:v', 'high', '-level', '4.1',
           '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
           '-map', '0:v:0']
    if has_audio:
        cmd += ['-map', '0:a:0', '-c:a', 'aac', '-b:a', '160k', '-ac', '2']
    cmd.append(output_path)

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            universal_newlines=True, bufsize=1)
    last_pct = -1
    for line in proc.stderr:
        if duration > 0:
            m = re.search(r'time=(\d+):(\d+):(\d+\.\d+)', line)
            if m:
                elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                pct = min(100, int(100 * elapsed / duration))
                if pct - last_pct >= 5:
                    log_cb(f"  encoding {pct}%")
                    last_pct = pct
    proc.wait()
    if proc.returncode != 0:
        log_cb(f"  ffmpeg failed (exit {proc.returncode})")
        return False
    try:
        return os.path.getsize(output_path) > 0
    except Exception:
        return False


def extract_video_frame(src_path, thumb_path, size=(400, 400)):
    try:
        probe = probe_video(src_path)
        dur = float(probe.get('format', {}).get('duration') or 0)
    except Exception:
        dur = 0.0
    seek = min(1.0, dur * 0.5) if dur > 0 else 0.0
    for ss in (seek, 0.0):
        subprocess.run(
            ['ffmpeg', '-y', '-ss', str(ss), '-i', src_path,
             '-vframes', '1', '-vf', f'scale={size[0]}:-1', thumb_path],
            capture_output=True, timeout=60
        )
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return True
    return False


# --------------------------------------------------------------------------- app
class MediaLoaderApp:
    def __init__(self, root):
        self.root = root
        root.title("iOS Media Loader (AFC2 + USB SSH + FFmpeg)")
        root.geometry("1150x720")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log_queue = queue.Queue()
        self.forwarder_process = None

        self.preview_cache = OrderedDict()
        self.preview_token = 0
        self.preview_after = None
        self.preview_photo = None
        self.upload_index = UploadIndex(INDEX_PATH)

        self.start_usb_forwarder()

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Device UDID:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(top, textvariable=self.device_var, width=50)
        self.device_combo.pack(side="left", padx=6)
        ttk.Button(top, text="Refresh", command=self.refresh_devices).pack(side="left")
        ttk.Button(top, text="List Files", command=self.list_files).pack(side="left", padx=6)

        mid = ttk.Frame(root, padding=(8, 0, 8, 8))
        mid.pack(fill="both", expand=True)

        preview_frame = ttk.LabelFrame(mid, text="Preview", padding=6)
        preview_frame.pack(side="right", fill="y", padx=(8, 0))
        preview_frame.pack_propagate(False)
        preview_frame.configure(width=PREVIEW_SIZE[0] + 30, height=PREVIEW_SIZE[1] + 70)

        self.preview_label = tk.Label(
            preview_frame, text="Select a file to preview\n(or drop files here to upload)",
            bg="#2b2b2b", fg="#cccccc",
            compound="center", justify="center", wraplength=PREVIEW_SIZE[0])
        self.preview_label.pack(fill="both", expand=True)
        self.preview_caption = ttk.Label(preview_frame, text="", justify="center", anchor="center")
        self.preview_caption.pack(fill="x", pady=(6, 0))

        self.tree = ttk.Treeview(mid, columns=("filename", "type"),
                                 show="headings", selectmode="extended")
        self.tree.heading("filename", text="Filename")
        self.tree.heading("type", text="Type")
        self.tree.column("filename", width=340)
        self.tree.column("type", width=70, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left")

        scrollbar = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        if HAVE_DND:
            for w in (self.tree, self.preview_label):
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', self.on_drop)
            root.title("iOS Media Loader (AFC2 + USB SSH + FFmpeg) - drag & drop enabled")

        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        bottom = ttk.Frame(root, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Download Selected", command=self.download_selected).pack(side="left")
        ttk.Button(bottom, text="Download All", command=self.download_all).pack(side="left", padx=6)
        ttk.Button(bottom, text="Upload Files", command=self.upload_files).pack(side="left", padx=6)
        ttk.Button(bottom, text="Delete Selected", command=self.delete_selected).pack(side="left", padx=6)
        ttk.Button(bottom, text="Reboot", command=self.manual_rebuild).pack(side="left", padx=6)
        ttk.Button(bottom, text="Clear Dedup Index", command=self.clear_dedup_index).pack(side="left", padx=6)

        self.log_text = tk.Text(root, height=8, state='disabled', bg="#1e1e1e", fg="#00ff00")
        self.log_text.pack(fill="x", padx=8, pady=(0, 8))

        self.refresh_devices()
        self._poll_log_queue()

    # ------------------------------------------------------------------ USB / SSH
    def start_usb_forwarder(self):
        try:
            self.forwarder_process = subprocess.Popen(
                ["pymobiledevice3", "usbmux", "forward", str(SSH_PORT), "22"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            time.sleep(1)
            self.log(f"USB SSH Forwarder started on 127.0.0.1:{SSH_PORT}")
        except Exception as e:
            self.log(f"Failed to start USB forwarder: {e}")

    def on_close(self):
        if self.forwarder_process:
            self.forwarder_process.terminate()
        self.root.destroy()

    # ------------------------------------------------------------------ UI helpers
    def select_all(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def log(self, msg):
        self.log_queue.put(_safe_str(msg))

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_text.config(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        self.root.after(100, self._poll_log_queue)

    def refresh_devices(self):
        try:
            p = subprocess.run([IDEVICE_ID, "-l"], capture_output=True, text=True)
            udids = [x.strip() for x in p.stdout.splitlines() if x.strip()]
            self.device_combo["values"] = udids
            if udids:
                self.device_combo.current(0)
                self.log(f"Found {len(udids)} device(s).")
            else:
                self.log("No devices found. Check USB and Trust prompt.")
        except Exception as e:
            self.log(f"Error refreshing devices: {e}")

    def get_udid(self):
        udid = self.device_var.get().strip()
        if not udid:
            messagebox.showerror("Error", "No device selected.")
            raise ValueError("No UDID")
        return udid

    # ------------------------------------------------------------------ Drag & drop
    def on_drop(self, event):
        raw = _parse_drop(event.data, self.root)
        files = []
        for p in raw:
            p = clean_drop_path(p)
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in ALL_EXTS:
                files.append(p)
            elif os.path.isdir(p):
                for root_dir, _, names in os.walk(p):
                    for n in names:
                        fp = os.path.join(root_dir, n)
                        if os.path.splitext(n)[1].lower() in ALL_EXTS:
                            files.append(fp)

        if not files:
            self.log("Drop ignored: no supported image/video files.")
            return

        try:
            udid = self.get_udid()
        except ValueError:
            return

        self.log(f"Drop received: {len(files)} file(s) -> uploading...")
        threading.Thread(target=self._upload_thread,
                         args=(udid, files), daemon=True).start()

    # ------------------------------------------------------------------ Listing
    def list_files(self):
        self.tree.delete(*self.tree.get_children())
        self.preview_cache.clear()
        self.preview_token += 1
        self._clear_preview("Select a file to preview\n(or drop files here to upload)")
        try:
            udid = self.get_udid()
            self.log(f"Listing files in {REMOTE_DIR}...")
            threading.Thread(target=self._list_thread, args=(udid,), daemon=True).start()
        except ValueError:
            pass

    def _list_thread(self, udid):
        async def async_list():
            photos = videos = skipped = 0
            try:
                lockdown = await create_using_usbmux(serial=udid)
                async with AfcService(lockdown=lockdown, service_name='com.apple.afc2') as afc:
                    if not await afc.exists(REMOTE_DIR):
                        self.log(f"Directory not found: {REMOTE_DIR}")
                        return
                    files = await afc.listdir(REMOTE_DIR)
                    for f in files:
                        if f in ('.', '..') or f.startswith('.'):
                            continue
                        ext = os.path.splitext(f)[1].lower()
                        if ext in PHOTO_EXTS:
                            kind = "Image"; photos += 1
                        elif ext in VIDEO_EXTS:
                            kind = "Video"; videos += 1
                        else:
                            continue
                        ok, why = validate_remote_name(f)
                        if not ok:
                            skipped += 1
                            self.log(f"Skipping unreadable name ({why}): {_safe_str(f)!r}")
                            continue
                        safe = _safe_str(f)
                        self.root.after(0, lambda n=safe, k=kind:
                                        self.tree.insert("", "end", values=(n, k)))
            except Exception as e:
                self.log(f"Connection error: {e}")

            if photos + videos == 0:
                self.log(f"No media found in {REMOTE_DIR}.")
            else:
                self.log(f"Loaded {photos} image(s), {videos} video(s).")
            if skipped:
                self.log(f"Skipped {skipped} file(s) with unusable names.")
        asyncio.run(async_list())

    # ------------------------------------------------------------------ Preview
    def on_select(self, event=None):
        if self.preview_after is not None:
            self.root.after_cancel(self.preview_after)
            self.preview_after = None

        sel = self.tree.selection()
        if len(sel) != 1:
            self.preview_token += 1
            if len(sel) > 1:
                self._clear_preview(f"{len(sel)} files selected\n(select one to preview)")
            else:
                self._clear_preview("Select a file to preview\n(or drop files here to upload)")
            return

        name = self.tree.item(sel[0], "values")[0]
        self.preview_after = self.root.after(250, lambda: self._start_preview(name))

    def _start_preview(self, name):
        self.preview_after = None
        if not HAVE_PIL:
            self._clear_preview("Preview needs Pillow:\npip install pillow pillow-heif")
            return
        if name in self.preview_cache:
            self.preview_cache.move_to_end(name)
            img, info = self.preview_cache[name]
            self._show_preview(img, f"{name}\n{info}")
            return
        try:
            udid = self.get_udid()
        except ValueError:
            return
        self.preview_token += 1
        token = self.preview_token
        self._clear_preview(f"Loading {name}...")
        threading.Thread(target=self._preview_thread,
                         args=(udid, name, token), daemon=True).start()

    def _preview_thread(self, udid, name, token):
        ok, why = validate_remote_name(name)
        if not ok:
            self.root.after(0, lambda: self._preview_failed(
                name, token, f"invalid name ({why})"))
            return

        ext = os.path.splitext(name)[1].lower()
        is_video = ext in VIDEO_EXTS
        limit = VID_PREVIEW_LIMIT if is_video else IMG_PREVIEW_LIMIT

        async def fetch():
            lockdown = await create_using_usbmux(serial=udid)
            async with AfcService(lockdown=lockdown, service_name='com.apple.afc2') as afc:
                size_hint = None
                try:
                    info = await afc.stat(f"{REMOTE_DIR}/{name}")
                    for k in ('st_size', 'StSize', 'size'):
                        if k in info:
                            size_hint = int(info[k]); break
                except Exception:
                    pass
                if size_hint and size_hint > limit:
                    return None, size_hint
                data = await afc.get_file_contents(f"{REMOTE_DIR}/{name}")
                return data, size_hint or len(data)

        try:
            data, size = asyncio.run(fetch())
            size_mb = size / (1024 * 1024)
            if data is None:
                self.root.after(0, lambda: self._preview_failed(
                    name, token, f"Too large for preview ({size_mb:.0f} MB)"))
                return

            if is_video:
                img, info = self._make_video_preview(data, name, size_mb)
            else:
                img, info = self._make_image_preview(data, size_mb)
            self.root.after(0, lambda: self._preview_done(name, token, img, info))
        except Exception as e:
            msg = _safe_str(e)
            self.root.after(0, lambda: self._preview_failed(name, token, msg))

    def _make_image_preview(self, data, size_mb):
        img = Image.open(io.BytesIO(data))
        orig_w, orig_h = img.size
        try:
            img.draft('RGB', (PREVIEW_SIZE[0] * 2, PREVIEW_SIZE[1] * 2))
        except Exception:
            pass
        img = ImageOps.exif_transpose(img)
        img = img.convert('RGB')
        img.thumbnail(PREVIEW_SIZE, RESAMPLE)
        return img, f"{orig_w}x{orig_h} px, {size_mb:.1f} MB"

    def _make_video_preview(self, data, name, size_mb):
        ext = os.path.splitext(name)[1].lower()
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.write(data); tmp.close()
        frame_path = None
        try:
            try:
                probe = probe_video(tmp.name)
            except Exception:
                probe = {}
            duration = 0.0
            try:
                duration = float(probe.get('format', {}).get('duration') or 0)
            except Exception:
                pass
            vstream = next((s for s in probe.get('streams', [])
                            if s.get('codec_type') == 'video'), None)
            res = f"{vstream.get('width',0)}x{vstream.get('height',0)}" if vstream else "?"
            vcodec = (vstream.get('codec_name', '?') if vstream else '?')

            f = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
            frame_path = f.name; f.close()
            if not extract_video_frame(tmp.name, frame_path, PREVIEW_SIZE):
                raise RuntimeError("frame extraction failed")
            img = Image.open(frame_path).convert('RGB')
            img.load()
            info = f"{size_mb:.1f} MB, {int(duration)}s, {res}, {vcodec}"
            return img, info
        finally:
            try: os.unlink(tmp.name)
            except Exception: pass
            if frame_path:
                try: os.unlink(frame_path)
                except Exception: pass

    def _preview_done(self, name, token, img, info):
        if token != self.preview_token:
            return
        self.preview_cache[name] = (img, info)
        while len(self.preview_cache) > PREVIEW_CACHE_MAX:
            self.preview_cache.popitem(last=False)
        self._show_preview(img, f"{name}\n{info}")

    def _preview_failed(self, name, token, msg):
        if token != self.preview_token:
            return
        self._clear_preview(f"No preview available\nfor {name}\n({msg})")
        self.log(f"Preview failed for {name}: {msg}")

    def _show_preview(self, img, caption):
        self.preview_photo = ImageTk.PhotoImage(img)
        self.preview_label.config(image=self.preview_photo, text="")
        self.preview_caption.config(text=caption)

    def _clear_preview(self, text):
        self.preview_photo = None
        self.preview_label.config(image="", text=text)
        self.preview_caption.config(text="")

    # ------------------------------------------------------------------ Download
    def download_files(self, filenames):
        if not filenames:
            messagebox.showinfo("Download", "No files selected.")
            return
        out_dir = filedialog.askdirectory(title="Select Download Folder")
        if not out_dir:
            return
        try:
            udid = self.get_udid()
            self.log(f"Starting download of {len(filenames)} file(s)...")
            threading.Thread(target=self._download_thread,
                             args=(udid, filenames, out_dir), daemon=True).start()
        except ValueError:
            pass

    def _download_thread(self, udid, filenames, out_dir):
        async def async_download():
            success = 0
            try:
                lockdown = await create_using_usbmux(serial=udid)
                async with AfcService(lockdown=lockdown, service_name='com.apple.afc2') as afc:
                    for i, filename in enumerate(filenames, 1):
                        remote_path = f"{REMOTE_DIR}/{filename}"
                        ok, why = validate_remote_name(filename)
                        if not ok:
                            self.log(f"[{i}/{len(filenames)}] Skipped invalid name "
                                     f"({why}): {_safe_str(filename)!r}")
                            continue
                        try:
                            data = await afc.get_file_contents(remote_path)
                            local_name = safe_local_name(filename)
                            local_path = unique_local_path(out_dir, local_name)
                            with open(local_path, 'wb') as f:
                                f.write(data)
                            note = "" if local_name == filename else f" (saved as {local_name})"
                            self.log(f"[{i}/{len(filenames)}] Downloaded: {_safe_str(filename)} "
                                     f"({len(data)//1024//1024} MB){note}")
                            success += 1
                        except Exception as e:
                            self.log(f"[{i}/{len(filenames)}] Failed: {_safe_str(filename)} | "
                                     f"{_safe_str(e)}")
            except Exception as e:
                self.log(f"Connection error: {_safe_str(e)}")
            self.log(f"--- Finished. Downloaded {success}/{len(filenames)} file(s). ---")
        asyncio.run(async_download())

    def download_selected(self):
        self.download_files([self.tree.item(i, "values")[0] for i in self.tree.selection()])

    def download_all(self):
        self.download_files([self.tree.item(i, "values")[0] for i in self.tree.get_children()])

    # ------------------------------------------------------------------ Delete
    def delete_selected(self):
        selected = [self.tree.item(i, "values")[0] for i in self.tree.selection()]
        if not selected:
            messagebox.showinfo("Delete", "No files selected.")
            return
        if not messagebox.askyesno("Confirm Delete",
                                   f"Delete {len(selected)} file(s) from the device?"):
            return
        try:
            udid = self.get_udid()
            self.log(f"Starting deletion of {len(selected)} file(s)...")
            threading.Thread(target=self._delete_thread,
                             args=(udid, selected), daemon=True).start()
        except ValueError:
            pass

    def _delete_thread(self, udid, filenames):
        async def async_delete():
            success = 0
            try:
                lockdown = await create_using_usbmux(serial=udid)
                async with AfcService(lockdown=lockdown, service_name='com.apple.afc2') as afc:
                    for i, filename in enumerate(filenames, 1):
                        ok, why = validate_remote_name(filename)
                        if not ok:
                            self.log(f"[{i}/{len(filenames)}] Skipped invalid name "
                                     f"({why}): {_safe_str(filename)!r}")
                            continue
                        try:
                            await afc.rm(f"{REMOTE_DIR}/{filename}")
                            self.log(f"[{i}/{len(filenames)}] Deleted: {_safe_str(filename)}")
                            success += 1
                        except Exception as e:
                            self.log(f"[{i}/{len(filenames)}] Failed: {_safe_str(filename)} | "
                                     f"{_safe_str(e)}")
            except Exception as e:
                self.log(f"Connection error: {_safe_str(e)}")
            self.log(f"--- Finished. Deleted {success}/{len(filenames)} file(s). ---")
            self.root.after(0, lambda: messagebox.showinfo(
                "Delete Complete",
                f"Deleted {success} file(s).\n\nPress 'Reboot' to update the Photos database."))
            if success > 0:
                self.root.after(0, self.list_files)
        asyncio.run(async_delete())

    # ------------------------------------------------------------------ Upload
    def upload_files(self):
        file_paths = filedialog.askopenfilenames(
            title="Select Files to Upload",
            filetypes=[("Media", " ".join("*" + e for e in ALL_EXTS)),
                       ("Images", " ".join("*" + e for e in PHOTO_EXTS)),
                       ("Videos", " ".join("*" + e for e in VIDEO_EXTS))])
        if not file_paths:
            return
        try:
            udid = self.get_udid()
            self.log(f"Starting upload of {len(file_paths)} file(s)...")
            threading.Thread(target=self._upload_thread,
                             args=(udid, file_paths), daemon=True).start()
        except ValueError:
            pass

    def _upload_thread(self, udid, file_paths):
        async def async_upload():
            success = 0
            skipped_dupes = 0
            afc_ctx = None
            afc = None

            async def open_afc():
                nonlocal afc_ctx, afc
                lockdown = await create_using_usbmux(serial=udid)
                afc_ctx = AfcService(lockdown=lockdown, service_name='com.apple.afc2')
                afc = await afc_ctx.__aenter__()
                return afc

            async def close_afc():
                nonlocal afc_ctx, afc
                if afc_ctx:
                    try: await afc_ctx.__aexit__(None, None, None)
                    except Exception: pass
                afc_ctx = None; afc = None

            try:
                a = await open_afc()
                if not await a.exists(UPLOAD_DIR):
                    await a.makedirs(UPLOAD_DIR)

                existing = await a.listdir(UPLOAD_DIR)
                remote_set = set(existing)
                nums = [int(m.group(1)) for n in existing
                        for m in [re.match(r"IMG_(\d+)\.", n, re.I)] if m]
                counter = max(nums, default=0)
                batch_seen = {}

                for i, local_path in enumerate(file_paths, 1):
                    base = os.path.basename(local_path)
                    ext = os.path.splitext(local_path)[1].lower()

                    try:
                        src_hash = quick_hash(local_path)
                    except Exception as e:
                        self.log(f"[{i}/{len(file_paths)}] Hash failed "
                                 f"{_safe_str(base)}: {_safe_str(e)}")
                        continue

                    prev = self.upload_index.lookup(src_hash, remote_set)
                    if prev:
                        self.log(f"[{i}/{len(file_paths)}] Skipped (already on device "
                                 f"as {prev}): {_safe_str(base)}")
                        skipped_dupes += 1
                        continue

                    if src_hash in batch_seen:
                        self.log(f"[{i}/{len(file_paths)}] Skipped (duplicate in batch of "
                                 f"{batch_seen[src_hash]}): {_safe_str(base)}")
                        skipped_dupes += 1
                        continue

                    data = None
                    new_ext = None
                    reencoded = False
                    try:
                        if ext in PHOTO_EXTS:
                            data, new_ext = prepare_image(local_path)
                            self.log(f"[{i}/{len(file_paths)}] {_safe_str(base)}: image ready "
                                     f"({len(data)//1024} KB)")
                        elif ext in VIDEO_EXTS:
                            try:
                                probe = probe_video(local_path)
                            except Exception as e:
                                self.log(f"[{i}/{len(file_paths)}] Probe failed "
                                         f"{_safe_str(base)}: {_safe_str(e)}")
                                continue
                            playable, reason = check_playable(probe, ext)
                            if playable:
                                with open(local_path, 'rb') as f:
                                    data = f.read()
                                new_ext = '.MOV'
                                self.log(f"[{i}/{len(file_paths)}] {_safe_str(base)}: playable as-is "
                                         f"({len(data)//1024//1024} MB)")
                            else:
                                self.log(f"[{i}/{len(file_paths)}] {_safe_str(base)}: {reason} "
                                         f"-> re-encoding...")
                                await close_afc()
                                tmp = tempfile.NamedTemporaryFile(suffix='.MOV', delete=False)
                                tmp_path = tmp.name; tmp.close()
                                try:
                                    if not reencode_video(local_path, tmp_path, self.log):
                                        raise RuntimeError("re-encode failed")
                                    with open(tmp_path, 'rb') as f:
                                        data = f.read()
                                    new_ext = '.MOV'
                                    reencoded = True
                                    self.log(f"[{i}/{len(file_paths)}] {_safe_str(base)}: re-encoded to "
                                             f"{len(data)//1024//1024} MB")
                                finally:
                                    try: os.unlink(tmp_path)
                                    except Exception: pass
                                a = await open_afc()
                        else:
                            self.log(f"[{i}/{len(file_paths)}] Skipped (unsupported): "
                                     f"{_safe_str(base)}")
                            continue

                        counter += 1
                        new_name = f"IMG_{counter:04d}{new_ext}"
                        await a.set_file_contents(f"{UPLOAD_DIR}/{new_name}", data)
                        self.log(f"[{i}/{len(file_paths)}] Uploaded {_safe_str(base)} as {new_name}")
                        success += 1
                        batch_seen[src_hash] = new_name
                        remote_set.add(new_name)
                        self.upload_index.add(src_hash, new_name, len(data), reencoded)
                    except Exception as e:
                        self.log(f"[{i}/{len(file_paths)}] Failed {_safe_str(base)}: "
                                 f"{_safe_str(e)}")
                        try:
                            await close_afc(); a = await open_afc()
                        except Exception:
                            pass

                self.log(f"--- Finished. Uploaded {success}/{len(file_paths)} file(s), "
                         f"skipped {skipped_dupes} duplicate(s). ---")
            except Exception as e:
                self.log(f"Upload error: {_safe_str(e)}")
            finally:
                await close_afc()

        asyncio.run(async_upload())

    def clear_dedup_index(self):
        if not messagebox.askyesno(
                "Clear Dedup Index",
                "Forget all previously uploaded files?\n"
                "Next upload will re-upload even if already on device."):
            return
        try:
            if os.path.exists(INDEX_PATH):
                os.unlink(INDEX_PATH)
            self.upload_index = UploadIndex(INDEX_PATH)
            self.log("Dedup index cleared.")
        except Exception as e:
            self.log(f"Failed to clear index: {_safe_str(e)}")

    # ------------------------------------------------------------------ SSH & Rebuild
    def ssh_run(self, ssh, cmd, timeout=60):
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="ignore").strip()
        err = stderr.read().decode(errors="ignore").strip()
        return stdout.channel.recv_exit_status(), out, err

    def manual_rebuild(self):
        try:
            self.get_udid()
            self.log("Starting manual DB fix and reboot...")
            threading.Thread(target=self._manual_rebuild_thread, daemon=True).start()
        except ValueError:
            pass

    def _manual_rebuild_thread(self):
        if self.trigger_ssh_rebuild():
            self.log("Success! Database cleared. Device is rebooting.")
            self.root.after(0, lambda: messagebox.showinfo(
                "Reboot", "Device is now rebooting to rebuild the Photos database."))
        else:
            self.log("SSH step failed. Check the [ssh] log lines above.")

    def trigger_ssh_rebuild(self, user="root", pw="alpine"):
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect("127.0.0.1", port=SSH_PORT, username=user, password=pw,
                        timeout=10, look_for_keys=False, allow_agent=False)

            ts = time.strftime("%Y%m%d_%H%M%S")
            pd = PHOTODATA_DIR
            backup = f"/var/mobile/Media/PhotoData_backup_{ts}"

            steps = [
                ("Fix ownership/permissions",
                 "chown -R mobile:mobile /var/mobile/Media/DCIM && "
                 "chmod -R a+rX /var/mobile/Media/DCIM"),
                ("Backup old DB",
                 f"mkdir -p {backup} && "
                 f"cp -R -p {pd}/Photos.sqlite* {backup}/ 2>/dev/null; true"),
                ("Kill daemons + delete DB/caches",
                 "killall -9 MobileSlideShow assetsd 2>/dev/null; "
                 f"rm -rf {pd}/Photos.sqlite* {pd}/PhotosAux.sqlite* "
                 f"{pd}/MISC/DCIM_APPLE.plist {pd}/MISC/PreviewWellImage.tiff "
                 f"{pd}/Thumbnails {pd}/Metadata 2>/dev/null; sync; true"),
            ]
            for label, cmd in steps:
                code, out, err = self.ssh_run(ssh, cmd)
                self.log(f"[ssh] {label}: exit={code} {err}".strip())
                if code != 0:
                    ssh.close(); return False

            try:
                ssh.exec_command("sync; (sleep 1; reboot) >/dev/null 2>&1 &")
                time.sleep(1)
            except Exception:
                pass
            ssh.close()
            return True
        except Exception as e:
            self.log(f"SSH Error: {_safe_str(e)}")
            return False


if __name__ == "__main__":
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    app = MediaLoaderApp(root)
    root.mainloop()
