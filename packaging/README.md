# Packaging d4extract for Windows

This directory holds everything needed to turn the Python source into a
single double-clickable `d4extract.exe` for end users.

## Quick build

From the repo root, in an activated venv that has d4extract installed
in editable mode (`pip install -e .[dev]`):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

The output lands in `dist\d4extract.exe`. Expect a 400–800 MB file —
PySide6 + VTK are heavy.

To also bundle a rustydemon-cli binary next to the .exe (so users do
not have to install it themselves):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 `
    -RustydemonExe C:\path\to\rustydemon-cli.exe
```

## Files

- **`d4extract.spec`** — the PyInstaller spec. Single source of truth
  for hidden imports, data files, and Qt module exclusions.
- **`build.ps1`** — PowerShell driver: installs PyInstaller if needed,
  invokes it, optionally bundles rustydemon-cli, and prints a size
  summary.

## When things go wrong

**`ModuleNotFoundError: vtkmodules.<something>` at runtime.** A new VTK
submodule is being loaded dynamically and PyInstaller missed it. Add
the name to the `hiddenimports` list in `d4extract.spec` and rebuild.

**The .exe launches and immediately closes with no window.** Run it
from a terminal (`.\dist\d4extract.exe`) to see the traceback —
`--windowed` swallows stdout/stderr otherwise. Common culprit is a
missing Qt platform plugin; check the `pyside6` data files were
collected.

**SmartScreen blocks the .exe.** Expected for unsigned binaries from a
new publisher. Users click "More info → Run anyway". Document this in
the user-facing README. Code-signing certs are $200–$400/year and the
only real fix.

**The .exe is 1 GB+.** Check the `excludes` list in the spec —
PySide6 has a lot of optional modules (QtWebEngine alone is ~300 MB).
You can also pass `--exclude-module <name>` on the command line to
test exclusions before committing them to the spec.

## What is not yet here

- Inno Setup installer script (turns the dist folder into a proper
  `Setup.exe` with Start Menu entry and uninstaller).
- GitHub Actions workflow for automated builds on tagged releases.
- Code-signing wiring.

See `docs/packaging_plan.md` for the full deployment roadmap.
