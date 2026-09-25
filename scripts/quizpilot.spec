# PyInstaller spec: builds dist/quizpilot/ (a folder, not --onefile, so the
# exe starts instantly instead of unpacking itself on every command).
from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["quizpilot_entry.py"],
    pathex=["../src"],
    datas=collect_data_files("quizpilot") + collect_data_files("playwright"),
    hiddenimports=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="quizpilot", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="quizpilot")
