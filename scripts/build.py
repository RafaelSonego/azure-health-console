"""Build the console for the machine you run this on: a single-file executable (no Python needed
to run it) and a wheel (for people who have Python). PyInstaller can't cross-build, so build on
each OS you want to support. Run from the repo root:  python scripts/build.py [--onedir]"""
import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIRS = ["healthconsole/profiles", "healthconsole/report_templates",
             "healthconsole/web/templates", "healthconsole/web/static"]


def run(*command):
    print("+", " ".join(str(c) for c in command))
    subprocess.run(command, check=True, cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onedir", action="store_true",
                        help="A folder instead of one file: starts faster, and antivirus flags it less often")
    args = parser.parse_args()

    version = (ROOT / "VERSION").read_text().strip()
    system = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), platform.system().lower())
    machine = platform.machine().lower().replace("x86_64", "x64").replace("amd64", "x64")
    name = f"azure-health-console-{version}-{system}-{machine}"
    dist, work = ROOT / "dist", ROOT / "build"

    run(sys.executable, "-m", "pip", "install", "--quiet", "-e", ".[build]")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--console",
        "--onedir" if args.onedir else "--onefile",
        "--name", name, "--distpath", dist, "--workpath", work, "--specpath", work,
        # keyring finds its OS backends through package metadata, which PyInstaller drops unless asked.
        "--collect-submodules", "keyring", "--copy-metadata", "keyring",
        *[arg for d in DATA_DIRS for arg in ("--add-data", f"{ROOT / d}{os.pathsep}{d}")],
        ROOT / "scripts" / "launcher.py")
    run(sys.executable, "-m", "pip", "wheel", "--quiet", "--no-deps", "-w", dist, ".")

    print("\nBuilt in", dist)
    for item in sorted(dist.iterdir()):
        size = sum(f.stat().st_size for f in item.rglob("*")) if item.is_dir() else item.stat().st_size
        print(f"  {item.name}  ({size / 2**20:.0f} MB)")
    if system == "macos":
        print("\nmacOS: unsigned builds are quarantined when downloaded. A colleague can allow it once with\n"
              f"  xattr -d com.apple.quarantine {name}")


if __name__ == "__main__":
    main()
