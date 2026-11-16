# PyInstaller spec for the bundled local workbench (run by build.sh from the repository root).
# -*- mode: python -*-
import os

import pgserver
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(".")
PG = os.path.dirname(pgserver.__file__)

a = Analysis(
    [os.path.join(ROOT, "desktop", "sidecar", "entry.py")],
    pathex=[ROOT],
    datas=[(os.path.join(PG, "pginstall"), "pgserver/pginstall"),
           *collect_data_files("ontology"), *collect_data_files("workbench")],
    hiddenimports=[*collect_submodules("pgserver"), *collect_submodules("uvicorn"),
                   *collect_submodules("ontology"), *collect_submodules("workbench")],
    excludes=["tkinter", "matplotlib", "IPython", "pytest"],
)

# PyInstaller gives every shared library one top-level name that all code loads it by. pgserver's tools and psycopg
# both ship libpq.5.dylib, and the alias picked pgserver's older one (PostgreSQL 16.2), which lacks symbols psycopg
# needs ("Symbol not found: _PQcancelBlocking"). Point the alias at psycopg's copy: libpq is backward compatible,
# so initdb and pg_dump run on it too.
def prefer_psycopg_libpq(toc):
    out = []
    for dest, src, kind in toc:
        if kind == "SYMLINK" and dest == "libpq.5.dylib" and src.startswith("pgserver/"):
            src = "psycopg_binary/.dylibs/libpq.5.dylib"
        out.append((dest, src, kind))
    return out


a.binaries = prefer_psycopg_libpq(a.binaries)
a.datas = prefer_psycopg_libpq(a.datas)  # PyInstaller 6 keeps the aliases (symlinks) with the data
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="onto-workbench",
    console=True,
    upx=False,
    strip=False,
)
