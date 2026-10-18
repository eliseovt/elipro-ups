# Build with: python -m PyInstaller --noconfirm --clean packaging/elipro.spec
# The allowlist deliberately excludes data/, diagnostics and local configuration.
from pathlib import Path

project = Path(SPECPATH).parent
a = Analysis(
    [str(project / "packaging" / "elipro_entry.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project / "static"), "static")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Elipro",
    contents_directory="_internal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(project / "static" / "assets" / "elipro.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Elipro")
