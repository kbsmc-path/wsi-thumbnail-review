"""
Review records for WSI Thumbnail Review: registration flags and the work export.

Pure logic only - no tkinter, no Pillow, no `xml` package (build_exe.py excludes `xml` from the
.exe, so the .xlsx writer below builds its XML by hand). Everything here can be run and tested
without the GUI.

Registration flags
    A slide can be flagged as "needs registration later" (rotation, flip, ...) with an optional
    note. Flags live in a sidecar file in the source folder, written on every change:

        <source>/review_flags.csv   (utf-8-sig, opens in Excel)
        file, note, status, moved_to, flagged_at, updated_at

    status: active   - the file is in the source folder
            excluded - the file was moved to the 'Move to' folder (moved_to = its path there)
            missing  - the file is gone from the source folder for another reason
    The flag follows group renames and their undo, and a move / undo-move only changes the status,
    so the fact that a flagged slide was excluded is kept.

Export
    One call writes, into the source folder by default:
        review_export_<stamp>.xlsx               sheets: slides, excluded, registration, renames, info
        review_export_<stamp>_<sheet>.csv        one utf-8-sig CSV per sheet (except info)
    'excluded' is rebuilt from move_log.csv in the 'Move to' folder (moves minus undos), so work
    from earlier sessions is included too.
"""
import csv
import os
import re
import time
import zipfile

FLAGS_FILE = "review_flags.csv"
FLAG_COLUMNS = ["file", "note", "status", "moved_to", "flagged_at", "updated_at"]
ACTIVE, EXCLUDED, MISSING = "active", "excluded", "missing"
MOVE_LOG = "move_log.csv"
RENAME_LOG = "rename_log.csv"
EXPORT_PREFIX = "review_export_"
SLIDE_EXTS = (".svs", ".tif", ".tiff")

# quick-add notes offered in the note dialog (kept in English so the data is the same in both UIs)
NOTE_PRESETS = ["rotate 90", "rotate 180", "rotate 270", "flip horizontal", "flip vertical"]

# --- file name fields: mirrors split_fields / block_of in wsi_review.py ----------------------
BLOCK_FIELD = 1
GROUP_RE = re.compile(r"^(?P<base>.*?)(?P<suffix>_\d+)?$")


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def parse_name(name):
    """Fields of "S 2024000123,A_1,PD-L1[22C3+],x.svs" -> accession, block (with group tag), ..."""
    stem, _ = os.path.splitext(name)
    parts = stem.split(",")
    out = {"accession": parts[0], "block": "", "block_base": "", "group_suffix": "",
           "stain_field": "", "stain": "", "extra": "", "has_block": len(parts) > BLOCK_FIELD}
    if out["has_block"]:
        blk = parts[BLOCK_FIELD].strip()
        m = GROUP_RE.match(blk)
        out.update(block=blk, block_base=m.group("base"), group_suffix=m.group("suffix") or "")
    if len(parts) > 2:
        out["stain_field"] = parts[2]
        out["stain"] = parts[2].strip() or "H&E"          # an empty stain field means H&E
    if len(parts) > 3:
        out["extra"] = ",".join(parts[3:])
    return out


def pair_key(name):
    """Slides with the same accession + block field (group tag included) form one pair / group.
    None when the name has no block field (it can only pair with itself)."""
    f = parse_name(name)
    if not f["has_block"]:
        return None
    return f"{f['accession'].strip()}|{f['block']}".casefold()


def pair_label(name):
    f = parse_name(name)
    return f"{f['accession'].strip()},{f['block']}" if f["has_block"] else name


# ============================================================================ flags
class FlagStore:
    """Registration flags of one source folder, keyed by file name (case-insensitive)."""

    def __init__(self, folder):
        self.folder = folder
        self.entries = {}          # name.casefold() -> dict(FLAG_COLUMNS)
        self.load_error = None

    @property
    def path(self):
        return os.path.join(self.folder, FLAGS_FILE) if self.folder else None

    @classmethod
    def load(cls, folder):
        st = cls(folder)
        p = st.path
        if not p or not os.path.exists(p):
            return st
        try:
            for row in read_csv_rows(p):
                name = (row.get("file") or "").strip()
                if not name:
                    continue
                e = {c: (row.get(c) or "").strip() for c in FLAG_COLUMNS}
                e["file"] = name
                e["status"] = e["status"] or ACTIVE
                st.entries[name.casefold()] = e
        except (OSError, csv.Error, UnicodeDecodeError) as ex:
            st.load_error = str(ex)
        return st

    def save(self):
        """Rewrite the sidecar atomically. Raises OSError if the folder is not writable."""
        p = self.path
        if not p:
            return
        tmp = p + ".tmp"
        rows = sorted(self.entries.values(), key=lambda e: e["file"].casefold())
        with open(tmp, "w", newline="", encoding="utf-8-sig", errors="replace") as fh:
            wr = csv.writer(fh)
            wr.writerow(FLAG_COLUMNS)
            for e in rows:
                wr.writerow([e.get(c, "") for c in FLAG_COLUMNS])
        os.replace(tmp, p)

    # ---- queries ----
    def get(self, name):
        return self.entries.get(name.casefold())

    def is_flagged(self, name):
        e = self.get(name)
        return e is not None and e["status"] == ACTIVE

    def note(self, name):
        e = self.get(name)
        return e["note"] if e else ""

    def count_active(self):
        return sum(1 for e in self.entries.values() if e["status"] == ACTIVE)

    def active_pair_keys(self):
        return {pair_key(e["file"]) for e in self.entries.values()
                if e["status"] == ACTIVE and pair_key(e["file"]) is not None}

    # ---- changes (callers save() afterwards) ----
    def flag(self, names, note=None):
        """Flag `names`. note=None keeps an existing note."""
        ts = now_str()
        for n in names:
            e = self.get(n)
            if e is None:
                e = {c: "" for c in FLAG_COLUMNS}
                e.update(file=n, flagged_at=ts)
                self.entries[n.casefold()] = e
            e.update(status=ACTIVE, moved_to="", updated_at=ts)
            e["file"] = n
            if note is not None:
                e["note"] = note.strip()

    def unflag(self, names):
        for n in names:
            self.entries.pop(n.casefold(), None)

    def toggle(self, names):
        """All flagged -> unflag all; otherwise flag all. Returns True if they are now flagged."""
        if names and all(self.is_flagged(n) for n in names):
            self.unflag(names)
            return False
        self.flag([n for n in names if not self.is_flagged(n)])
        return True

    def rename(self, old, new):
        e = self.entries.pop(old.casefold(), None)
        if e is None:
            return False
        e["file"] = new
        e["updated_at"] = now_str()
        self.entries[new.casefold()] = e
        return True

    def mark_moved(self, name, target):
        e = self.get(name)
        if e is None:
            return False
        e.update(status=EXCLUDED, moved_to=target, updated_at=now_str())
        return True

    def mark_restored(self, name):
        e = self.get(name)
        if e is None:
            return False
        e.update(status=ACTIVE, moved_to="", updated_at=now_str())
        return True

    def reconcile(self, present_names):
        """Match the flags to the files actually in the folder (after a scan). Returns True if
        anything changed. Nothing is deleted: a vanished flagged file becomes 'missing'."""
        present = {n.casefold() for n in present_names}
        changed = False
        for k, e in self.entries.items():
            here = k in present
            st = e["status"]
            if st == ACTIVE and not here:
                e["status"] = MISSING
            elif st == MISSING and here:
                e["status"] = ACTIVE
            elif st == EXCLUDED and here and not (e["moved_to"] and os.path.exists(e["moved_to"])):
                e.update(status=ACTIVE, moved_to="")      # put back by hand, outside the tool
            else:
                continue
            e["updated_at"] = now_str()
            changed = True
        return changed


# ============================================================================ logs
def read_csv_rows(path):
    """DictReader rows of a CSV written by this tool - or re-saved by Excel (BOM / cp949)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return list(csv.DictReader(text.splitlines()))


def net_moves(rows):
    """move_log rows (time, action, from, to) -> files still excluded: moves minus their undos.
    An undo row is written as (undo, <path in target folder>, <original path>)."""
    live = {}                      # normalised target path -> entry
    for r in rows:
        action = (r.get("action") or "").strip().lower()
        src, dst = (r.get("from") or "").strip(), (r.get("to") or "").strip()
        if not src or not dst:
            continue
        if action == "move":
            live[_norm(dst)] = {"moved_at": (r.get("time") or "").strip(),
                                "original_path": src, "current_path": dst}
        elif action == "undo":
            live.pop(_norm(src), None)
    return sorted(live.values(), key=lambda e: e["moved_at"])


# ============================================================================ export data
SLIDE_COLS = ["file", "accession", "block", "block_base", "group_suffix", "stain", "stain_field",
              "other_fields", "pair_key", "pair_count", "pair_partners", "registration",
              "registration_note", "pair_registration", "size_bytes", "size_mb", "modified",
              "thumbnail", "path"]
EXCL_COLS = ["file", "moved_as", "accession", "block", "stain", "pair_key", "moved_at",
             "original_path", "current_path", "exists_now", "from_this_source",
             "registration", "registration_note"]
REG_COLS = ["pair_key", "accession", "block", "flagged_files", "notes", "flag_status",
            "pair_files_in_source", "pair_files_excluded", "n_in_source", "flagged_at",
            "updated_at"]
RENAME_COLS = ["time", "action", "from_name", "to_name", "from_path", "to_path"]


def scan_slides(folder):
    """Slide infos straight from the folder (when the GUI's list is not at hand)."""
    out = []
    for de in os.scandir(folder):
        if de.is_file() and de.name.lower().endswith(SLIDE_EXTS):
            st = de.stat()
            out.append({"name": de.name, "size": st.st_size, "mtime": st.st_mtime,
                        "error": None, "loaded": None})
    return out


def _thumb_state(sl):
    if sl.get("error"):
        return "error: " + str(sl["error"])
    if sl.get("loaded") is None:
        return ""
    return "ok" if sl["loaded"] else "not loaded yet"


def _ts(mtime):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else ""


def collect(src, dst, slides=None, flags=None):
    """Build every export table. Never raises for missing / unreadable logs: those become notes.

    slides: [{name, size, mtime, error, loaded}] for the files currently in `src`
            (None = scan the folder). flags: FlagStore of `src` (None = load it).
    Returns {"sheets": [(name, columns, rows)], "notes": [{"code", "text", ...}]}.
    """
    notes = []
    if slides is None:
        slides = scan_slides(src)
    if flags is None:
        flags = FlagStore.load(src)
    if flags.load_error:
        notes.append({"code": "flags_unreadable", "path": flags.path, "e": flags.load_error,
                      "text": f"{FLAGS_FILE} could not be read: {flags.load_error}"})
    slides = sorted(slides, key=lambda s: s["name"].casefold())

    # ---- excluded: from the move log ----
    excluded = []
    log_path = os.path.join(dst, MOVE_LOG) if dst else None
    if not dst:
        notes.append({"code": "no_dst", "text": "no 'Move to' folder set - the excluded sheet is empty"})
    elif not os.path.exists(log_path):
        notes.append({"code": "no_move_log", "path": log_path,
                      "text": f"no move log: {log_path} - the excluded sheet is empty"})
    else:
        try:
            excluded = net_moves(read_csv_rows(log_path))
        except (OSError, csv.Error) as ex:
            notes.append({"code": "log_unreadable", "path": log_path, "e": str(ex),
                          "text": f"move log unreadable: {log_path}: {ex}"})

    src_n = _norm(src)
    stores = {src_n: flags}

    def store_for(folder):
        k = _norm(folder)
        if k not in stores:
            stores[k] = FlagStore.load(folder)
        return stores[k]

    for e in excluded:
        orig = e["original_path"]
        e["file"] = os.path.basename(orig)
        e["from_this_source"] = _norm(os.path.dirname(orig)) == src_n
        fl = store_for(os.path.dirname(orig)).get(e["file"])
        e["flag"] = fl if (fl and (fl["status"] == EXCLUDED or
                                    (fl["moved_to"] and _norm(fl["moved_to"]) == _norm(e["current_path"])))) else None

    # ---- groups (pairs) ----
    by_key = {}
    for sl in slides:
        k = pair_key(sl["name"])
        if k is not None:
            by_key.setdefault(k, []).append(sl["name"])
    excl_by_key = {}
    for e in excluded:
        k = pair_key(e["file"])
        if k is not None and e["from_this_source"]:
            excl_by_key.setdefault(k, []).append(e["file"])
    flagged_keys = flags.active_pair_keys()

    slide_rows = []
    for sl in slides:
        n = sl["name"]
        f = parse_name(n)
        k = pair_key(n)
        members = by_key.get(k, [n]) if k else [n]
        fe = flags.get(n)
        flagged = fe is not None and fe["status"] == ACTIVE
        slide_rows.append([
            n, f["accession"], f["block"], f["block_base"], f["group_suffix"], f["stain"],
            f["stain_field"], f["extra"], pair_label(n) if k else "", len(members),
            " | ".join(m for m in members if m != n),
            "Y" if flagged else "", fe["note"] if flagged else "",
            "Y" if (k in flagged_keys) else "",
            int(sl.get("size") or 0), round((sl.get("size") or 0) / 1e6, 1), _ts(sl.get("mtime")),
            _thumb_state(sl), os.path.join(src, n),
        ])

    excl_rows = []
    for e in excluded:
        f = parse_name(e["file"])
        k = pair_key(e["file"])
        fl = e["flag"]
        excl_rows.append([
            e["file"], os.path.basename(e["current_path"]), f["accession"], f["block"], f["stain"],
            pair_label(e["file"]) if k else "", e["moved_at"], e["original_path"], e["current_path"],
            "yes" if os.path.exists(e["current_path"]) else "no",
            "yes" if e["from_this_source"] else "no",
            "Y" if fl else "", fl["note"] if fl else "",
        ])

    # ---- registration: one row per flagged pair ----
    groups = {}
    for fe in flags.entries.values():
        k = pair_key(fe["file"]) or ("file|" + fe["file"].casefold())
        groups.setdefault(k, []).append(fe)
    reg_rows = []
    for k, fes in sorted(groups.items(), key=lambda kv: kv[1][0]["file"].casefold()):
        fes.sort(key=lambda e: e["file"].casefold())
        first = fes[0]["file"]
        f = parse_name(first)
        real_key = pair_key(first)
        in_src = by_key.get(real_key, []) if real_key else [e["file"] for e in fes if e["status"] == ACTIVE]
        reg_rows.append([
            pair_label(first) if real_key else first, f["accession"], f["block"],
            " | ".join(e["file"] for e in fes),
            " | ".join(f"{e['file']}: {e['note']}" for e in fes if e["note"]),
            " | ".join(f"{e['file']}: {e['status']}" for e in fes),
            " | ".join(in_src),
            " | ".join(excl_by_key.get(real_key, [])) if real_key else "",
            len(in_src),
            min(e["flagged_at"] for e in fes), max(e["updated_at"] for e in fes),
        ])

    # ---- renames: the source folder's rename log ----
    ren_rows = []
    rlog = os.path.join(src, RENAME_LOG)
    if os.path.exists(rlog):
        try:
            for r in read_csv_rows(rlog):
                a, b = (r.get("from") or "").strip(), (r.get("to") or "").strip()
                ren_rows.append([(r.get("time") or "").strip(), (r.get("action") or "").strip(),
                                 os.path.basename(a), os.path.basename(b), a, b])
        except (OSError, csv.Error) as ex:
            notes.append({"code": "log_unreadable", "path": rlog, "e": str(ex),
                          "text": f"rename log unreadable: {rlog}: {ex}"})

    not_loaded = sum(1 for s in slides if s.get("loaded") is False and not s.get("error"))
    if not_loaded:
        notes.append({"code": "not_loaded", "n": not_loaded,
                      "text": f"{not_loaded} thumbnail(s) were still loading - 'thumbnail' is blank for them"})

    info = [["exported_at", now_str()], ["source_folder", src], ["move_to_folder", dst or ""],
            ["move_log", log_path or ""], ["slides_in_source", len(slide_rows)],
            ["excluded_net", len(excl_rows)], ["flagged_slides_active", flags.count_active()],
            ["flagged_pairs", len(reg_rows)], ["rename_log_rows", len(ren_rows)]]
    info += [["note", n["text"]] for n in notes]

    sheets = [("slides", SLIDE_COLS, slide_rows), ("excluded", EXCL_COLS, excl_rows),
              ("registration", REG_COLS, reg_rows), ("renames", RENAME_COLS, ren_rows),
              ("info", ["key", "value"], info)]
    return {"sheets": sheets, "notes": notes}


# ============================================================================ writers
def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8-sig", errors="replace") as fh:
        wr = csv.writer(fh)
        wr.writerow(columns)
        wr.writerows(rows)


# ---- minimal .xlsx (Office Open XML) writer: inline strings, one bold header row ----
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")
_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_NS_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_HDR = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
MAX_CELL = 32767


def _esc(s):
    s = _BAD_XML.sub("", str(s))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def col_letter(i):
    """0 -> A, 25 -> Z, 26 -> AA"""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _width(v):
    s = str(v)
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def _sheet_xml(columns, rows, selected):
    ncol = max(1, len(columns))
    last = f"{col_letter(ncol - 1)}{len(rows) + 1}"
    widths = []
    for c in range(ncol):
        w = _width(columns[c]) if c < len(columns) else 4
        for r in rows[:2000]:
            if c < len(r):
                w = max(w, _width(r[c]))
        widths.append(min(max(w + 2, 6), 70))
    out = [_HDR, f"<worksheet {_NS} {_NS_R}>", f'<dimension ref="A1:{last}"/>',
           '<sheetViews><sheetView workbookViewId="0"' + (' tabSelected="1"' if selected else "") + ">"
           '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
           '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView></sheetViews>',
           '<sheetFormatPr defaultRowHeight="15"/><cols>']
    out += [f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)]
    out.append("</cols><sheetData>")
    for ri, row in enumerate([columns] + list(rows), start=1):
        cells = []
        for ci, v in enumerate(row):
            ref = f"{col_letter(ci)}{ri}"
            st = ' s="1"' if ri == 1 else ""
            if v is None or v == "":
                continue
            if isinstance(v, bool):
                v = "TRUE" if v else "FALSE"
            if isinstance(v, (int, float)) and v == v and abs(v) != float("inf"):
                cells.append(f'<c r="{ref}"{st}><v>{v}</v></c>')
            else:
                s = _esc(v)[:MAX_CELL]
                cells.append(f'<c r="{ref}"{st} t="inlineStr"><is><t xml:space="preserve">{s}</t></is></c>')
        out.append(f'<row r="{ri}">' + "".join(cells) + "</row>")
    out.append("</sheetData>")
    out.append(f'<autoFilter ref="A1:{last}"/>')
    out.append('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>')
    out.append("</worksheet>")
    return "".join(out), last


def safe_sheet_names(names):
    out, seen = [], set()
    for n in names:
        base = (_BAD_SHEET.sub("_", n).strip("'") or "Sheet")[:31]
        cand, k = base, 2
        while cand.casefold() in seen:
            suf = f" ({k})"
            cand = base[:31 - len(suf)] + suf
            k += 1
        seen.add(cand.casefold())
        out.append(cand)
    return out


def write_xlsx(path, sheets):
    """sheets: [(name, columns, rows)]. Values: str / int / float (None or '' = empty cell)."""
    names = safe_sheet_names([s[0] for s in sheets])
    n = len(sheets)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ct = [_HDR, '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
          '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
          '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>']
    ct += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
           for i in range(1, n + 1)]
    ct.append("</Types>")
    rels = (_HDR + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            "</Relationships>")
    core = (_HDR + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            "<dc:title>WSI review export</dc:title><dc:creator>WSI Thumbnail Review</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified></cp:coreProperties>')
    app = (_HDR + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           "<Application>WSI Thumbnail Review</Application></Properties>")
    styles = (_HDR + f"<styleSheet {_NS}>"
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
              '<fills count="3"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FFDDEBF7"/><bgColor indexed="64"/></patternFill></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
              "</styleSheet>")
    sheet_xml, defined = [], []
    for i, (_, cols, rows) in enumerate(sheets):
        x, last = _sheet_xml(cols, rows, selected=(i == 0))
        sheet_xml.append(x)
        q = names[i].replace("'", "''")
        lc, lr = re.match(r"([A-Z]+)(\d+)", last).groups()
        defined.append(f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                       f"'{_esc(q)}'!$A$1:${lc}${lr}</definedName>")
    wb = (_HDR + f"<workbook {_NS} {_NS_R}>"
          '<bookViews><workbookView activeTab="0"/></bookViews><sheets>'
          + "".join(f'<sheet name="{_esc(nm)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, nm in enumerate(names))
          + "</sheets><definedNames>" + "".join(defined) + "</definedNames></workbook>")
    wb_rels = (_HDR + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                         f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1))
               + f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
               "</Relationships>")
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels", rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", styles)
        for i, x in enumerate(sheet_xml, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", x)
    os.replace(tmp, path)


def export_all(src, dst, slides=None, flags=None, out_dir=None, stamp=None):
    """Collect and write the .xlsx plus one CSV per sheet. Returns (written_paths, notes).
    Raises OSError if `out_dir` (default: `src`) is not writable."""
    data = collect(src, dst, slides, flags)
    out_dir = out_dir or src
    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    base = os.path.join(out_dir, EXPORT_PREFIX + stamp)
    k = 2
    while os.path.exists(base + ".xlsx"):
        base = os.path.join(out_dir, f"{EXPORT_PREFIX}{stamp}_{k}")
        k += 1
    written = [base + ".xlsx"]
    write_xlsx(written[0], data["sheets"])
    for name, cols, rows in data["sheets"]:
        if name == "info":
            continue
        p = f"{base}_{name}.csv"
        write_csv(p, cols, rows)
        written.append(p)
    return written, data["notes"]
