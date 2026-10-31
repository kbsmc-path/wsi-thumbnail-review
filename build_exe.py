"""
Build a standalone Windows .exe of the WSI thumbnail review tool (wsi_review.py) with PyInstaller.

The Python sources are left untouched - this only produces exe\\WSI_Review_v<version>.exe (version
taken from __version__ in wsi_review.py, also embedded as the exe's file-version resource), which runs
without Python. Console mode on purpose: Windows Smart App Control blocks PyInstaller's *windowed*
bootloader as an unsigned, unknown binary, while the console one is let through. The console
window also shows the log.

Usage:
    python -m pip install pyinstaller pillow
    python build_exe.py
"""
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "exe")
WORK = os.path.join(HERE, "build_tmp")
SCRIPT = "wsi_review.py"
BASE_NAME = "WSI_Review"


def read_version():
    """__version__ from wsi_review.py without importing it (importing would pull in tkinter/PIL)."""
    with open(os.path.join(HERE, SCRIPT), encoding="utf-8") as fh:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', fh.read(), re.M)
    if not m:
        raise SystemExit(f"__version__ not found in {SCRIPT}")
    return m.group(1)


def write_version_file(version):
    """Windows version resource (shown in the exe's Properties > Details tab)."""
    parts = [int(x) for x in re.findall(r"\d+", version)][:4]
    parts += [0] * (4 - len(parts))
    tup = ", ".join(str(x) for x in parts)
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'kbsmc-path'),
      StringStruct('FileDescription', 'WSI Thumbnail Review'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', '{BASE_NAME}'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', '{BASE_NAME}_v{version}.exe'),
      StringStruct('ProductName', 'WSI Thumbnail Review'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, "version_info.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path

# modules the tool does not use - excluding them keeps the .exe small
EXCLUDES = [
    "numpy", "scipy", "matplotlib", "pandas", "IPython", "jupyter", "notebook",
    "pytest", "setuptools", "pydoc", "doctest", "unittest", "email", "http",
    "xml", "pdb", "PIL.ImageQt", "PyQt5", "PyQt6", "PySide2", "PySide6",
]


def main():
    try:
        import PyInstaller  # noqa
    except ImportError:
        print("PyInstaller is not installed. Run:\n    python -m pip install pyinstaller")
        return 1
    version = read_version()
    name = f"{BASE_NAME}_v{version}"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", name,
        "--distpath", DIST,
        "--workpath", WORK,
        "--specpath", WORK,
        "--version-file", write_version_file(version),
    ]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    # i18n.py is imported from the same folder
    cmd += ["--paths", HERE, os.path.join(HERE, SCRIPT)]

    print(f"=== building {name}.exe ===")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=HERE)
    shutil.rmtree(WORK, ignore_errors=True)          # drop intermediate build files
    if r.returncode != 0:
        print(f"!! build FAILED (exit {r.returncode})")
        return 1
    out = os.path.join(DIST, name + ".exe")
    print(f"=== done in {time.time() - t0:.0f}s: {out}  ({os.path.getsize(out) / 1e6:.1f} MB) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
