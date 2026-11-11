# PyInstaller build of the desktop app: one file with a console window, which shows the address
# and stops the simulator when it's closed. From the project root:
#
#     pip install ".[app]"
#     pyinstaller packaging/elevator-sim.spec
#
# The result is dist/ElevatorSimulator (.exe on Windows). Build on each platform you ship for.

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    datas=[(str(ROOT / "web"), "web")],          # the UI, including web/samples/
    excludes=["tkinter", "unittest", "pydoc", "doctest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="ElevatorSimulator",
    console=True,
    upx=False,
)
