# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec.

What ships: the Python package and the built dashboard bundle.

What deliberately does NOT ship, and why: .env holds the builder's mail
password and API keys, outreach.db holds real people's names and addresses, and
assets/resume.pdf is a personal document with a phone number in it. A .exe is
trivially unpacked, so anything added here is public. Each is created on the
user's own machine by the setup console instead.
"""

import os
from pathlib import Path

ROOT = Path(os.getcwd())
DIST = ROOT / "dashboard" / "dist"
if not DIST.exists():
    raise SystemExit(
        "dashboard/dist missing. Build it first:\n"
        "  cd dashboard && npm install && npm run build"
    )

# Ship the compiled front end and a template config the user can start from.
datas = [(str(DIST), "dashboard"), (str(ROOT / ".env.example"), ".")]

for unsafe in (".env", "outreach.db", "assets/resume.pdf"):
    if any(str(ROOT / unsafe) == src for src, _ in datas):
        raise SystemExit(f"refusing to bundle {unsafe}: it is personal data")

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    # tzdata is loaded lazily by zoneinfo through importlib.resources, which
    # static analysis does not see. Named explicitly so the timezone database
    # is always collected into the build.
    hiddenimports=["dns.resolver", "bs4", "lxml", "requests", "dotenv", "tzdata"],
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="mailbot",
    debug=False,
    strip=False,
    upx=False,
    console=True,          # the console prints the URL and any setup errors
    disable_windowed_traceback=False,
)
