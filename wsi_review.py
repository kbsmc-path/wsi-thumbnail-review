"""
WSI (Aperio .svs) quick thumbnail reviewer / mover.

- Reads only the small embedded thumbnail page of each .svs (no full-res decode;
  label and macro pages are ignored), so even multi-GB slides show up in ~1 s.
  Thumbnails are cached on disk (<source>/.wsi_thumbs) so later launches are instant.
- Select slides, then move them to a target folder. Every move is logged
  (move_log.csv in the target folder) and can be undone.
- Group matching slides (an H&E and the IHC section it really lines up with) by tagging their
  block field: "S 2024000123,A,,dup1.svs" -> "S 2024000123,A_1,,dup1.svs". Sorting by name then
  keeps each pair together. Logged to rename_log.csv and undoable with Ctrl+Z.

Requirements: Python 3.8+ with tkinter, and Pillow (python -m pip install -r requirements.txt).
    i18n.py must sit in the same folder. A missing piece is reported with install instructions.

Usage:
    python wsi_review.py [source_folder] [target_folder]
    python wsi_review.py --check [source_folder]     # print which TIFF page is used per file (no GUI)

Mouse:
    Click           focus (preview only, no selection)
    Ctrl + Click    toggle selection
    Shift + Click   select range from focused slide to clicked slide
    Wheel on preview   zoom in / out      Drag on preview   pan
    Double-click preview   reset zoom
Keys:
    Arrows          move focus              Space           toggle selection of focused
    Enter / M       move selected files     Ctrl+Z          undo last move
    P               pin the focused slide as a reference (press P on it again to release)
    Shift+P         release the pinned slide from anywhere (or the Unpin button)
    Ctrl+A          select all              Esc             clear selection
    + / -           thumbnail size step     F5              rescan folder
    Home / End      first / last slide      PageUp / PageDown
    Ctrl+F          jump to the filename filter box
    (toolbar)       Language: 한국어 / English - the window rebuilds itself, nothing is lost
    (toolbar)       saturation / gamma sliders brighten faint slides - display only,
                    the cached thumbnails and the slide files are never changed
    0               reset preview zoom
    G               group the selected files (block rename, e.g. A -> A_1)

Log: everything (scan, extraction, errors with reason, moves, undo) is printed to the console
     and appended to wsi_review.log next to this script.
"""
import csv
import functools
import hashlib
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

MIN_PYTHON = (3, 8)


def _missing(problem, fix):
    """A requirement is missing: say what and how to install it (console + message box), then exit.

    The message box matters when the .py was started by double-click - the console closes at once.
    """
    msg = f"{problem}\n\n{fix}"
    print("\n[!] " + msg.replace("\n", "\n    ") + "\n", file=sys.stderr, flush=True)
    try:
        import tkinter
        from tkinter import messagebox as mb
        root = tkinter.Tk()
        root.withdraw()
        mb.showerror("WSI Thumbnail Review", msg)
        root.destroy()
    except Exception:  # noqa - no tkinter either: the console text has to do
        pass
    sys.exit(1)


if sys.version_info < MIN_PYTHON:
    _missing(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required (this is {sys.version.split()[0]}).\n"
             f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상이 필요합니다.",
             "https://www.python.org/downloads/  (Windows: tick 'Add python.exe to PATH')")
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as e:
    _missing(f"tkinter (Python's GUI library) is not available: {e}\n"
             "tkinter(파이썬 GUI 모듈)가 없습니다.",
             "Windows / macOS: install Python from https://www.python.org/downloads/ (tkinter is included)\n"
             "Ubuntu / Debian:  sudo apt install python3-tk\n"
             "Fedora:           sudo dnf install python3-tkinter\n"
             "macOS Homebrew:   brew install python-tk")
try:
    from PIL import Image, ImageEnhance, ImageTk
except ImportError as e:
    _missing(f"The Pillow package is missing: {e}\n"
             "Pillow 패키지가 설치되어 있지 않습니다.",
             "Install it from this folder, then run again / 이 폴더에서 설치 후 다시 실행하세요:\n"
             "    python -m pip install -r requirements.txt\n"
             "(or: python -m pip install pillow;  Linux ImageTk error: sudo apt install python3-pil.imagetk)")
try:
    import i18n
    from i18n import t
except ImportError:
    _missing("i18n.py was not found next to wsi_review.py.\n"
             "wsi_review.py 와 같은 폴더에 i18n.py 가 없습니다.",
             "Download the whole repository (GitHub: Code > Download ZIP) and keep all files together.\n"
             "저장소 전체를 받아서(GitHub: Code > Download ZIP) 파일들을 한 폴더에 두세요.")

Image.MAX_IMAGE_PIXELS = None

SIZES = [("XS", 140), ("S", 200), ("M", 270), ("L", 360), ("XL", 480), ("XXL", 640)]

# label -> (key function over Slide, reverse?)
# (i18n key, sort key function, reverse?) - the combobox value is the KEY, so the choice
# survives a language switch.
SORTS = [
    ("sort_name", lambda s: natural_key(s.name), False),
    ("sort_name_desc", lambda s: natural_key(s.name), True),
    ("sort_mtime_asc", lambda s: s.mtime, False),
    ("sort_mtime_desc", lambda s: s.mtime, True),
    ("sort_size_asc", lambda s: s.size, False),
    ("sort_size_desc", lambda s: s.size, True),
]
DEFAULT_SORT = SORTS[0][0]
ZOOM_MIN, ZOOM_MAX = 1.0, 12.0

# display-only image correction (never written to the cache)
SAT_RANGE = (0.0, 3.0)
GAMMA_RANGE = (0.4, 2.5)
SAT_DEFAULT = GAMMA_DEFAULT = 1.0
DEFAULT_SIZE = "M"
CACHE_DIR = ".wsi_thumbs"
LOAD_THREADS = 4        # parallel thumbnail readers (I/O bound)
POLL_BATCH = 20         # queue items handled per UI tick
EXTS = (".svs", ".tif", ".tiff")

def app_dir():
    """Folder the program 'lives' in.

    For a PyInstaller one-file .exe, __file__ points into a temporary extraction folder that
    is deleted on exit - the log written there would vanish. Use the .exe's own folder instead.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


LOG_FILE = os.path.join(app_dir(), "wsi_review.log")

BANNER_KO = """\
============================================================
  WSI 썸네일 리뷰 / 정리 도구  (WSI_Review)
============================================================
  이 검은 창은 프로그램의 '기록 창'입니다. 정상입니다.
  썸네일 추출 진행 상황과 오류, 파일 이동 내역이 여기에
  남습니다. 창을 닫지 마세요.

  [ 사용 순서 ]
    1. 함께 열린 'WSI Thumbnail Review' 창에서
       [원본 폴더] / [이동 폴더]를 지정하고 [다시 스캔]
    2. 썸네일을 보며 검토
         클릭        = 크게 보기 (선택 아님)
         Ctrl+클릭   = 선택 (빨간 테두리)
         Shift+클릭  = 범위 선택
         P           = 비교용 고정(오른쪽 아래에 붙박이)
         + / -       = 썸네일 크기,  Ctrl+F = 파일명 찾기
       오른쪽 미리보기: 휠=줌, 드래그=이동, 0=줌 초기화
    3. Enter = 선택한 파일을 이동 폴더로 이동
       Ctrl+Z = 방금 이동 취소

  [ 주의 ]
    * 원본 폴더와 이동 폴더가 서로 다른 드라이브이면 '복사 후
      삭제'가 되어 매우 느립니다. 같은 드라이브를 쓰세요.
    * 이동 내역은 이동 폴더의 move_log.csv에도 기록됩니다.
    * 썸네일은 원본 폴더의 .wsi_thumbs에 저장되어 다음 실행
      때 즉시 표시됩니다. 지워도 다시 만들어집니다.

  [ 종료 방법 ]
    'WSI Thumbnail Review' 창을 닫으면 이 창도 닫힙니다.
    (이 창만 먼저 닫으면 프로그램이 강제 종료됩니다)

  기록 파일: {log}
============================================================
"""

BANNER_EN = """\
============================================================
  WSI Thumbnail Review / Cleanup  (WSI_Review)
============================================================
  This black window is the program's LOG WINDOW. It is normal.
  Thumbnail progress, errors and every file move are recorded
  here. Do not close it.

  [ How to use ]
    1. In the 'WSI Thumbnail Review' window that opened
       alongside, set [Source] / [Move to] and press [Rescan]
    2. Review the thumbnails
         Click        = show large (does not select)
         Ctrl+Click   = select (red border)
         Shift+Click  = select a range
         P            = pin for comparison (bottom right)
         + / -        = thumbnail size,  Ctrl+F = find by name
       Preview pane: wheel=zoom, drag=pan, 0=reset zoom
    3. Enter  = move the selected files to the target folder
       Ctrl+Z = undo the last move

  [ Warnings ]
    * If source and target are on different drives the files
      are copied and then deleted - very slow. Use the same
      drive.
    * Moves are also recorded in move_log.csv in the target.
    * Thumbnails are cached in .wsi_thumbs inside the source
      folder, so the next run is instant. Safe to delete.

  [ How to quit ]
    Close the 'WSI Thumbnail Review' window and this one
    closes with it.
    (Closing this one first kills the program.)

  Log file: {log}
============================================================
"""


def banner():
    return BANNER_EN if i18n.get_lang() == "en" else BANNER_KO
log = logging.getLogger("wsi_review")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
        log.addHandler(fh)
    except OSError as e:
        log.warning("cannot write log file %s: %s", LOG_FILE, e)


BG = "#202020"
CELL_BG = "#2c2c2c"
COLOR_SELECTED = "#e03030"
COLOR_FOCUS = "#3080ff"
TEXT_H = 34             # space reserved under each thumbnail for the file name (2 lines)
PAD = 8                 # gap between cells


# ----------------------------------------------------------------------------
# thumbnail extraction
# ----------------------------------------------------------------------------
def _page_desc(im):
    return str(im.tag_v2.get(270, "")).lower()


def _is_label_or_macro(im, base_aspect):
    """True for slide label / macro (glass slide photo) pages.

    Not only the description text is checked (it may be blanked by anonymisation tools):
    - NewSubfileType 1 / 9 marks label / macro in Aperio files
    - label & macro are stored as plain RGB (photometric 2), pyramid levels as YCbCr JPEG (6)
    - a real thumbnail is a downsample of level 0 and therefore has the same aspect ratio
    """
    t = im.tag_v2
    d = _page_desc(im)
    if "label" in d or "macro" in d:
        return True
    subfile = t.get(254, 0)
    if subfile in (1, 9):
        return True
    w, h = im.size
    aspect = w / h
    if abs(aspect - base_aspect) / base_aspect > 0.03:      # different shape than the scan area
        return True
    if t.get(262) == 2 and t.get(259) == 5:                  # RGB + LZW: the label page in Aperio files
        return True
    return False


def pick_thumbnail_page(path):
    """Return (page_index, reason, pages_info) for the embedded thumbnail page of an SVS."""
    im = Image.open(path)
    n = getattr(im, "n_frames", 1)
    im.seek(0)
    bw, bh = im.size
    base_aspect = bw / bh
    info, candidates = [], []
    for i in range(n):
        im.seek(i)
        skip = i != 0 and _is_label_or_macro(im, base_aspect)
        info.append((i, im.size, _page_desc(im).strip().replace("\n", " ")[:50], "skip" if skip else "ok"))
        if not skip:
            candidates.append((im.size[0] * im.size[1], i))
    im.close()
    candidates.sort()
    idx = candidates[0][1] if candidates else 0
    reason = "smallest level matching level-0 aspect" if candidates else "no candidate, using level 0"
    return idx, reason, info


def extract_thumbnail(path):
    """Return the embedded thumbnail (smallest pyramid level) of an Aperio SVS.
    Label / macro pages are never used; level 0 is never decoded."""
    idx, _, _ = pick_thumbnail_page(path)
    im = Image.open(path)
    im.seek(idx)
    if im.size[0] > 4000:                  # no small level at all: decode a reduced copy
        im.draft("RGB", (2000, 2000))
    thumb = im.copy()
    im.close()
    if thumb.size[0] > 2000:
        thumb.thumbnail((1920, 1920))
    return thumb


def check_folder(folder):
    """CLI helper: show which page is used for every slide, flagging suspicious ones."""
    files = sorted((f for f in os.listdir(folder) if f.lower().endswith(EXTS)), key=natural_key)
    bad = 0
    for f in files:
        path = os.path.join(folder, f)
        size = os.path.getsize(path)
        if size == 0:
            print(f"ERROR  {f}: empty file (0 bytes)")
            bad += 1
            continue
        try:
            idx, reason, info = pick_thumbnail_page(path)
        except Exception as e:  # noqa
            print(f"ERROR  {f}: {e}  ({size} bytes)")
            bad += 1
            continue
        i, size, desc, _ = info[idx]
        w, h = size
        flag = ""
        if "label" in desc.lower() or "macro" in desc.lower() or w < 500:
            flag = "  <-- CHECK"
            bad += 1
        print(f"page {idx} {w}x{h:<5} {f}{flag}")
        if flag:
            for row in info:
                print("        ", row)
    print(f"{len(files)} files, {bad} flagged")


def cache_key(path):
    st = os.stat(path)
    return hashlib.md5(f"v2|{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]


def cache_path(src_dir, path):
    return os.path.join(src_dir, CACHE_DIR, cache_key(path) + "_t.jpg")


def is_cached(src_dir, path):
    return os.path.exists(cache_path(src_dir, path))


def move_cache(src_dir, old_cache, new_path):
    """Rename a cached thumbnail so it still matches `new_path`.

    The cache key contains the file name, so a rename would otherwise orphan the cache and
    force a fresh extraction from the slide. `old_cache` must be computed BEFORE the rename
    (cache_key() stats the file, which no longer exists at the old path afterwards).
    """
    if not old_cache or not os.path.exists(old_cache):
        return False
    try:
        new_cache = cache_path(src_dir, new_path)
    except OSError:
        return False
    if os.path.abspath(new_cache) == os.path.abspath(old_cache):
        return True
    try:
        if os.path.exists(new_cache):
            os.remove(old_cache)       # already cached under the new name
        else:
            os.rename(old_cache, new_cache)
        return True
    except OSError as e:
        log.warning("cache move failed for %s: %s", os.path.basename(new_path), e)
        return False


def load_cached(src_dir, path):
    """Return the thumbnail from the on-disk cache, extracting (and caching) it only if missing."""
    name = os.path.basename(path)
    tp = cache_path(src_dir, path)
    if os.path.exists(tp):
        try:
            return Image.open(tp).convert("RGB")
        except Exception as e:  # noqa - corrupt cache file: rebuild it
            log.warning("cache file unreadable, rebuilding: %s (%s)", name, e)
            os.remove(tp)
    size = os.path.getsize(path)
    if size == 0:
        raise ValueError("empty file (0 bytes)")
    if size < 64 * 1024:
        raise ValueError(f"file too small to be a slide ({size} bytes)")
    t0 = time.time()
    try:
        idx, reason, info = pick_thumbnail_page(path)
    except Image.UnidentifiedImageError:
        raise ValueError("not a readable TIFF/SVS (corrupt or incomplete file)")
    im = Image.open(path)
    im.seek(idx)
    if im.size[0] > 4000:
        im.draft("RGB", (2000, 2000))
    thumb = im.copy()
    im.close()
    if thumb.size[0] > 2000:
        thumb.thumbnail((1920, 1920))
    thumb = thumb.convert("RGB")
    os.makedirs(os.path.dirname(tp), exist_ok=True)
    thumb.save(tp, quality=90)
    log.info("extracted  %-50s page %d %dx%d  %.2fs  (%s)", name, idx, thumb.size[0], thumb.size[1],
             time.time() - t0, reason)
    return thumb


@functools.lru_cache(maxsize=64)
def _gamma_lut(gamma):
    """256-entry lookup table; gamma < 1 darkens mid-tones, > 1 brightens them."""
    inv = 1.0 / max(gamma, 1e-3)
    return [min(255, int(round(255.0 * (i / 255.0) ** inv))) for i in range(256)]


def enhance(img, sat, gamma):
    """Apply saturation / gamma for display. Returns the image unchanged at the defaults."""
    if abs(sat - 1.0) > 0.01:
        img = ImageEnhance.Color(img).enhance(sat)
    if abs(gamma - 1.0) > 0.01:
        lut = _gamma_lut(round(gamma, 2))
        img = img.point(lut * len(img.getbands()))
    return img


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ----------------------------------------------------------------------------
# filename grouping:  "S 2024000123,A,PD-L1[22C3+].svs"
#                      ^accession   ^block ^stain ("" = H&E)
# ----------------------------------------------------------------------------
BLOCK_FIELD = 1                       # 0-based index of the block field between commas
GROUP_RE = re.compile(r"^(?P<base>.*?)(?P<suffix>_\d+)?$")


def split_fields(name):
    stem, ext = os.path.splitext(name)
    return stem.split(","), ext


def block_of(name):
    """Return (base_block, existing_suffix) or (None, None) if the name has no block field."""
    parts, _ = split_fields(name)
    if len(parts) <= BLOCK_FIELD:
        return None, None
    m = GROUP_RE.match(parts[BLOCK_FIELD].strip())
    return m.group("base"), (m.group("suffix") or "")


def accession_of(name):
    parts, _ = split_fields(name)
    return parts[0] if parts else name


def apply_group(name, suffix):
    """Return the new filename with the block field tagged by `suffix` ('' removes the tag)."""
    parts, ext = split_fields(name)
    if len(parts) <= BLOCK_FIELD:
        return None
    m = GROUP_RE.match(parts[BLOCK_FIELD].strip())
    parts[BLOCK_FIELD] = m.group("base") + suffix
    return ",".join(parts) + ext


def next_group_suffix(names, accession, base_block):
    """Smallest unused _<n> among files of the same accession + block."""
    used = set()
    for n in names:
        if accession_of(n) != accession:
            continue
        b, suf = block_of(n)
        if b == base_block and suf:
            used.add(int(suf[1:]))
    i = 1
    while i in used:
        i += 1
    return f"_{i}"


def same_drive(a, b):
    return os.path.splitdrive(os.path.abspath(a))[0].lower() == os.path.splitdrive(os.path.abspath(b))[0].lower()


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class Slide:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        try:
            st = os.stat(path)
            self.size, self.mtime = st.st_size, st.st_mtime
        except OSError:
            self.size, self.mtime = 0, 0.0
        self.visible = True
        self.thumb = None      # PIL full thumbnail (preview)
        self.mid = None        # reduced copy (grid rendering)
        self.tk_small = None   # PhotoImage currently shown in the grid (None = not rendered)
        self.rendered_w = 0
        self.selected = False
        self.error = None
        # canvas item ids
        self.rect = self.img = self.txt = self.err_txt = None


class App(tk.Tk):
    def __init__(self, src, dst):
        super().__init__()
        self.title(t("app_title"))
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{int(sw * 0.9)}x{int(sh * 0.85)}+{int(sw * 0.05)}+{int(sh * 0.05)}")
        self.src = src
        self.dst = dst
        self.all_slides = []       # every file found in the folder
        self.slides = []           # current view: filtered + sorted
        self.focus_idx = None
        self.sat = SAT_DEFAULT     # display-only correction
        self.gamma = GAMMA_DEFAULT
        self._enh_job = None
        self.prev_zoom = 1.0       # preview zoom (1.0 = fit to panel)
        self.prev_cx = self.prev_cy = 0.5
        self._drag = None
        self._filter_job = None
        self.pinned = None
        self.undo_stack = []
        self.busy = False
        self._render_job = None
        self.q = queue.Queue()
        self.cols = 1
        self.size_idx = [n for n, _ in SIZES].index(DEFAULT_SIZE)
        self.thumb_w = SIZES[self.size_idx][1]
        self._poll_job = None
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.after(100, self._poll_queue)
        self.after(200, lambda: self.rescan(silent=True))

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
                t("close_confirm_title"), t("close_busy_move"), parent=self, icon="warning"):
            return
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        log.info("=== exit. log file: %s ===", LOG_FILE)
        self.destroy()

    # geometry of one grid cell for the current thumbnail size
    @property
    def cell_w(self):
        return self.thumb_w + 2 * 4

    @property
    def img_h(self):
        return int(self.thumb_w * 0.6)

    @property
    def cell_h(self):
        return self.img_h + TEXT_H + 2 * 4

    # ---------------- language ----------------
    def _change_language(self, label):
        code = i18n.code_for(label)
        if code == i18n.get_lang():
            return
        keep = dict(src=self.src_var.get(), dst=self.dst_var.get(),
                    sort=self._sort_key_name(), size=self.size_var.get(),
                    filt=self.filter_var.get(), sat=self.sat, gamma=self.gamma)
        i18n.set_lang(code)
        for w in self.winfo_children():
            w.destroy()
        self.title(t("app_title"))
        self._build_ui()
        self.src_var.set(keep["src"]);  self.dst_var.set(keep["dst"])
        self.sort_var.set(t(keep["sort"]))          # same sort, label in the new language
        self.size_var.set(keep["size"])
        self.filter_var.set(keep["filt"])
        self.thumb_w = SIZES[[n for n, _ in SIZES].index(keep["size"])][1]
        self.sat_scale.set(keep["sat"]); self.gam_scale.set(keep["gamma"])
        self._build_items()             # the canvas was destroyed - recreate the cells
        self._apply_view()
        self._update_status_count()
        log.info("language -> %s", code)

    # ---------------- UI construction ----------------
    def _build_ui(self):
        # ---- row 1: folders (entries stretch with the window) ----
        top = ttk.Frame(self, padding=(6, 6, 6, 2))
        top.pack(side=tk.TOP, fill=tk.X)
        top.columnconfigure(1, weight=1, uniform="path")
        top.columnconfigure(4, weight=1, uniform="path")
        ttk.Label(top, text=t("src_folder")).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.src_var = tk.StringVar(value=self.src)
        ttk.Entry(top, textvariable=self.src_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text=t("browse"), width=11,
                   command=self._pick_src).grid(row=0, column=2, padx=(4, 16))
        ttk.Label(top, text=t("dst_folder")).grid(row=0, column=3, sticky="w", padx=(0, 6))
        self.dst_var = tk.StringVar(value=self.dst)
        ttk.Entry(top, textvariable=self.dst_var).grid(row=0, column=4, sticky="ew")
        ttk.Button(top, text=t("browse"), width=11,
                   command=self._pick_dst).grid(row=0, column=5, padx=(4, 0))
        ttk.Label(top, text=t("language")).grid(row=0, column=6, sticky="w", padx=(16, 6))
        self.lang_var = tk.StringVar(value=i18n.lang_label())
        lang_cb = ttk.Combobox(top, textvariable=self.lang_var, width=9, state="readonly",
                               values=[lb for lb, _ in i18n.LANGS])
        lang_cb.grid(row=0, column=7)
        lang_cb.bind("<<ComboboxSelected>>",
                     lambda e: self._change_language(self.lang_var.get()))

        # ---- row 2: actions + view options ----
        bar = ttk.Frame(self, padding=(6, 2, 6, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text=t("rescan"), command=self.rescan).pack(side=tk.LEFT)
        ttk.Button(bar, text=t("move_sel"),
                   command=self.move_selected).pack(side=tk.LEFT, padx=6)
        ttk.Button(bar, text=t("undo_btn"), command=self.undo).pack(side=tk.LEFT)
        ttk.Button(bar, text=t("group_btn"), command=self.group_selected).pack(side=tk.LEFT, padx=6)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=12)

        ttk.Label(bar, text=t("sort")).pack(side=tk.LEFT, padx=(0, 4))
        self.sort_var = tk.StringVar(value=DEFAULT_SORT)
        sb = ttk.Combobox(bar, textvariable=self.sort_var, width=19, state="readonly",
                          values=[t(k) for k, _, _ in SORTS])
        self._sort_labels = [t(k) for k, _, _ in SORTS]
        sb.pack(side=tk.LEFT)
        sb.bind("<<ComboboxSelected>>", lambda e: (self._apply_view(), self.focus_set()))
        # show the label for the currently selected key
        for k, _, _ in SORTS:
            if self.sort_var.get() in (k, t(k)):
                self.sort_var.set(t(k))
                break

        ttk.Label(bar, text="  " + t("size")).pack(side=tk.LEFT, padx=(12, 4))
        self.size_var = tk.StringVar(value=DEFAULT_SIZE)
        cb = ttk.Combobox(bar, textvariable=self.size_var, values=[n for n, _ in SIZES],
                          width=5, state="readonly")
        cb.pack(side=tk.LEFT)
        cb.bind("<<ComboboxSelected>>", lambda e: (self._set_size(self.size_var.get()), self.focus_set()))

        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=12)

        ttk.Label(bar, text=t("find_name")).pack(side=tk.LEFT, padx=(0, 4))
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(bar, textvariable=self.filter_var, width=22)
        self.filter_entry.pack(side=tk.LEFT)
        self.filter_var.trace_add("write", lambda *a: self._schedule_filter())
        ttk.Button(bar, text=t("clear"), width=7,
                   command=lambda: (self.filter_var.set(""), self.focus_set())).pack(side=tk.LEFT, padx=4)
        self.count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.count_var, foreground="#0050a0").pack(side=tk.LEFT, padx=8)

        # ---- row 3: display-only image correction ----
        adj = ttk.Frame(self, padding=(6, 0, 6, 6))
        adj.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(adj, text=t("adjust_title")).pack(side=tk.LEFT)

        # the StringVars must exist before the scales: Scale.set() fires the command callback
        self.sat_lbl = tk.StringVar(value=f"{SAT_DEFAULT:.2f}")
        self.gam_lbl = tk.StringVar(value=f"{GAMMA_DEFAULT:.2f}")

        ttk.Label(adj, text="   " + t("saturation")).pack(side=tk.LEFT, padx=(12, 4))
        self.sat_scale = ttk.Scale(adj, from_=SAT_RANGE[0], to=SAT_RANGE[1], length=170,
                                   value=SAT_DEFAULT,
                                   command=lambda v: self._on_adjust("sat", v))
        self.sat_scale.pack(side=tk.LEFT)
        ttk.Label(adj, textvariable=self.sat_lbl, width=5).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(adj, text="   " + t("gamma")).pack(side=tk.LEFT, padx=(12, 4))
        self.gam_scale = ttk.Scale(adj, from_=GAMMA_RANGE[0], to=GAMMA_RANGE[1], length=170,
                                   value=GAMMA_DEFAULT,
                                   command=lambda v: self._on_adjust("gamma", v))
        self.gam_scale.pack(side=tk.LEFT)
        ttk.Label(adj, textvariable=self.gam_lbl, width=5).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(adj, text=t("gamma_hint"), foreground="#606060").pack(side=tk.LEFT, padx=(2, 0))

        ttk.Button(adj, text=t("reset"), width=10,
                   command=self._reset_adjust).pack(side=tk.LEFT, padx=12)
        ttk.Button(adj, text=t("faint_preset"), width=16,
                   command=lambda: self._set_adjust(1.8, 0.75)).pack(side=tk.LEFT)

        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status, anchor="w", padding=(6, 2)).pack(side=tk.BOTTOM, fill=tk.X)

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # grid (left): everything is drawn directly on one canvas
        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.canvas = tk.Canvas(left, bg=BG, highlightthickness=0)
        vsb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=lambda a, b: (vsb.set(a, b), self._schedule_render()))
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # preview (right)
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.prev_title = tk.StringVar(value=t("pick_slide"))
        head = ttk.Frame(right)
        head.pack(fill=tk.X, padx=4)
        ttk.Label(head, textvariable=self.prev_title, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        ttk.Button(head, text=t("zoom_reset"), width=14,
                   command=self._reset_zoom).pack(side=tk.RIGHT)
        self.prev_canvas = tk.Canvas(right, bg="#303030", highlightthickness=0, cursor="fleur")
        self.prev_canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        self.pin_title = tk.StringVar(value=t("pin_none"))
        pinhead = ttk.Frame(right)
        pinhead.pack(fill=tk.X, padx=4)
        ttk.Label(pinhead, textvariable=self.pin_title,
                  font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.unpin_btn = ttk.Button(pinhead, text=t("unpin"), width=11, command=self._unpin)
        self.unpin_btn.pack(side=tk.RIGHT)
        self.unpin_btn.state(["disabled"])
        self.pin_canvas = tk.Canvas(right, bg="#303030", highlightthickness=0)
        self.pin_canvas.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        for c in (self.prev_canvas, self.pin_canvas):
            c.bind("<Configure>", lambda e: self._update_preview())
        self.prev_canvas.bind("<Button-1>", self._prev_press)
        self.prev_canvas.bind("<B1-Motion>", self._prev_drag)
        self.prev_canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.prev_canvas.bind("<Double-Button-1>", lambda e: self._reset_zoom())

        # keys
        self.bind("<Left>", lambda e: self._move_focus(-1))
        self.bind("<Right>", lambda e: self._move_focus(1))
        self.bind("<Up>", lambda e: self._move_focus(-self.cols))
        self.bind("<Down>", lambda e: self._move_focus(self.cols))
        self.bind("<Prior>", lambda e: self._move_focus(-self.cols * self._rows_per_page()))
        self.bind("<Next>", lambda e: self._move_focus(self.cols * self._rows_per_page()))
        self.bind("<Home>", lambda e: self._move_focus(-10 ** 9))
        self.bind("<End>", lambda e: self._move_focus(10 ** 9))
        self.bind("<space>", lambda e: self._toggle_focused())
        self.bind("<Return>", lambda e: self.move_selected())
        self.bind("<m>", lambda e: self.move_selected())
        self.bind("<M>", lambda e: self.move_selected())
        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<p>", lambda e: self._pin())
        self.bind("<P>", lambda e: self._unpin())      # Shift+P always releases
        self.bind("<Control-a>", lambda e: self._select_all(True))
        self.bind("<Escape>", lambda e: self._select_all(False))
        self.bind("<F5>", lambda e: self.rescan())
        self.bind("<Control-f>", lambda e: (self.filter_entry.focus_set(),
                                            self.filter_entry.select_range(0, "end"), "break")[-1])
        self.bind("<0>", lambda e: self._reset_zoom())
        self.bind("<g>", lambda e: self.group_selected())
        self.bind("<G>", lambda e: self.group_selected())
        for k in ("<plus>", "<equal>", "<KP_Add>"):
            self.bind(k, lambda e: self._step_size(1))
        for k in ("<minus>", "<KP_Subtract>"):
            self.bind(k, lambda e: self._step_size(-1))

    def _ask_dir(self, start, title):
        # the folder may not exist yet (e.g. "<src>/_removed"): start from the nearest existing ancestor
        d = os.path.abspath(start) if start else os.getcwd()
        while d and not os.path.isdir(d):
            parent = os.path.dirname(d)
            if parent == d:
                d = os.getcwd()
                break
            d = parent
        try:
            return filedialog.askdirectory(parent=self, initialdir=d, title=title, mustexist=False)
        except Exception as e:  # noqa
            messagebox.showerror("Folder dialog", str(e), parent=self)
            return ""

    def _pick_src(self):
        d = self._ask_dir(self.src_var.get(), "Select source folder (SVS files)")
        if d:
            self.src_var.set(os.path.normpath(d))
            self.rescan()

    def _pick_dst(self):
        d = self._ask_dir(self.dst_var.get() or self.src_var.get(), "Select 'Move to' folder")
        if d:
            self.dst_var.set(os.path.normpath(d))
            self.status.set(f"Move to: {self.dst_var.get()}")

    # ---------------- scanning / loading ----------------
    def rescan(self, silent=False):
        """Scan the source folder. `silent` suppresses the 'not found' dialog (used at startup)."""
        self.src = self.src_var.get().strip()
        if not self.src or not os.path.isdir(self.src):
            self.canvas.delete("all")
            self.all_slides = self.slides = []
            self.focus_idx = None
            self.pinned = None
            self._update_preview()
            self.count_var.set("")
            msg = t("pick_src") if not self.src else t("folder_missing", path=self.src)
            self.status.set(msg)
            if not silent and self.src:
                messagebox.showerror(t("folder_missing_title"),
                                     t("folder_missing_msg", path=self.src))
            log.info("scan skipped: %r", self.src)
            return
        self.canvas.delete("all")
        for s in self.all_slides:
            s.rect = s.img = s.txt = s.err_txt = None
        files = sorted((f for f in os.listdir(self.src) if f.lower().endswith(EXTS)), key=natural_key)
        log.info("scan %s: %d slide files", self.src, len(files))
        self.all_slides = [Slide(os.path.join(self.src, f)) for f in files]
        self.pinned = None
        self._build_items()
        self._apply_view(keep_focus=False)
        self.canvas.yview_moveto(0)
        self.loaded = 0
        self.status.set(t("scanning", n=len(self.all_slides)))
        threading.Thread(target=self._loader, args=(list(self.all_slides), self.src), daemon=True).start()

    # ---------------- view: filter + sort ----------------
    def _schedule_filter(self):
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(180, self._apply_view)

    def _update_count(self):
        hidden = len(self.all_slides) - len(self.slides)
        self.count_var.set(t("shown", shown=len(self.slides), total=len(self.all_slides))
                           + (t("hidden_n", n=hidden) if hidden else ""))

    def _sort_key_name(self):
        """The i18n key of the currently selected sort (the combobox shows a translated label)."""
        cur = self.sort_var.get()
        for k, _, _ in SORTS:
            if cur in (k, t(k)):
                return k
        # the label may still be in the previous language - match against every language
        for k, _, _ in SORTS:
            for table in i18n.STRINGS.values():
                if table.get(k) == cur:
                    return k
        return SORTS[0][0]

    def _sort_spec(self):
        want = self._sort_key_name()
        for k, key, rev in SORTS:
            if k == want:
                return key, rev
        return SORTS[0][1], SORTS[0][2]

    def _apply_view(self, keep_focus=True):
        """Rebuild self.slides from all_slides using the filter text and the sort order."""
        self._filter_job = None
        focused = self.slides[self.focus_idx] if (keep_focus and self.focus_idx is not None
                                                  and self.focus_idx < len(self.slides)) else None
        needle = self.filter_var.get().strip().lower()
        for sl in self.all_slides:
            sl.visible = (needle in sl.name.lower()) if needle else True
        key, rev = self._sort_spec()
        self.slides = sorted([sl for sl in self.all_slides if sl.visible], key=key, reverse=rev)

        for sl in self.all_slides:
            state = "normal" if sl.visible else "hidden"
            for item in (sl.rect, sl.img, sl.txt, sl.err_txt):
                if item is not None:
                    self.canvas.itemconfigure(item, state=state)

        if focused is not None and focused in self.slides:
            self.focus_idx = self.slides.index(focused)
        else:
            self.focus_idx = 0 if self.slides else None
        self._layout()
        if self.focus_idx is not None:
            self._scroll_to(self.focus_idx)
        self._update_preview()
        self._update_count()

    def _loader(self, slides, src):
        t0 = time.time()
        cached = [s for s in slides if is_cached(src, s.path)]
        missing = [s for s in slides if s not in cached]
        log.info("thumbnails: %d cached, %d to extract (cache dir: %s)", len(cached), len(missing),
                 os.path.join(src, CACHE_DIR))
        self.q.put(("plan", len(cached), len(missing)))
        errors = []

        def work(s):
            try:
                thumb = load_cached(src, s.path)
                mid = thumb.copy()
                mid.thumbnail((SIZES[-1][1], SIZES[-1][1]))
                self.q.put(("thumb", s, thumb, mid, None))
            except ValueError as e:                     # expected: empty / corrupt / unreadable file
                log.error("ERROR      %-50s %s", s.name, e)
                errors.append(s.name)
                self.q.put(("thumb", s, None, None, str(e)))
            except Exception as e:  # noqa               # unexpected: keep the traceback in the log
                log.error("ERROR      %-50s %s: %s\n%s", s.name, type(e).__name__, e, traceback.format_exc())
                errors.append(s.name)
                self.q.put(("thumb", s, None, None, f"{type(e).__name__}: {e}"))

        with ThreadPoolExecutor(max_workers=LOAD_THREADS) as ex:
            list(ex.map(work, cached))          # instant: only reads small cached JPEGs
            list(ex.map(work, missing))         # extraction happens only for these
        dt = time.time() - t0
        log.info("done in %.1fs: %d from cache, %d newly extracted, %d errors", dt, len(cached),
                 len(missing) - len(errors), len(errors))
        if errors:
            log.warning("files without thumbnail:\n  " + "\n  ".join(errors))
        self.q.put(("done", dt, len(cached), len(missing)))

    def _poll_queue(self):
        got_thumbs = False
        try:
            for _ in range(POLL_BATCH):
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "plan":
                    self.status.set(t("plan", cached=item[1], missing=item[2]))
                elif kind == "done":
                    errs = sum(1 for s in self.all_slides if s.error)
                    msg = t("ready", n=len(self.all_slides), sec=item[1],
                            cached=item[2], fresh=item[3] - errs)
                    msg += t("ready_err", errs=errs) if errs else ")"
                    self.status.set(msg + t("ready_hint"))
                    self._apply_view()
                elif kind == "thumb":
                    _, s, thumb, mid, err = item
                    if s.rect is None:          # slide list was rebuilt meanwhile
                        continue
                    s.thumb, s.mid, s.error = thumb, mid, err
                    got_thumbs = True
                    self.loaded += 1
                    if self.loaded % 10 == 0:
                        self.status.set(t("loading", done=self.loaded, total=len(self.all_slides)))
                    if err and s in self.slides:
                        self._style_cell(s, self.slides.index(s))
                        self._layout_error_text(s)
                    if self.focus_idx is not None and self.slides[self.focus_idx] is s:
                        self._update_preview()
                elif kind == "moved":
                    self._on_move_done(item[1], item[2])
                elif kind == "renamed":
                    self._on_rename_done(item[1], item[2])
                elif kind == "undone":
                    self._on_undo_done(item[1], item[2])
        except queue.Empty:
            pass
        except tk.TclError:          # window closed while loading
            return
        try:
            if got_thumbs:
                self._schedule_render(20)
            self._poll_job = self.after(30, self._poll_queue)
        except tk.TclError:          # window closed
            self._poll_job = None

    # ---------------- grid (canvas) ----------------
    def _build_items(self):
        c = self.canvas
        for s in self.all_slides:
            s.rect = c.create_rectangle(0, 0, 1, 1, fill=CELL_BG, outline=BG, width=3)
            s.img = c.create_image(0, 0, anchor="n")
            s.txt = c.create_text(0, 0, anchor="nw", fill="#ddd", font=("Segoe UI", 9), width=self.thumb_w)
            s.tk_small = None
            s.rendered_w = 0

    def _layout_error_text(self, s):
        if s not in self.slides:
            return
        i = self.slides.index(s)
        x, y = self._cell_xy(i)
        msg = f"ERROR\n{s.error}"
        if s.err_txt is None:
            s.err_txt = self.canvas.create_text(0, 0, anchor="center", fill="#ff7070", justify="center",
                                                font=("Segoe UI", 9, "bold"))
        self.canvas.coords(s.err_txt, x + self.cell_w / 2, y + 4 + self.img_h / 2)
        self.canvas.itemconfigure(s.err_txt, text=msg, width=self.thumb_w - 8)

    def _cell_xy(self, i):
        r, col = divmod(i, self.cols)
        return PAD + col * (self.cell_w + PAD), PAD + r * (self.cell_h + PAD)

    def _layout(self):
        """Position every item for the current size / column count. Pure coordinate math -> instant."""
        c = self.canvas
        cw, ch, ih = self.cell_w, self.cell_h, self.img_h
        for i, s in enumerate(self.slides):
            x, y = self._cell_xy(i)
            c.coords(s.rect, x, y, x + cw, y + ch)
            c.coords(s.img, x + cw / 2, y + 4)
            c.coords(s.txt, x + 4, y + 4 + ih + 2)
            c.itemconfigure(s.txt, width=self.thumb_w)
            if s.error:
                self._layout_error_text(s)
        rows = (len(self.slides) + self.cols - 1) // self.cols
        c.configure(scrollregion=(0, 0, self.cols * (cw + PAD) + PAD, rows * (ch + PAD) + PAD))
        self._style_all()
        self._schedule_render(0)

    def _on_canvas_resize(self, e):
        cols = max(1, (e.width - PAD) // (self.cell_w + PAD))
        if cols != self.cols:
            self.cols = cols
            self._layout()
        else:
            self._schedule_render()

    def _rows_per_page(self):
        return max(1, self.canvas.winfo_height() // (self.cell_h + PAD))

    def _set_size(self, name):
        names = [n for n, _ in SIZES]
        if name not in names:
            return
        self.size_idx = names.index(name)
        self.thumb_w = SIZES[self.size_idx][1]
        self.size_var.set(name)
        self.cols = max(1, (self.canvas.winfo_width() - PAD) // (self.cell_w + PAD))
        for s in self.all_slides:                   # drop old-size images; visible ones re-render right away
            s.tk_small = None
            s.rendered_w = 0
            if s.img is not None:
                self.canvas.itemconfigure(s.img, image="")
        self._layout()
        if self.focus_idx is not None:
            self._scroll_to(self.focus_idx)
        self._render_visible()
        self.status.set(t("size_status", name=name, px=self.thumb_w, cols=self.cols))

    # ---------------- display-only correction ----------------
    def _on_adjust(self, which, value):
        if not hasattr(self, "gam_lbl"):
            return                      # fired while the toolbar is still being built
        v = float(value)
        if which == "sat":
            self.sat = v
            self.sat_lbl.set(f"{v:.2f}")
        else:
            self.gamma = v
            self.gam_lbl.set(f"{v:.2f}")
        if self._enh_job is not None:
            self.after_cancel(self._enh_job)
        self._enh_job = self.after(120, self._redraw_all)

    def _set_adjust(self, sat, gamma):
        self.sat_scale.set(sat)
        self.gam_scale.set(gamma)          # each set() fires _on_adjust

    def _reset_adjust(self):
        self._set_adjust(SAT_DEFAULT, GAMMA_DEFAULT)

    def _redraw_all(self):
        """Re-render every thumbnail with the current correction (display only)."""
        self._enh_job = None
        for sl in self.all_slides:
            sl.rendered_w = 0              # force a re-render of the grid cells
        self._render_visible()
        self._update_preview()

    def _step_size(self, d):
        i = min(max(0, self.size_idx + d), len(SIZES) - 1)
        if i != self.size_idx:
            self._set_size(SIZES[i][0])

    def _on_wheel(self, e):
        if e.widget.winfo_toplevel() is not self:
            return
        under = self.canvas.winfo_containing(e.x_root, e.y_root)
        if under is self.canvas:
            self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")
        elif under is self.prev_canvas:
            self._zoom_at(1.25 if e.delta > 0 else 0.8,
                          e.x_root - self.prev_canvas.winfo_rootx(),
                          e.y_root - self.prev_canvas.winfo_rooty())

    # ---- lazy rendering: only cells near the viewport hold a PhotoImage ----
    def _visible_range(self, margin_rows=1):
        if not self.slides:
            return 0, 0
        top, bot = self.canvas.yview()
        rows = (len(self.slides) + self.cols - 1) // self.cols
        total = rows * (self.cell_h + PAD) + PAD
        r0 = max(0, int(top * total // (self.cell_h + PAD)) - margin_rows)
        r1 = min(rows, int(bot * total // (self.cell_h + PAD)) + 1 + margin_rows)
        return r0 * self.cols, min(len(self.slides), r1 * self.cols)

    def _schedule_render(self, delay=40):
        if self._render_job is not None:
            self.after_cancel(self._render_job)
        self._render_job = self.after(delay, self._render_visible)

    def _render_visible(self):
        self._render_job = None
        a, b = self._visible_range()
        # evict images far from the viewport (keeps memory flat even at XXL with 1000 slides)
        ea, eb = self._visible_range(margin_rows=6)
        for i, s in enumerate(self.slides):
            if s.tk_small is not None and not (ea <= i < eb):
                s.tk_small = None
                s.rendered_w = 0
                self.canvas.itemconfigure(s.img, image="")
        for i in range(a, b):
            s = self.slides[i]
            if s.thumb is None or s.rendered_w == self.thumb_w:
                continue
            self._render_cell(s)

    def _render_cell(self, s):
        src = s.mid if s.mid is not None else s.thumb
        w, h = src.size
        sc = min(self.thumb_w / w, self.img_h / h)
        small = src.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.BILINEAR, reducing_gap=2.0)
        s.tk_small = ImageTk.PhotoImage(enhance(small, self.sat, self.gamma))
        s.rendered_w = self.thumb_w
        self.canvas.itemconfigure(s.img, image=s.tk_small)

    def _style_cell(self, s, idx):
        if s.selected:
            color = COLOR_SELECTED
        elif idx == self.focus_idx:
            color = COLOR_FOCUS
        else:
            color = BG
        label = ("📌 " + s.name) if self.pinned is s else s.name
        if s.error:
            label = "ERROR: " + s.name
        self.canvas.itemconfigure(s.rect, outline=color, fill=(color if color != BG else CELL_BG))
        self.canvas.itemconfigure(s.txt, text=label, fill="#fff" if color != BG else "#ddd")

    def _style_all(self):
        for i, s in enumerate(self.slides):
            self._style_cell(s, i)

    # ---------------- selection / focus ----------------
    def _index_at(self, x, y):
        """Slide index under canvas coords (x, y), or None."""
        cx, cy = self.canvas.canvasx(x), self.canvas.canvasy(y)
        col = int((cx - PAD) // (self.cell_w + PAD))
        row = int((cy - PAD) // (self.cell_h + PAD))
        if col < 0 or col >= self.cols or row < 0:
            return None
        i = row * self.cols + col
        if i >= len(self.slides):
            return None
        x0, y0 = self._cell_xy(i)
        if cx > x0 + self.cell_w or cy > y0 + self.cell_h:
            return None                       # in the gap between cells
        return i

    def _on_canvas_click(self, e):
        self.focus_set()
        idx = self._index_at(e.x, e.y)
        if idx is None:
            return
        ctrl, shift = bool(e.state & 0x4), bool(e.state & 0x1)
        s = self.slides[idx]
        if shift and self.focus_idx is not None:
            a, b = sorted((self.focus_idx, idx))
            for i in range(a, b + 1):
                self.slides[i].selected = True
                self._style_cell(self.slides[i], i)
            self._update_status_count()
        elif ctrl:
            s.selected = not s.selected
            self._update_status_count()
        self._set_focus(idx)

    def _set_focus(self, idx):
        old = self.focus_idx
        self.focus_idx = idx
        if old is not None and old < len(self.slides) and old != idx:
            self._style_cell(self.slides[old], old)
        if idx is not None:
            self._style_cell(self.slides[idx], idx)
        self._update_preview()

    def _move_focus(self, delta):
        if not self.slides:
            return
        old = self.focus_idx if self.focus_idx is not None else 0
        new = min(max(0, old + delta), len(self.slides) - 1)
        self._set_focus(new)
        self._scroll_to(new)

    def _scroll_to(self, idx):
        rows = (len(self.slides) + self.cols - 1) // self.cols
        total = rows * (self.cell_h + PAD) + PAD
        _, y0 = self._cell_xy(idx)
        y1 = y0 + self.cell_h + PAD
        top, bot = self.canvas.yview()
        vis0, vis1 = top * total, bot * total
        if y0 - PAD < vis0:
            self.canvas.yview_moveto((y0 - PAD) / total)
        elif y1 > vis1:
            self.canvas.yview_moveto((y1 - (vis1 - vis0)) / total)

    def _toggle_focused(self):
        if self.focus_idx is None:
            return
        s = self.slides[self.focus_idx]
        s.selected = not s.selected
        self._style_cell(s, self.focus_idx)
        self._update_status_count()

    def _select_all(self, flag):
        """Select / clear every slide in the current view (hidden ones are cleared too)."""
        if not flag:
            for s in self.all_slides:
                s.selected = False
        for i, s in enumerate(self.slides):
            s.selected = flag
        self._style_all()
        self._update_status_count()

    def _update_status_count(self):
        n = sum(s.selected for s in self.all_slides)
        hidden_sel = sum(1 for s in self.all_slides if s.selected and not s.visible)
        msg = t("sel_count", n=n, total=len(self.all_slides))
        if hidden_sel:
            msg += t("sel_hidden", n=hidden_sel)
        self.status.set(msg + t("sel_hint"))

    def _restyle(self, sl):
        """Redraw one slide's cell, ignoring slides hidden by the filter or already removed."""
        if sl is None or sl.rect is None:
            return
        try:
            idx = self.slides.index(sl)
        except ValueError:
            return                      # currently filtered out - nothing on screen to restyle
        self._style_cell(sl, idx)

    def _pin(self):
        """P: pin the focused slide, or release it if it is already the pinned one."""
        if self.focus_idx is None or self.focus_idx >= len(self.slides):
            return
        s = self.slides[self.focus_idx]
        old = self.pinned
        self.pinned = None if old is s else s
        self._restyle(old)
        self._restyle(s)
        self._update_preview()

    def _unpin(self):
        """Release the pinned slide from anywhere (button or Shift+P)."""
        if self.pinned is None:
            self.status.set(t("no_pin"))
            return
        old, self.pinned = self.pinned, None
        self._restyle(old)
        self._update_preview()
        self.status.set(t("unpinned", name=old.name))

    # ---------------- preview ----------------
    def _draw_on(self, canvas, pil):
        """Draw `pil` into `canvas` at the current zoom / centre. Only the visible region is
        resized, so zooming stays cheap even at 12x."""
        W, H = max(canvas.winfo_width(), 20), max(canvas.winfo_height(), 20)
        w, h = pil.size
        fit = min(W / w, H / h)
        scale = fit * self.prev_zoom
        sw, sh = min(w, W / scale), min(h, H / scale)
        left = min(max(self.prev_cx * w - sw / 2, 0), max(0, w - sw))
        top = min(max(self.prev_cy * h - sh / 2, 0), max(0, h - sh))
        box = (int(left), int(top), int(min(left + sw, w)), int(min(top + sh, h)))
        region = pil.crop(box)
        tw, th = max(1, int((box[2] - box[0]) * scale)), max(1, int((box[3] - box[1]) * scale))
        resample = Image.LANCZOS if scale <= 1.5 else Image.NEAREST
        photo = ImageTk.PhotoImage(enhance(region.resize((tw, th), resample), self.sat, self.gamma))
        canvas.delete("all")
        canvas.create_image(W // 2, H // 2, image=photo, anchor="center")
        return photo

    def _update_preview(self):
        if self.focus_idx is not None and self.focus_idx < len(self.slides):
            s = self.slides[self.focus_idx]
            zoom = t("zoom_pct", pct=self.prev_zoom * 100) if self.prev_zoom > 1.001 else ""
            self.prev_title.set(f"[{self.focus_idx + 1}/{len(self.slides)}] {s.name}{zoom}")
            if s.thumb is not None:
                self._prev_tk = self._draw_on(self.prev_canvas, s.thumb)
            else:
                self.prev_canvas.delete("all")
        else:
            self.prev_title.set(t("pick_slide"))
            self.prev_canvas.delete("all")
        if self.pinned is not None and self.pinned.rect is not None:
            hidden = "" if self.pinned in self.slides else t("pin_hidden")
            self.pin_title.set(t("pin_on", name=self.pinned.name, hidden=hidden))
            if self.pinned.thumb is not None:
                self._pin_tk = self._draw_on(self.pin_canvas, self.pinned.thumb)
            else:
                self.pin_canvas.delete("all")
            self.unpin_btn.state(["!disabled"])
        else:
            self.pinned = None
            self.pin_title.set(t("pin_none"))
            self.pin_canvas.delete("all")
            self.unpin_btn.state(["disabled"])

    # ---- zoom / pan on the preview ----
    def _reset_zoom(self):
        self.prev_zoom, self.prev_cx, self.prev_cy = 1.0, 0.5, 0.5
        self._update_preview()

    def _zoom_at(self, factor, mx=None, my=None):
        old = self.prev_zoom
        new = min(ZOOM_MAX, max(ZOOM_MIN, old * factor))
        if abs(new - old) < 1e-6:
            return
        if mx is not None and self.focus_idx is not None and self.focus_idx < len(self.slides):
            s = self.slides[self.focus_idx]
            if s.thumb is not None:
                W = max(self.prev_canvas.winfo_width(), 20)
                H = max(self.prev_canvas.winfo_height(), 20)
                w, h = s.thumb.size
                fit = min(W / w, H / h)
                # keep the image point under the cursor fixed
                ix = self.prev_cx * w + (mx - W / 2) / (fit * old)
                iy = self.prev_cy * h + (my - H / 2) / (fit * old)
                self.prev_cx = (ix - (mx - W / 2) / (fit * new)) / w
                self.prev_cy = (iy - (my - H / 2) / (fit * new)) / h
        self.prev_zoom = new
        self.prev_cx = min(max(self.prev_cx, 0.0), 1.0)
        self.prev_cy = min(max(self.prev_cy, 0.0), 1.0)
        self._update_preview()

    def _prev_press(self, e):
        self.focus_set()
        self._drag = (e.x, e.y, self.prev_cx, self.prev_cy)

    def _prev_drag(self, e):
        if self._drag is None or self.focus_idx is None or self.focus_idx >= len(self.slides):
            return
        s = self.slides[self.focus_idx]
        if s.thumb is None:
            return
        x0, y0, cx0, cy0 = self._drag
        W, H = max(self.prev_canvas.winfo_width(), 20), max(self.prev_canvas.winfo_height(), 20)
        w, h = s.thumb.size
        scale = min(W / w, H / h) * self.prev_zoom
        self.prev_cx = min(max(cx0 - (e.x - x0) / (w * scale), 0.0), 1.0)
        self.prev_cy = min(max(cy0 - (e.y - y0) / (h * scale), 0.0), 1.0)
        self._update_preview()

    # ---------------- block grouping (rename so matched pairs sort together) ----------
    def group_selected(self):
        if self.busy:
            self.status.set(t("busy"))
            return
        sel = [s for s in self.all_slides if s.selected]
        if not sel:
            self.status.set(t("group_pick_first"))
            return
        bad = [s.name for s in sel if block_of(s.name)[0] is None]
        if bad:
            messagebox.showerror(t("name_format_title"),
                                 t("name_format_msg", names="\n".join(bad[:8])), parent=self)
            return
        GroupDialog(self, sel)

    def apply_group_rename(self, pairs):
        """pairs: [(slide, new_name), ...] - rename on disk in a worker thread."""
        self.busy = True
        self.status.set(t("renaming", n=len(pairs)))
        log.info("group rename: %d file(s) in %s", len(pairs), self.src)
        threading.Thread(target=self._rename_worker, args=(pairs, self.src), daemon=True).start()

    def _rename_worker(self, pairs, folder):
        done, errors = [], []
        log_path = os.path.join(folder, "rename_log.csv")
        new_log = not os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            if new_log:
                wr.writerow(["time", "action", "from", "to"])
            for s, new_name in pairs:
                target = os.path.join(folder, new_name)
                if os.path.abspath(target) == os.path.abspath(s.path):
                    continue
                if os.path.exists(target):
                    log.error("  RENAME SKIPPED (already exists) %s", new_name)
                    errors.append(t("rename_exists", name=new_name))
                    continue
                try:
                    old_cache = cache_path(folder, s.path)     # before the rename
                except OSError:
                    old_cache = None
                try:
                    os.rename(s.path, target)
                except Exception as e:  # noqa
                    log.error("  RENAME FAILED %s: %s", s.name, e)
                    errors.append(f"{s.name}: {e}")
                    continue
                kept = move_cache(folder, old_cache, target)
                log.info("  renamed  %-45s -> %-45s (thumbnail cache %s)", s.name, new_name,
                         "moved" if kept else "none")
                wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), "rename", s.path, target])
                done.append((s, s.path, target))
                s.path, s.name = target, new_name
        self.q.put(("renamed", done, errors))

    def _on_rename_done(self, done, errors):
        self.busy = False
        if done:
            self.undo_stack.append(("rename", done))
        if errors:
            messagebox.showerror(t("rename_failed"), "\n".join(errors[:10]), parent=self)
        # the grouping is finished - drop the selection so the next pair can be picked right away
        for sl in self.all_slides:
            sl.selected = False
        self._apply_view()
        if done:                       # keep the new group in view so the result is visible
            first = done[0][0]
            if first in self.slides:
                self._set_focus(self.slides.index(first))
                self._scroll_to(self.focus_idx)
        self._style_all()
        self.status.set(t("rename_done", n=len(done)))


    # ---------------- move / undo (file I/O runs in a worker thread) ----------------
    def move_selected(self):
        if self.busy:
            self.status.set(t("busy"))
            return
        sel = [s for s in self.all_slides if s.selected]
        if not sel:
            self.status.set(t("nothing_selected"))
            return
        dst = self.dst_var.get().strip()
        if not dst:
            messagebox.showerror(t("error"), t("set_dst_first"))
            return
        if os.path.abspath(dst) == os.path.abspath(self.src):
            messagebox.showerror(t("error"), t("same_folder"))
            return
        if not same_drive(self.src, dst):
            if not messagebox.askyesno(t("diff_drive_title"), t("diff_drive_msg")):
                return
        os.makedirs(dst, exist_ok=True)

        # remove from the grid right away; the actual file move runs in the background
        visible_sel = [s for s in sel if s in self.slides]
        first = min((self.slides.index(s) for s in visible_sel), default=0)
        for s in sel:
            if self.pinned is s:
                self.pinned = None
            for item in (s.rect, s.img, s.txt, s.err_txt):
                if item is not None:
                    self.canvas.delete(item)
            s.rect = s.img = s.txt = s.err_txt = None
            s.tk_small = None
            self.all_slides.remove(s)
            if s in self.slides:
                self.slides.remove(s)
        self.focus_idx = min(first, len(self.slides) - 1) if self.slides else None
        self._layout()
        self._update_preview()
        self._update_count()
        self.busy = True
        self.status.set(t("moving", n=len(sel), dst=dst))
        log.info("move %d file(s) -> %s", len(sel), dst)
        threading.Thread(target=self._move_worker, args=(sel, dst), daemon=True).start()

    def _move_worker(self, sel, dst):
        moved, errors = [], []
        log_path = os.path.join(dst, "move_log.csv")
        new_log = not os.path.exists(log_path)
        with open(log_path, "a", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            if new_log:
                wr.writerow(["time", "action", "from", "to"])
            for s in sel:
                target = os.path.join(dst, s.name)
                base, ext = os.path.splitext(s.name)
                k = 1
                while os.path.exists(target):
                    target = os.path.join(dst, f"{base}_({k}){ext}")
                    k += 1
                try:
                    t0 = time.time()
                    shutil.move(s.path, target)
                    log.info("  moved    %-50s -> %s  (%.1fs)", s.name, target, time.time() - t0)
                except Exception as e:  # noqa
                    log.error("  MOVE FAILED %s: %s", s.name, e)
                    errors.append(f"{s.name}: {e}")
                    continue
                wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), "move", s.path, target])
                moved.append((s, s.path, target))
        self.q.put(("moved", moved, errors))

    def _on_move_done(self, moved, errors):
        self.busy = False
        if moved:
            self.undo_stack.append(("move", moved))
        if errors:
            messagebox.showerror(t("move_failed"), "\n".join(errors[:10]))
            self.rescan()
            return
        self.status.set(t("moved", n=len(moved), left=len(self.slides)))

    def undo(self):
        if self.busy:
            self.status.set(t("busy"))
            return
        if not self.undo_stack:
            self.status.set(t("nothing_undo"))
            return
        kind, batch = self.undo_stack.pop()
        self.busy = True
        what = t("undo_rename") if kind == "rename" else t("undo_move")
        log.info("undo (%s): %d file(s)", kind, len(batch))
        self.status.set(t("undoing", what=what, n=len(batch)))
        threading.Thread(target=self._undo_worker, args=(kind, batch), daemon=True).start()

    def _undo_worker(self, kind, batch):
        dst = os.path.dirname(batch[0][2])
        log_path = os.path.join(dst, "rename_log.csv" if kind == "rename" else "move_log.csv")
        restored, errors = 0, []
        with open(log_path, "a", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            for s, orig, target in batch:
                try:
                    if kind == "rename":
                        folder = os.path.dirname(target)
                        try:
                            old_cache = cache_path(folder, target)   # before the rename
                        except OSError:
                            old_cache = None
                        os.rename(target, orig)
                        move_cache(folder, old_cache, orig)
                        s.path, s.name = orig, os.path.basename(orig)
                    else:
                        shutil.move(target, orig)
                    log.info("  restored %-50s -> %s", os.path.basename(target), orig)
                    wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), "undo", target, orig])
                    restored += 1
                except Exception as e:  # noqa
                    log.error("  UNDO FAILED %s: %s", os.path.basename(target), e)
                    errors.append(f"{os.path.basename(target)}: {e}")
        self.q.put(("undone", restored, errors))

    def _on_undo_done(self, restored, errors):
        self.busy = False
        if errors:
            messagebox.showerror(t("undo_failed"), "\n".join(errors[:10]), parent=self)
        self.rescan()
        self.status.set(t("undo_done", n=restored))


class GroupDialog(tk.Toplevel):
    """Ask for a block-group tag and show exactly how each selected file will be renamed."""

    def __init__(self, app, slides):
        super().__init__(app)
        self.app, self.slides = app, slides
        self.title(t("dlg_group_title"))
        self.transient(app)
        self.resizable(True, False)

        acc = accession_of(slides[0].name)
        base, _ = block_of(slides[0].name)
        all_names = [sl.name for sl in app.all_slides]
        default = next_group_suffix(all_names, acc, base) if base else "_1"

        ttk.Label(self, text=t("dlg_group_info"), justify="left").pack(anchor="w", padx=12, pady=(12, 6))

        row = ttk.Frame(self)
        row.pack(fill=tk.X, padx=12)
        ttk.Label(row, text=t("dlg_suffix")).pack(side=tk.LEFT)
        self.suffix_var = tk.StringVar(value=default)
        ent = ttk.Entry(row, textvariable=self.suffix_var, width=10)
        ent.pack(side=tk.LEFT, padx=6)
        ttk.Label(row, text=t("dlg_suffix_hint"),
                  foreground="#606060").pack(side=tk.LEFT)
        self.suffix_var.trace_add("write", lambda *a: self._refresh())

        self.preview = tk.Text(self, height=min(14, max(4, len(slides) + 1)), width=96,
                               font=("Consolas", 9), wrap="none")
        self.preview.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.preview.tag_configure("same", foreground="#808080")
        self.preview.tag_configure("bad", foreground="#c00000")

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        self.ok = ttk.Button(btns, text=t("dlg_apply"), command=self._apply)
        self.ok.pack(side=tk.RIGHT)
        ttk.Button(btns, text=t("cancel"), command=self.destroy).pack(side=tk.RIGHT, padx=6)

        self._refresh()
        ent.focus_set()
        ent.select_range(0, "end")
        self.bind("<Return>", lambda e: self._apply())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def _plan(self):
        suffix = self.suffix_var.get().strip()
        out = []
        for sl in self.slides:
            new = apply_group(sl.name, suffix)
            out.append((sl, new))
        return suffix, out

    def _refresh(self):
        suffix, plan = self._plan()
        self.preview.delete("1.0", "end")
        taken = {sl.name for sl in self.app.all_slides} - {sl.name for sl, _ in plan}
        ok = False
        for sl, new in plan:
            if new is None:
                self.preview.insert("end", f"{sl.name}\n   -> {t('dlg_no_block')}\n", "bad")
            elif new == sl.name:
                self.preview.insert("end", f"{sl.name}\n   -> {t('dlg_no_change')}\n", "same")
            elif new in taken:
                self.preview.insert("end", f"{sl.name}\n   -> {new}   {t('dlg_exists')}\n", "bad")
            else:
                self.preview.insert("end", f"{sl.name}\n   -> {new}\n")
                ok = True
        bad = any(n is None or n in taken for _, n in plan)
        if not suffix:
            self.preview.insert("end", t("dlg_clearing"), "same")
        self.ok.state(["!disabled"] if (ok and not bad) else ["disabled"])

    def _apply(self):
        if "disabled" in self.ok.state():
            return
        _, plan = self._plan()
        pairs = [(sl, new) for sl, new in plan if new and new != sl.name]
        self.destroy()
        if pairs:
            self.app.apply_group_rename(pairs)


def main():
    setup_logging()
    if not (len(sys.argv) > 1 and sys.argv[1] == "--check"):
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleTitleW(
                    "WSI_Review - log window (do not close)"
                    if i18n.get_lang() == "en" else "WSI_Review - 기록 창 (닫지 마세요)")
                os.system("mode con: cols=100 lines=3000 >nul 2>&1")
            except Exception:  # noqa
                pass
        print(banner().format(log=LOG_FILE), flush=True)
    log.info("=== WSI Thumbnail Review started (log file: %s) ===", LOG_FILE)
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        folder = sys.argv[2] if len(sys.argv) > 2 else os.path.join(app_dir(), "WSI_example")
        check_folder(folder)
        return
    if sys.platform == "win32":
        try:                              # crisp rendering on HiDPI displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa
            pass
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(app_dir(), "WSI_example")
    if len(sys.argv) <= 1 and not os.path.isdir(src):
        src = ""                       # nothing to scan yet - the user picks a folder in the window
    dst = sys.argv[2] if len(sys.argv) > 2 else (os.path.join(src, "_removed") if src else "")
    try:
        App(src, dst).mainloop()
    except Exception:  # noqa - keep the traceback visible in the console window
        log.error("unhandled error:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
