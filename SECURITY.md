# Security notes

KOReader's SSH server runs as root. A private key authorizes broad device access. Key authentication, a trusted initial pairing and a pinned SSH host fingerprint are required by this project. Password bypass is not used and is checked before transfers. Turn SSH off on unfamiliar networks.

OTA checks are a bounded observation of a known protection method, not a guarantee that firmware can never update. The project neither changes system binaries nor blocks Amazon network endpoints. Check the current documentation for your jailbreak, model and firmware.

Default web access is loopback only. API requests need a bearer token; Host validation blocks common DNS rebinding paths. LAN mode is opt-in and uses HTTP, so anyone who obtains its phone or OPDS link can access the corresponding interface/readings. Use only a trusted home LAN and never expose this port to the Internet.

URL imports accept public HTTP/HTTPS addresses and reject private destinations and credential-bearing URLs. Redirects are validated and connections use checked numeric addresses. Download, EPUB metadata and decompression sizes are bounded. Article conversion handles untrusted web content through Calibre; keep Calibre and dependencies maintained.

`.kindle-drop/` is private state: keys, tokens, device evidence and books. Its default location is inside the repo directory but it is ignored by Git. Keep it private and exclude it from shared archives. The diagnostic report is locally challenge-bound; it is not signed and cannot defend against a compromised PC or device.

Use GitHub's private vulnerability reporting if enabled on the repository. Otherwise, do not post keys, credentials or sensitive device reports in public issues.
