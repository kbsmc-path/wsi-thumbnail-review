"""
Build a standalone Windows .exe of the WSI thumbnail review tool (wsi_review.py) with PyInstaller.

The Python sources are left untouched - this only produces exe\\WSI_Review.exe, which runs without
Python. Console mode on purpose: Windows Smart App Control blocks PyInstaller's *windowed*
bootloader as an unsigned, unknown binary, while the console one is let through. The console
window also shows the log.

Usage:
    python -m pip install pyinstaller pillow
    python build_exe.py
"""
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "exe")
WORK = os.path.join(HERE, "build_tmp")
SCRIPT = "wsi_review.py"
NAME = "WSI_Review"

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
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", NAME,
        "--distpath", DIST,
        "--workpath", WORK,
        "--specpath", WORK,
    ]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    # i18n.py is imported from the same folder
    cmd += ["--paths", HERE, os.path.join(HERE, SCRIPT)]

    print(f"=== building {NAME}.exe ===")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=HERE)
    shutil.rmtree(WORK, ignore_errors=True)          # drop intermediate build files
    if r.returncode != 0:
        print(f"!! build FAILED (exit {r.returncode})")
        return 1
    out = os.path.join(DIST, NAME + ".exe")
    print(f"=== done in {time.time() - t0:.0f}s: {out}  ({os.path.getsize(out) / 1e6:.1f} MB) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
