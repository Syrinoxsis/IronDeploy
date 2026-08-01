# Codex Workspace Instructions

Before changing or rebuilding the IronDeploy WinPE image, read and follow:

- `Core\Docs\WINPE.md`

Important:

- Never execute `Core\WinPE\Runtime\deploy.ps1` on this machine. It wipes disk 0.
- Do not start IronAPI unless the user explicitly asks.
- Do not commit `.iso`, `.venv`, `.env`, Python caches, or other ignored files.
- Treat `Core\WinPE\Runtime` as the source of truth for files copied into WinPE.
- After modifying WinPE scripts, rebuild the ISO only when the user requests it.
