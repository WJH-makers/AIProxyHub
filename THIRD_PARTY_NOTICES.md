# Third-party notices

This file records the third-party components intentionally used or distributed by AIProxyHub. The exact dependency versions in a release artifact must be checked before publishing.

| Component | Use in AIProxyHub | License / source |
|---|---|---|
| CLIProxyAPI v7.2.129 | User-authorized provider gateway binary | MIT; https://github.com/router-for-me/CLIProxyAPI; full text in `licenses/CLIProxyAPI-LICENSE.txt` |
| zstandard 0.25.0 | Normalize client zstd-compressed requests | BSD 3-Clause; https://github.com/indygreg/python-zstandard; full text in `licenses/zstandard-LICENSE.txt` |
| PyInstaller 6.22.0 | Windows executable packaging tool | GPL with bootloader exception; https://pyinstaller.org/; full text in `licenses/PyInstaller-COPYING.txt` |

## Distribution rule

When a release contains a third-party binary or vendored source, the release archive must include the corresponding copyright and permission text under `licenses/`. A release must not rely on an unverified `latest` download without recording the selected upstream version and SHA-256. This repository does not grant access rights to any AI provider.
