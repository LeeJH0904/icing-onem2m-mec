# tinyIoT patches (edge MN-CSE)

Applied by `scripts/build_tinyiot.sh` on top of tinyIoT commit `c140495`, in file-name order, with `git apply`.

| Patch | File | Why |
|---|---|---|
| `01-csr-registration-mobius4.patch` | `onem2m.c` | Registration to Mobius4 v4.24.5 failed with `400 dcse is not allowed`: tinyIoT's `<remoteCSE>` create carries `dcse`, `ast`, `spi`, which that Mobius4 version does not accept. With `INTEROP_R2A_CSR` defined (the build script defines it) they are left out. |
| `02-stability-main.patch` | `main.c` | Stability patch for long-running operation. |
| `03-stability-sqlite.patch` | `sqlite_implement.c` | Stability patch for long-running operation. |
| `04-stability-cin.patch` | `resources/cin.c` | Stability patch for long-running operation. |

Configuration (not a patch): `scripts/build_tinyiot.sh` sets `SERVER_TYPE MN_CSE`, port 3001,
`CSE_BASE_NAME` = `CSE_BASE_RI` = `EdgeMN` (they must be equal for IN→MN forwarding to land on the right path),
the registrar (`/Mobius4`, `Mobius`, host, 7599), SQLite, and `ALLOWED_REMOTE_CSE_ID "/*"`.

Patch 01 is an interoperability fix; patches 02–04 are stability patches applied for long-running demo
operation. All four are local to this deployment and have not been reviewed upstream.

License: these patches modify tinyIoT (BSD 3-Clause License, Copyright (c) 2024, tinyIoT Contributors) and are
provided under the same BSD 3-Clause terms. tinyIoT itself is not redistributed here; `scripts/build_tinyiot.sh`
clones it from https://github.com/seslabSJU/tinyIoT.
