print("""Kindle Drop
  make install             Create a virtual environment and install dependencies
  make doctor              Check PC prerequisites
  make check MOUNT=D:/      Inspect USB and install the read-only KUAL diagnostic
  make verify MOUNT=D:/     Verify its fresh report after running it on the Kindle
  make key MOUNT=D:/        Reverify, create a key, install only its public portion
  make pair HOST=...        Inspect the SSH host fingerprint; repeat with FINGERPRINT=...
  make test                Check key authentication and live prerequisites
  make send URL            Convert a public article and send its EPUB
  make send books/*.epub    Send matching EPUB files
  make send INPUT=\"...\"    Send one URL or path with spaces/query parameters
  make open                Start the web app and open your browser (Ctrl+C stops it)
  make open LAN=1           Also allow a phone on the same trusted network
  make tests               Run isolated integration tests

Use PYTHON=python3 if that is your Python 3.12+ executable.
On Linux/macOS, replace D:/ with your Kindle USB mount path.
Read docs/setup.md before enabling Kindle Wi-Fi.
""")
