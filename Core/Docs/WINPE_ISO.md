# Building IronDeploy WinPE

## Safety

- Never run `WinPE\Runtime\deploy.ps1` on the host. It wipes disk 0.
- Run DISM and ISO creation elevated.
- Never mount an image a second time when DISM reports it as mounted.
- Never rebuild unless a new WIM or ISO was explicitly requested.
- The initializer must not remove .work\WinPE_amd64 while DISM reports mounted images.
- If a servicing command fails, inspect mount state before continuing.

## Paths

All build scripts derive the project root from their own location:

```text
WinPE\Runtime\                   source copied into WinPE
.work\WinPE_amd64\              mutable ADK working tree
.work\WinPE_amd64\mount\        DISM mount directory
.work\WinPE_amd64\media\sources\boot.wim
dist\IronDeploy_PE.wim          published PXE/WDS image
dist\IronDeploy_PE.iso          published ISO
```

The runtime source of truth is:

```text
WinPE\Runtime\deploy.ps1
WinPE\Runtime\deploy.config.ps1
WinPE\Runtime\diskpart-uefi.txt
WinPE\Runtime\startnet.cmd
```

## Normal builds

From the IronDeploy root:

On a fresh clone, prepare `.work` from the setup menu or initialize and build
both artifacts in one explicit command:

```powershell
.\Tools\Build-IronDeployWinPE.ps1 -Initialize -Target Both
```

`-Initialize` runs `copype` and always adds the required WinPE components. If
`.work\WinPE_amd64` already exists, it asks whether to reinstall that working
tree. Reinstall removes only `.work\WinPE_amd64`, and only when DISM reports no
mounted images.

Running the build wrapper without `-Target` prompts for what to rebuild:

```powershell
.\Tools\Build-IronDeployWinPE.ps1
```

```powershell
.\Tools\Build-IronDeployWinPE.ps1 -Target Wim
```

This updates and verifies `boot.wim`, then publishes
`dist\IronDeploy_PE.wim`. It does not build an ISO.

```powershell
.\Tools\Build-IronDeployWinPE.ps1 -Target Iso
```

This builds only `dist\IronDeploy_PE.iso` from the current `.work` tree. It
does not mount, modify, or publish `boot.wim`.

```powershell
.\Tools\Build-IronDeployWinPE.ps1 -Target Both
```

This updates `boot.wim`, publishes `dist\IronDeploy_PE.wim`, and builds
`dist\IronDeploy_PE.iso`.

Add `-UpdatePxeBundle` to replace `dist\PXE\irondeploy\boot.wim` after a
successful verified WIM or Both build.

## Mount-state recovery

Check mount state:

```powershell
dism.exe /English /Get-MountedWimInfo
```

Expected states:

- No mounted images: a normal build may proceed.
- The expected `.work\WinPE_amd64\mount` is `Ok`: inspect it, then use
  `-Target Wim -UseExistingMount` if it is safe to commit.
- The expected mount is `Invalid`: use `-RecoverInvalidMount`.
- Any unrelated mount is listed: stop. Do not modify or unmount it.

Recovery discards only the expected invalid IronDeploy mount:

```powershell
.\Tools\Build-IronDeployWinPE.ps1 `
  -Target Wim `
  -RecoverInvalidMount
```

## Manual verification

After a build:

```powershell
Get-Item .\dist\IronDeploy_PE.wim, .\dist\IronDeploy_PE.iso `
  -ErrorAction SilentlyContinue |
  Select-Object FullName, Length, LastWriteTime

Get-FileHash .\dist\IronDeploy_PE.wim -Algorithm SHA256
Get-FileHash .\dist\IronDeploy_PE.iso -Algorithm SHA256
dism.exe /English /Get-MountedWimInfo
```

Required final state:

- requested artifacts exist and are non-empty;
- SHA-256 hashes can be produced;
- DISM reports no mounted images;
- generated WIM/ISO files remain ignored by Git.
