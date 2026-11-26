# WSI Thumbnail Review

**Fast thumbnail review and sorting of Aperio `.svs` whole-slide images.**

A small desktop tool for cleaning up a folder of whole-slide images before analysis: flip through hundreds of
slides, spot duplicates, faint or misaligned serial sections, move them aside, and tag which H&E lines up with which
IHC section. It reads only the small thumbnail page embedded in each `.svs` (no full-resolution decode), so even
multi-GB slides appear in about a second, and the thumbnails are cached for instant re-opening.

![WSI Thumbnail Review](docs/screenshot.png)

## Features

- **Thumbnail grid** of every `.svs` / `.tif` / `.tiff` in a folder, cached in `<folder>/.wsi_thumbs`.
- **Sort and filter** — name, modified date, size; find by part of the file name.
- **Preview and compare** — wheel zoom / drag pan, and a *pinned reference* slide that zooms along with the preview,
  for checking orientation and alignment of serial sections.
- **Display correction** — saturation and gamma sliders for faint slides (view only; files are never changed).
- **Move with undo** — selected slides are moved to a target folder, logged to `move_log.csv`, undoable with Ctrl+Z.
- **Grouping** — tag matching H&E / IHC sections in the block field (`S 2024000123,A,.svs` → `S 2024000123,A_1,.svs`)
  so they sort together; logged to `rename_log.csv` and undoable.
- **Registration flags** — mark slides whose H&E / stain pair needs registration later (rotation, flip…), with an
  optional note. Saved at once to `review_flags.csv` in the source folder; the flag follows renames, moves and undo.
- **Export** — one `.xlsx` (sheets *slides*, *excluded*, *registration*, *renames*, *info*) plus a CSV per sheet
  (UTF-8 with BOM, opens in Excel with Korean intact). *excluded* is rebuilt from `move_log.csv`, so earlier
  sessions count too. No extra packages needed.
- **Korean / English** user interface (switch any time from the toolbar).

## Download (Windows, no Python needed)

Download `WSI_Review_v<version>.exe` from the [Releases](https://github.com/kbsmc-path/wsi-thumbnail-review/releases) page and
double-click it (or drop a folder onto it). A console window opens alongside — it is the log window; close the review
window to exit.

## Run from source

Requires **Python 3.8+** with tkinter (included in the python.org installer) and **Pillow**.

```bat
git clone https://github.com/kbsmc-path/wsi-thumbnail-review.git
cd wsi-thumbnail-review
python -m pip install -r requirements.txt
python wsi_review.py
```

On Windows you can also double-click `run.bat` or drag a folder onto it. Python does not have to be on PATH:
`run.bat` looks for `.venv` in this folder, the `py` launcher, `python` on PATH, and the usual install folders
(python.org per-user / all-users, `C:\Python3x`, Anaconda / Miniconda). If Pillow is missing it offers to install it
into a private `.venv` here (the system Python is left alone); with no Python at all it offers to start
`exe\WSI_Review_v*.exe` instead. `set WSI_REVIEW_CHECK=1` before running prints which Python it would use.
Without git: **Code › Download ZIP** and keep all files in one folder (`wsi_review.py` needs `i18n.py` and
`review_records.py`).

If something is missing, the program says what and how to install it instead of crashing, for example:

```
[!] The Pillow package is missing: No module named 'PIL'
    Install it from this folder, then run again:
        python -m pip install -r requirements.txt
```

| Missing | Install |
|---|---|
| Pillow | `python -m pip install -r requirements.txt` (or `python -m pip install pillow`) |
| tkinter (Linux) | Ubuntu/Debian `sudo apt install python3-tk` · Fedora `sudo dnf install python3-tkinter` |
| tkinter (macOS Homebrew) | `brew install python-tk` |
| `ImageTk` (Linux distro Pillow) | `sudo apt install python3-pil.imagetk` |
| `i18n.py`, `review_records.py` | download the whole repository, not just `wsi_review.py` |

## Usage

```
python wsi_review.py [source_folder] [target_folder]
python wsi_review.py --check <folder>      # print which TIFF page is used per file (no GUI)
```

| Input | Action |
|---|---|
| Click / Ctrl+Click / Shift+Click | focus (preview) / toggle selection / select range |
| Arrows, Home/End, PageUp/PageDown | move focus |
| Space · Ctrl+A · Esc | toggle selection · select all · clear |
| Enter or M | move selected files to the target folder |
| Ctrl+Z | undo last move / rename |
| P · Shift+P | pin focused slide as reference · release pin |
| G | group selected slides (block tag, e.g. `A` → `A_1`) |
| R · Shift+R · Ctrl+R | flag / unflag *needs registration* (selection, else focused) · flag with a note · show flagged pairs only |
| Ctrl+E | export the work to `review_export_<time>.xlsx` + CSVs in the source folder |
| + / − · Ctrl+wheel · size slider · F5 · Ctrl+F · 0 | thumbnail size · rescan · find by name · reset zoom |
| Wheel / drag / double-click on preview | zoom / pan / reset |
| F1 or ? | list of all shortcuts |

One-key shortcuts are ignored while you type in a folder or find box (Esc or a click on the grid returns to it).
The source / *Move to* boxes turn orange with a hint while they are empty, missing or identical, and the
*Move selected* button stays grey until something is selected (orange: the *Move to* folder still needs fixing).

Notes

- Moving between different drives is a copy + delete and is slow; use a target folder on the same drive.
- Filtered-out slides that are still selected are moved too — the status bar says how many.
- Everything (scan, errors, moves, undo) is also written to `wsi_review.log` next to the program.

## 한국어 요약

SVS 파일에 들어 있는 작은 썸네일만 읽어 수백 장의 슬라이드를 빠르게 훑어보고, 중복되거나 정렬이 맞지 않는
연속절편을 다른 폴더로 옮기거나(Ctrl+Z로 취소), H&E–면역염색 짝을 그룹으로 묶는 도구입니다.

- **exe로 실행**: [Releases](https://github.com/kbsmc-path/wsi-thumbnail-review/releases)에서 `WSI_Review_v<버전>.exe`를 받아 더블클릭
  (Python 설치 불필요). 함께 뜨는 검은 창은 기록 창입니다.
- **소스로 실행**: Python 3.8 이상 설치 후 이 폴더에서
  `python -m pip install -r requirements.txt` → `python wsi_review.py` (또는 `run.bat` 더블클릭).
  필요한 모듈이 없으면 무엇을 어떻게 설치해야 하는지 안내 창이 뜹니다.
- **`run.bat`**: Python이 PATH에 없어도 됩니다. 이 폴더의 `.venv`, `py` 실행기, PATH의 `python`, 일반 설치 폴더
  (python.org, `C:\Python3x`, Anaconda/Miniconda) 순으로 찾습니다. Pillow가 없으면 이 폴더의 `.venv`에 설치할지
  묻고, Python이 아예 없으면 `exe\WSI_Review_v*.exe`를 대신 실행할지 묻습니다.
- 사용 순서: 원본 폴더 지정 → [다시 스캔] → Ctrl+클릭으로 선택 → Enter로 이동 (Ctrl+Z 취소) → 필요하면 G로 그룹 지정.
  단축키는 위 Usage 표를 참고하세요.
- **정합(registration) 표시**: 추후 정합(회전·뒤집기 등)이 필요한 슬라이드에서 R (메모는 Shift+R). 원본 폴더의
  `review_flags.csv`에 바로 저장되고, 그룹 이름변경·이동·되돌리기를 따라갑니다. Ctrl+R = 표시된 쌍만 보기.
- **내보내기 (Ctrl+E)**: 원본 폴더에 `review_export_<시각>.xlsx`(시트: slides / excluded / registration / renames / info)와
  시트별 CSV를 만듭니다. excluded는 이동 폴더의 `move_log.csv`에서 되돌린 것을 뺀 목록이라 이전 작업도 포함됩니다.

## Build the .exe

```bat
python -m pip install pyinstaller pillow
python build_exe.py            :: -> exe\WSI_Review_v<version>.exe
```

Built as PyInstaller *onefile + console*: Windows Smart App Control blocks the unsigned windowed bootloader, and the
console doubles as the log window.

## License

[MIT](LICENSE)
