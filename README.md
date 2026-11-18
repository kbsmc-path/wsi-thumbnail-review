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
- **Korean / English** user interface (switch any time from the toolbar).

## Install and run

Requires **Python 3.8+** with tkinter (included in the python.org installer) and **Pillow**. The program is
distributed as source only — there is no `.exe` download (see [Build your own .exe](#build-your-own-exe-optional)
for why).

**Windows, without git**

1. Install Python 3.8+ from [python.org](https://www.python.org/downloads/).
2. On this page: **Code › Download ZIP**.
3. Right-click the ZIP › **Properties** › at the bottom tick **Unblock** › **OK** — *before* extracting
   (see [If Windows blocks it](#if-windows-blocks-it-smart-app-control)).
4. Extract the ZIP and double-click `run.bat` (or drag a folder onto it). The first run installs Pillow
   (`python -m pip`); later runs start immediately. A console window opens alongside — it is the log window; close
   the review window to exit.

Keep all files in one folder (`wsi_review.py` needs `i18n.py`). `run.bat` uses `.venv` if present, otherwise the
`python` on PATH or the `py` launcher.

**With git, or on Linux / macOS**

```bat
git clone https://github.com/kbsmc-path/wsi-thumbnail-review.git
cd wsi-thumbnail-review
python -m pip install -r requirements.txt
python wsi_review.py
```

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
| `i18n.py` | download the whole repository, not just `wsi_review.py` |

## If Windows blocks it (Smart App Control)

On Windows 11 with **Smart App Control** turned on, files downloaded from the internet are marked, and a marked
`run.bat` is refused with *"Smart App Control blocked …"* / *"Dangerous file extension from the web"*. Unblocking the
ZIP before extracting it (step 3 above) removes the mark from everything inside.

Already extracted without unblocking? Right-click `run.bat` › Properties › tick **Unblock**, or in PowerShell in that
folder: `Get-ChildItem | Unblock-File`.
Use `python -m pip …`, not `pip …`: Smart App Control can also block the unsigned `pip.exe` launcher.

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
| + / − · F5 · Ctrl+F · 0 | thumbnail size · rescan · find by name · reset zoom |
| Wheel / drag / double-click on preview | zoom / pan / reset |

Notes

- Moving between different drives is a copy + delete and is slow; use a target folder on the same drive.
- Filtered-out slides that are still selected are moved too — the status bar says how many.
- Everything (scan, errors, moves, undo) is also written to `wsi_review.log` next to the program.

## 한국어 요약

SVS 파일에 들어 있는 작은 썸네일만 읽어 수백 장의 슬라이드를 빠르게 훑어보고, 중복되거나 정렬이 맞지 않는
연속절편을 다른 폴더로 옮기거나(Ctrl+Z로 취소), H&E–면역염색 짝을 그룹으로 묶는 도구입니다.

- **설치와 실행** (Windows): exe 파일은 배포하지 않고 소스로만 제공합니다.
  1. [python.org](https://www.python.org/downloads/)에서 Python 3.8 이상 설치
  2. 이 페이지에서 **Code › Download ZIP**으로 받기
  3. **압축을 풀기 전에** ZIP 우클릭 › **속성** › 아래쪽 **차단 해제** 체크 › **확인**
  4. 압축을 풀고 `run.bat` 더블클릭 (처음 한 번은 Pillow를 자동 설치하고, 다음부터는 바로 실행).
     함께 뜨는 검은 창은 기록 창입니다.

  직접 실행하려면 이 폴더에서 `python -m pip install -r requirements.txt` → `python wsi_review.py`.
  필요한 모듈이 없으면 무엇을 어떻게 설치해야 하는지 안내 창이 뜹니다.
- **"스마트 앱 컨트롤이 차단했습니다"가 뜰 때** (Windows 11): 인터넷에서 받은 파일이라 `run.bat`이 막힌 것입니다.
  3번(차단 해제)을 건너뛰고 압축을 풀었다면 `run.bat`만 우클릭 › 속성 › 차단 해제 하면 됩니다.
  `pip` 대신 항상 `python -m pip`를 쓰세요 (`pip.exe`도 차단될 수 있습니다).
- 사용 순서: 원본 폴더 지정 → [다시 스캔] → Ctrl+클릭으로 선택 → Enter로 이동 (Ctrl+Z 취소) → 필요하면 G로 그룹 지정.
  단축키는 위 Usage 표를 참고하세요.

## Build your own .exe (optional)

No `.exe` is published: it would not be code-signed, and Windows Smart App Control blocks unsigned executables it does not know
(whether built as console or windowed) with no way for the user to allow them. For PCs where Smart App Control is
off, you can build one yourself:

```bat
python -m pip install pyinstaller pillow
python build_exe.py            :: -> exe\WSI_Review_v<version>.exe
```

Built as PyInstaller *onefile + console*; the console doubles as the log window.

## License

[MIT](LICENSE)
