# WinPE documentation moved

The WinPE build, runtime, recovery, and verification guide now lives in
[WINPE.md](WINPE.md).

Before changing or rebuilding the image, read that document completely.

Safety reminder: never execute `WinPE\Runtime\deploy.ps1` on the host. The
deployment path erases disk 0. Rebuild WIM or ISO artifacts only when a rebuild
was explicitly requested.
