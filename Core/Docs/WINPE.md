# IronDeploy WinPE

WinPE is the destructive deployment runtime. It boots independently of the
installed operating system, obtains an authorized deployment plan from
IronAPI, reads payloads from SMB, and applies Windows to the physical disk
explicitly selected by the operator.

## Safety

- Never run `Core\WinPE\Runtime\deploy.ps1` on the deployment server or a normal
  Windows workstation. The deployment path erases the selected physical disk.
- Build and servicing commands require elevation.
- Never mount a WIM a second time when DISM already reports it as mounted.
- Do not discard an unrelated DISM mount.
- Rebuild WIM or ISO artifacts only when a rebuild is explicitly intended.

The runtime checks that it is running from `X:` inside Windows PE, but this is
an additional guard, not permission to test the destructive script on a host.

## Source and generated artifacts

`Core\WinPE\Runtime` is the source of truth for files copied into the image:

| File | Purpose |
| --- | --- |
| `startnet.cmd` | Initializes WinPE and starts PowerShell in STA mode. |
| `deploy.ps1` | Loads the engine and GUI and handles the final reboot. |
| `IronDeploy.Engine.ps1` | Performs the deployment and reports progress through callbacks. |
| `IronDeploy.Gui.ps1` | Collects operator input and renders progress. |
| `deploy.config.ps1` | Holds non-secret runtime settings. |
| `diskpart-uefi.txt` | Defines the x64 UEFI/GPT layout and contains the validated target-disk placeholder. |

The build pipeline copies those files into the ADK working tree and produces:

```text
Core\WinPE\Runtime
      |
      v
Core\.work\WinPE_amd64\media\sources\boot.wim
      |
      +--> Core\dist\IronDeploy_PE.wim
      +--> Core\dist\IronDeploy_PE.iso
```

`Core\.work` and `Core\dist` are replaceable output, not source.

## Building WinPE manually

Use the public wrapper from the repository root. On a fresh working copy,
initialize the ADK tree and build both artifacts:

```powershell
.\Core\Tools\Build-IronDeployWinPE.ps1 -Initialize -Target Both
```

Later builds can target one artifact:

```powershell
.\Core\Tools\Build-IronDeployWinPE.ps1 -Target Wim
.\Core\Tools\Build-IronDeployWinPE.ps1 -Target Iso
.\Core\Tools\Build-IronDeployWinPE.ps1 -Target Both
```

Without `-Target`, the wrapper prompts for WIM, ISO, or both. A normal ISO
build first updates the WIM so current runtime files cannot be packaged over a
stale boot image. `-SkipWimUpdate` is only for repackaging immediately after a
successful WIM build.

Use `-UpdatePxeBundle` with `Wim` or `Both` to replace
`Core\dist\PXE\irondeploy\boot.wim` after the published WIM passes verification.

## Building through IronAPI

The IronAPI Image configuration page can start a WIM or ISO rebuild. IronAPI
runs the same `Core\Tools\Build-IronDeployWinPE.ps1` wrapper in a background process
and records its output under `Core\Logs\WinPEBuilds`.

IronAPI must itself be running as Administrator for this feature. Only one
WinPE build may run at a time. The web action does not provide the manual
`Both`, recovery, or PXE update options.

## Delivery and boot

- Publish `Core\dist\IronDeploy_PE.wim` through WDS/PXE when the environment boots
  WinPE over the network.
- Use `Core\dist\IronDeploy_PE.iso` for a VM, optical/USB media, or a compatible
  ISO boot manager. Ventoy should work with the generated ISO, but has not yet
  been tested with IronDeploy.

IronDeploy does not configure WDS, DHCP, firmware boot order, USB media, or
Ventoy. Those systems only need to deliver the generated WinPE artifact.

After boot, `startnet.cmd` runs:

```text
wpeinit
  -> powershell.exe -STA
     -> X:\IronDeploy\deploy.ps1
        -> IronDeploy.Engine.ps1
        -> IronDeploy.Gui.ps1
```

The GUI is the only supported deployment front-end. If WPF cannot start, the
console is restored for diagnostics and no fallback deployment begins.

## Runtime configuration

`Core\WinPE\Runtime\deploy.config.ps1` defines:

- the IronAPI base URL and certificate-validation policy;
- the drive letter and SMB paths used for images, drivers, and programs;
- the fallback Windows image index;
- the local-account policy applied during post-install;
- whether DISM image-apply progress is reflected in the GUI.

SetupWeb creates this file from `deploy.config.example.ps1`. It must not contain
SMB credentials, a WinPE authorization PIN, or browser credentials. Those
values remain server-side and are returned only when the active deployment is
authorized.

## What happens during deployment

Before disk modification, WinPE:

1. waits for networking and contacts the configured IronAPI;
2. reads the server-owned WinPE authorization policy;
3. authenticates by deployment account, PIN, or the explicitly configured
   credential-free mode;
4. enumerates physical disks and shows each disk's number, model, and size;
5. requests a computer-name suggestion and the allowed catalog;
6. requires the operator to select a target disk and confirm its permanent
   erase;
7. submits the target-disk snapshot plus the final image, index, driver,
   program, and domain-join selection;
8. receives a deployment ID, then requests a server-validated manifest;
9. receives the configured read-only SMB account details, checks the image
   size and the selected driver package's total size and INF count.

Only then does the destructive phase begin:

1. WinPE re-enumerates the selected disk and stops if its number, model, or
   size no longer matches the operator-confirmed snapshot.
2. WinPE releases any transient `C:` and `S:` mount points so a volume on a
   non-target disk cannot block the selected disk's Windows and EFI letters.
3. WinPE substitutes the validated disk number into a temporary DiskPart
   script, then erases and partitions that disk for UEFI/GPT.
4. DISM applies the selected Windows image.
5. DISM stages the selected driver package, when one was selected.
6. WinPE writes deployment state into `C:\IronDeploy`.
7. WinPE downloads and applies the authorized unattend file.
8. Optional ODJ data is provisioned by IronAPI and applied to offline Windows.
9. SetupComplete, post-install configuration, and selected installers are
   copied into the offline system.
10. WinPE compares each copied installer's SHA-256 with the value in the
   server-approved manifest.
11. `bcdboot` creates the UEFI boot files.
12. WinPE moves the deployment into its post-install phase and reboots.

DiskPart output is copied into the WinPE log. If DiskPart returns a non-zero
exit code, the exit code and final output lines are included in the deployment
error reported to IronAPI.

WinPE reports stage transitions, failures, and aggregate network diagnostics
to IronAPI. The details of what each API request returns belong in
[API.md](API.md).

## Post-install boundary

Windows Setup runs `Core\ServerTemplates\PostInstall\SetupComplete.cmd`, which
starts `postinstall.ps1` in the installed system. The script:

- installs the selected EXE/MSI programs and records their results;
- applies the configured local administrator policy;
- installs the configured IronAPI trust certificate when required;
- reports completion back to IronAPI;
- writes logs and a success marker under `C:\IronDeploy`;
- removes the persisted deployment token after the final report.

At that point WinPE is no longer running. IronAPI owns the deployment record
and final result.

## Mount-state recovery

Inspect DISM state before recovery:

```powershell
dism.exe /English /Get-MountedWimInfo
```

- No mounted images: run a normal build.
- The expected `Core\.work\WinPE_amd64\mount` is `Ok`: inspect it, then use
  `-Target Wim -UseExistingMount` only if committing it is intentional.
- The expected mount is `Invalid`: use `-RecoverInvalidMount`.
- Any unrelated mount is present: stop and leave it untouched.

Recovery discards only the expected invalid IronDeploy mount:

```powershell
.\Core\Tools\Build-IronDeployWinPE.ps1 `
  -Target Wim `
  -RecoverInvalidMount
```

## Verification

After a requested build, verify the requested files and the final mount state:

```powershell
Get-Item .\Core\dist\IronDeploy_PE.wim, .\Core\dist\IronDeploy_PE.iso `
  -ErrorAction SilentlyContinue |
  Select-Object FullName, Length, LastWriteTime

Get-FileHash .\Core\dist\IronDeploy_PE.wim -Algorithm SHA256
Get-FileHash .\Core\dist\IronDeploy_PE.iso -Algorithm SHA256
dism.exe /English /Get-MountedWimInfo
```

The requested artifacts must exist and be non-empty, hashes must be readable,
and DISM must report no mounted images after a normal completed build.
