# Contributing

Use English for code, documentation, interfaces, issues and pull requests.

Run `make install`, `make doctor` and `make tests`. Tests must use temporary state and simulated devices; do not require a real Kindle or network access to article websites. Calibre is required for the conversion integration test.

Keep prerequisite verification separate from key creation. Never introduce implicit key generation or a password bypass fallback. Unknown device states must remain inconclusive. New OTA recognition rules need evidence from the relevant firmware/method, failure-case tests and updated documentation.

If reporting device compatibility, include model, firmware, KOReader version and observed results. Remove serial numbers, private keys, tokens, personal reading content and network details. Do not upload your `.kindle-drop/` directory or raw device reports.

The modules are `app.py` (conversion, queue, HTTP and SFTP), `prerequisites.py` (USB and live checks), `cli.py` (commands and process coordination), plus packaged `static/` and `diagnostic/` assets. Browser changes should remain usable on a phone and keyboard accessible.
