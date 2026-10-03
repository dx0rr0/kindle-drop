# Setup and verification

## 1. Install the PC tools

Install [Python 3.12 or later](https://www.python.org/downloads/), [Calibre](https://calibre-ebook.com/download), GNU Make and Git. Use a native terminal: PowerShell/CMD on Windows, or your normal terminal on Linux/macOS.

- Windows: Python must be callable from your terminal or supplied as `make install PYTHON="C:/path/to/python.exe"`. GNU Make for Windows (for example GnuWin32 Make or a suitable package from your package manager) must be on PATH. The default Calibre installation under Program Files is discovered automatically.
- macOS: use a GNU Make package or the `make` supplied by your developer tools; the normal `/Applications/calibre.app` installation is detected.
- Linux: install GNU Make and Calibre through your preferred distribution/package installation method; `ebook-convert` should be on PATH. Use `PYTHON=python3` when needed.

From the repository directory run `make install` and `make doctor`. Installation creates `.venv/` locally; it does not change system Python packages. Both must succeed before moving on. If dependencies are missing, `make install` resolves them. All Make recipes use Python, so Unix utilities are not required on Windows.

## 2. Prepare an already jailbroken Kindle

Keep Wi-Fi off until OTA protection has been checked. Consult the installation instructions for your exact model and firmware:

- [KOReader installation on Kindle](https://github.com/koreader/koreader/wiki/Installation-on-Kindle-devices)
- [Kindle Modding documentation](https://kindlemodding.org/)
- [KOReader SSH documentation](https://github.com/koreader/koreader/wiki/SSH)

Required layout is `/mnt/us/koreader`, with `reader.lua`, `git-rev`, `dropbear`, `sftp-server`, and `plugins/SSH.koplugin/main.lua`. KUAL must already work. This MVP does not automate jailbreak/hotfix installation or changes to OTA services.

The supported OTA proof is firmware 5.11+ with both `/usr/bin/otaupd` and `/usr/bin/otav3` absent and their regular `.bck` files present, with neither updater running and no pending update downloads. This is a deliberately limited recognition rule, based on the [community OTA status checker](https://github.com/neura-neura/Check-OTA-status). Older directory-block methods, `.bak` variants, network blocking alone and unfamiliar layouts are inconclusive here. An inconclusive result must be investigated, rather than skipped.

In KOReader, leave **Login without password** disabled. Stop SSH and disable **Start SSH server automatically** during initial USB setup. Keep autostart off for normal use too; the MVP does not edit KOReader preferences.

## 3. Run the staged diagnostic

Connect USB and choose the mount explicitly, e.g. `D:/` on Windows or `/media/your-user/Kindle` on Linux. Do not point it at an ordinary folder:

```sh
make check MOUNT=D:/
```

The command inspects the selected volume, hashes firmware/KOReader files and installs `extensions/kindle-drop-check/`. Its only device writes are that diagnostic and a random challenge. No SSH key is created and no firmware/OTA files are modified.

Safely eject USB, run **KUAL → Kindle Drop → Check prerequisites (read-only)**, then reconnect USB. The script reads system state and writes only `/mnt/us/kindle-drop-report.txt` (with a temporary file alongside it). It checks:

| Evidence | Required result |
| --- | --- |
| Report format and challenge | Current schema and exact nonce from this PC's check |
| KUAL execution | UID 0 |
| Firmware and SSH installation | Hashes agree with the inspected USB volume |
| OTA binary layout | Originals absent; both `.bck` files present |
| OTA processes and pending downloads | None detected |
| Dropbear and SFTP binaries | Executable on the Kindle |
| Password bypass preference/runtime | Off |
| User storage | Writable and at least 64 MB free |
| Report freshness | Prepared less than 24 hours ago, using PC time |

Run `make verify MOUNT=D:/`. Missing, malformed, stale, mismatched or failed fields stop setup. The report is evidence of the checks at that moment, not a cryptographically signed attestation. The physical USB connection and your PC are trusted.

## 4. Create the key only after a passing report

```sh
make key MOUNT=D:/
```

The command re-runs verification immediately before creating an ECDSA P-256 key. It installs only the public key under `koreader/settings/SSH/authorized_keys`. Existing entries are preserved, with a local backup before appending. Re-running the command reuses this PC's existing key and does not duplicate its entry.

The unencrypted private key is in `.kindle-drop/kindle_key`. On Unix it is created with mode 0600; on Windows it inherits the user directory's access permissions. Keep the state directory private. Do not copy it to the Kindle, commit it, or share it. The directory also contains bearer tokens, reading content, device fingerprints and diagnostic evidence. Git ignores it.

## 5. Pair and send wirelessly

Safely eject USB. Connect to a trusted LAN, start KOReader SSH with password bypass off, then use the IP and port shown on the reader:

```sh
make pair HOST=192.168.1.25
make pair HOST=192.168.1.25 FINGERPRINT=SHA256:THE_DISPLAYED_FINGERPRINT
make test
make send INPUT="path/to/book.epub"
make open
```

Initial pairing uses trust on first use: the first command shows a fingerprint, the second explicitly accepts it. Check the reader's IP on a trusted network before accepting. Subsequent connections pin that fingerprint. Never accept a changed fingerprint without understanding whether your IP was reassigned or the SSH host key legitimately changed.

Every authenticated connection checks the firmware and SSH file hashes, live OTA binary state, pending updates, running OTA processes and actual Dropbear command line. Wi-Fi may need re-enabling after USB eject; the Kindle must be awake. Reserve the Kindle's IP through your router if it changes frequently.

## Troubleshooting

- **Python or Make not found:** open a fresh terminal after installation; use an explicit `PYTHON` executable for `make install`.
- **Calibre not found:** set `EBOOK_CONVERT` to its executable path; article conversion needs it. Refer to the [ebook-convert manual](https://manual.calibre-ebook.com/generated/en/ebook-convert.html).
- **Missing prerequisites:** install/fix them on the Kindle using its current model-specific guide; this tool does not bypass a failed check.
- **No report:** the diagnostic must run from KUAL on the reader after ejecting USB. Check the KUAL menu and `/mnt/us/kindle-drop-report.txt`.
- **Challenge expired or files changed:** run `make check` and the KUAL diagnostic again, then verify. A previous free-form OTA report cannot substitute for this schema.
- **OTA unknown:** stop setup and inspect the OTA method. Do not rename system binaries merely to satisfy the checker.
- **Authentication rejected:** verify the public entry, password bypass off, and restart KOReader SSH. The tool never enables passwordless login as a workaround.
- **Timeout/refused connection:** check reader IP, port 2222, Wi-Fi and sleep state. USB must be ejected for normal Kindle network operation.
- **State busy:** stop `make open` before changing setup. `make send` automatically uses the running server.
- **Port occupied:** use `make open WEB_PORT=8770`.
- **Article extraction fails:** use a publicly accessible full article or supply an EPUB. No login/paywall bypass is attempted.

The optional LAN web server needs your operating system's firewall to allow its selected port from your trusted network. Default PC-only mode requires no inbound LAN rule.
