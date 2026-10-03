# Kindle Drop

Send a public article URL or an EPUB from your computer to a Kindle running KOReader, over your home Wi-Fi. Use Make from the terminal or a small local web app. Calibre converts extracted article text into EPUB; SSH/SFTP delivers it and verifies its SHA-256 checksum.

```sh
make send https://example.org/article
make send books/*.epub
make send INPUT="books/A book with spaces.epub"
make open
```

This is an MVP for **already jailbroken Kindles**. It does not install a jailbreak, KOReader, KUAL or an OTA blocker. First-time setup uses USB for the diagnostic and public key; normal reading transfers are wireless.

## Prerequisites

| Computer | Kindle |
| --- | --- |
| Python 3.12+, GNU Make, Git to clone the repo | A working jailbreak and its current hotfix |
| Calibre, including `ebook-convert` | KOReader installed at `/mnt/us/koreader` |
| Dependencies installed by `make install` | KUAL able to execute extensions as root |
| Same trusted LAN as the Kindle for sending | Firmware 5.11+ with OTA binaries disabled using the recognized `.bck` rename method |
| A browser for `make open` | KOReader's SSH plugin, Dropbear and SFTP server; password bypass disabled |

The checker verifies the requirements it can observe. Successful root execution shows that KUAL/jailbreak access works at that moment; it cannot prove every jailbreak/hotfix detail or guarantee future firmware behavior. Other OTA protection methods currently stop at an inconclusive result. Read [the setup guide](docs/setup.md) for installation links, limitations and troubleshooting.

## First-time setup

Clone this repository and enter its directory. Then:

```sh
make install                # use PYTHON=python3 if needed
make doctor
make check MOUNT=D:/        # Windows example; choose your actual Kindle USB mount
```

Safely eject the Kindle. Keep Wi-Fi off. Run **KUAL → Kindle Drop → Check prerequisites (read-only)**. Reconnect USB:

```sh
make verify MOUNT=D:/
make key MOUNT=D:/
```

`make check` creates no SSH key. `make verify` checks a fresh challenge-bound report. `make key` repeats verification before generating an ECDSA key, backs up existing `authorized_keys`, and appends only the public key. The private key stays in the ignored local `.kindle-drop/` directory. Unknown or failed checks prevent key creation.

Safely eject USB. Connect the Kindle to your trusted Wi-Fi. In **KOReader → Tools → Network → SSH server**, disable **Login without password** and **Start SSH server automatically**, then start the server. Read its IP and port:

```sh
make pair HOST=192.168.1.25 SSH_PORT=2222
# Check that the IP is the one shown on your Kindle. Inspect the displayed fingerprint.
make pair HOST=192.168.1.25 SSH_PORT=2222 FINGERPRINT=SHA256:THE_DISPLAYED_FINGERPRINT
make test
```

The first pairing command only displays the server fingerprint. The second authenticates with your key, checks live prerequisites and saves the pairing. Initial fingerprint acceptance relies on a trusted local network; the device report does not independently authenticate the SSH host key. Later connections reject a changed fingerprint.

## Everyday use

Keep the computer and Kindle on the same trusted LAN, with KOReader's SSH server running and the Kindle awake:

```sh
make send https://example.org/article
make send INPUT="https://example.org/article?a=1&b=2"
make send books/*.epub
make send first.epub second.epub
make open
```

Find delivered books in **`/mnt/us/documents/KindleDrop`** using KOReader's file browser. Existing identical files are reused; a different file with the same name is never overwritten. `make send` uses the running web app when available, so both interfaces share one queue safely.

`make open` opens your browser and keeps the server in the terminal. Paste a URL, choose or drop EPUBs, then click **Send to Kindle**. Press Ctrl+C to stop the server. Readings can be prepared before pairing, but sending requires completed setup.

For optional phone access, stop the server and run:

```sh
make open LAN=1
```

Open the private phone link printed in the terminal on a phone connected to the same trusted Wi-Fi. The PC must remain running. The app also exposes a private OPDS catalog URL for KOReader downloads. LAN access uses HTTP with a bearer token; treat those links like passwords and use it only on a trusted LAN. Default mode listens only on this computer.

## Configuration and development

`MOUNT`, `HOST`, `SSH_PORT`, `FINGERPRINT`, `WEB_PORT`, `LAN` and `INPUT` are Make variables. To isolate multiple readers, set `KINDLE_DROP_DATA` to a different private state directory. Set `EBOOK_CONVERT` or `CALIBRE` to an executable path if Calibre is not found automatically.

```sh
make help
make tests
make open WEB_PORT=8770
# The CLI is also available without Make after installation:
kindle-drop --help
kindle-drop send "books/A book.epub"
kindle-drop open --lan
```

Terminal Make arguments containing spaces should use `INPUT="..."`. For a literal `$` in a Make variable, write `$$`, or use the direct CLI. Globs expand inside Python as well as in Unix shells.

The package contains the transport/conversion core, CLI, prerequisite checker, KUAL diagnostic, and static browser UI. Tests use isolated state, simulated USB volumes and a real local SSH/SFTP server. CI runs on Windows, Linux and macOS; the actual device path has previously been exercised on a Paperwhite 3, firmware 5.16.2.1.1, KOReader v2025.08. Other devices need their own validation.

Article conversion preserves headings, paragraphs, emphasis, lists, quotations, code, tables and captions. Images are downloaded, normalized to reader-compatible PNGs, and embedded for offline reading; responsive light variants are preferred. Reader-friendly typography replaces the website's layout. If an image fails, its original link and a warning are included. Images are bounded to 32 per article, 8 MB each and 32 MB total.

Limitations: JavaScript-only pages, logins, DRM, paywalls and interactive layouts are not supported. EPUBs are limited to 40 MB. This project does not block Amazon endpoints or alter firmware. Turn SSH off before joining unfamiliar networks. Re-run the diagnostic after firmware or KOReader changes.

MIT licensed. See [CONTRIBUTING.md](CONTRIBUTING.md) and [the security notes](SECURITY.md).
