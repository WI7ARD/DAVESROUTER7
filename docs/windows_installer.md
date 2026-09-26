# Windows Installer

A standard Windows setup wizard built with **NSIS 3** (Nullsoft Scriptable Install
System, Modern UI 2), wrapping a **PyInstaller** bundle of the app.

![Installer pages: welcome, components, finish, uninstall options](images/windows-installer.png)

*Screenshots from a test build running under Wine. That build uses a stub payload, so
the "Space required" figure is not the real size.*

## What the user gets

| Wizard page | Content |
|---|---|
| Welcome | What the app does and does not do; no admin rights needed |
| License | MIT license |
| Components | App (required); **Start menu shortcut**; **Desktop shortcut**; **"Open with" entry for `.kicad_pcb`** (all ticked by default) |
| Install location | Default `%LOCALAPPDATA%\Programs\AI PCB Router` |
| Finish | Option to start the app |

The uninstaller is available from Settings ▸ Apps ▸ Installed apps, or as `Uninstall.exe` in
the install folder. It asks one question: whether to also delete **your settings,
logs, workspaces and saved API keys**. The default is to keep them, so a reinstall
picks up where you left off.

Installed files:

```
%LOCALAPPDATA%\Programs\AI PCB Router\
  AI PCB Router.exe     the desktop app (windowed; no console window)
  pcbrouter.exe         the same program as a console app, for the command line
  Uninstall.exe
  _internal\            Python runtime, Qt, dependencies (shared by both .exe files)
```

`pcbrouter.exe` is not added to `PATH`, because NSIS cannot edit `PATH` safely. Call it by
its full path, e.g.
`"%LOCALAPPDATA%\Programs\AI PCB Router\pcbrouter.exe" board.kicad_pcb --inspect`.

## Design decisions

- **Per-user install, no UAC prompt.** Settings, logs and API keys (Windows Credential
  Manager) are per-user anyway. An all-users install would have to run elevated, and when
  a standard user types an administrator's password, "current user" becomes the admin
  account, which puts shortcuts and registry entries in the wrong profile. Per-user
  avoids that problem.
- **The uninstaller deletes an exact file list.** The build generates the list of every
  installed file. The uninstaller deletes those files and removes folders only when they
  are empty. It never runs `RMDir /r` on the install folder, which the user may have
  pointed at a folder containing other files. The only recursive deletes target the
  app's own data folders (`%APPDATA%\AI PCB Router` and `%LOCALAPPDATA%\AI PCB Router`),
  and only when the user ticks the option.
- **"Open with", never the default.** The app is added to Explorer's *Open with* list
  for `.kicad_pcb` (a per-user ProgID plus an `OpenWithProgids` value). KiCad stays the
  program that opens boards on double-click. The uninstaller removes only its own
  value and never deletes the `.kicad_pcb` key. Using `/ifempty` there would be unsafe,
  because it checks subkeys but not values, so it could delete KiCad's registration.
- **API keys are not left behind.** When you choose to remove your data, the
  uninstaller runs `pcbrouter.exe --forget-api-keys` first. That command deletes each
  configured profile's entry from Windows Credential Manager.
- **Upgrades.** Running a newer setup detects the existing install and runs the old
  version's own uninstaller silently. That uninstaller knows exactly which files it
  installed. User data is kept.
- **Running-app check.** Setup and the uninstaller check whether the app is running by
  trying to open its `.exe` for writing (Windows locks the image of a running
  executable). If it is running, they show Retry/Cancel. In silent mode they exit with
  code 5.
- **64-bit Windows 10 1809 or later.** This is the minimum for Qt 6. The bundled
  Python and Qt are 64-bit. Setup itself is a 32-bit program, like most NSIS installers,
  because the official Windows build of NSIS ships only 32-bit stubs. Setup therefore
  checks for 64-bit Windows and uses the 64-bit registry view explicitly.
- **No UPX compression.** UPX-packed executables often trigger antivirus false
  positives.

## Silent install and uninstall (IT / scripts)

```bat
AI-PCB-Router-<version>-Setup-x64.exe /S                      :: default folder
AI-PCB-Router-<version>-Setup-x64.exe /S /D=C:\Tools\PCBRouter :: /D last, unquoted
"%LOCALAPPDATA%\Programs\AI PCB Router\Uninstall.exe" /S                  :: keep user data
"%LOCALAPPDATA%\Programs\AI PCB Router\Uninstall.exe" /S /REMOVEUSERDATA  :: remove it too
```

Exit codes: `0` success; `1` cancelled by the user; `2` aborted; `3` unsupported
Windows version; `4` another setup is running; `5` the app is running.

## Building the installer

On Windows 10/11, from the repository root:

```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[ai,packaging]"
winget install NSIS.NSIS            :: or: choco install nsis
python packaging\windows\build_installer.py
```

Output: `dist\installer\AI-PCB-Router-<version>-Setup-x64.exe`. The script prints the
file's SHA-256.

Pipeline (`packaging/windows/`):

| File | Role |
|---|---|
| `build_installer.py` | Reads the version from `pcbrouter.__version__` → runs PyInstaller → generates `files_install.nsh` / `files_uninstall.nsh` → runs `makensis -WX` (warnings are errors) |
| `pcbrouter.spec` | PyInstaller: one analysis, two executables (GUI + console), shared `_internal` |
| `installer.nsi` | The NSIS wizard |
| `make_assets.py` | Draws the icon and wizard bitmaps with Qt (output is committed and reproducible) |
| `smoke_test.ps1` | Installs, exercises the installed app, and uninstalls (used by CI) |

**CI:** `.github/workflows/windows-installer.yml` builds the installer on
`windows-latest` and runs `smoke_test.ps1`. The smoke test checks:
- the frozen bundle loads Qt, both AI SDKs and keyring;
- Windows Credential Manager is available;
- the CLI inspects a board, and the GUI opens one;
- the uninstaller refuses while the app runs;
- a clean uninstall.

The installer is uploaded as a workflow artifact. The same workflow runs the full test
suite on Windows.

**Linux:** the NSIS part is testable there (`apt install nsis wine wine32:i386`, after
`dpkg --add-architecture i386`), because makensis
runs on Linux:

```bash
pytest tests/unit/test_windows_packaging.py tests/integration/test_windows_installer.py
PCBROUTER_TEST_WINE=1 pytest tests/integration/test_windows_installer.py   # runs Setup under Wine
```

The Wine test covers the full lifecycle, using stub executables in place of the real
bundle:
1. Fresh install.
2. Upgrade.
3. Uninstall that keeps user data. A file the user added to the install folder
   survives, and KiCad's own `.kicad_pcb` registration is untouched.
4. Uninstall with `/REMOVEUSERDATA`. The test checks that `--forget-api-keys` was called.
5. Install into a custom folder.

## Known limitations

- **Not code-signed.** Windows SmartScreen shows "Windows protected your PC" for an
  unsigned download. Click *More info ▸ Run anyway*. Signing needs a code-signing
  certificate, which costs money and requires identity verification.
- No all-users (per-machine) install mode.
- `pcbrouter.exe` is not added to `PATH`.
- The "app is running" check covers the app installed in the target folder, not copies
  run from elsewhere.
- English only.
