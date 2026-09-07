# IronDeploy

**Deploy Windows 10 and Windows 11 to bare-metal PCs through a simple WinPE interface.**

> [!CAUTION]
> **IronDeploy is Alpha software.** Test it on a virtual machine or disposable
> computer before using it on production hardware. The physical disk selected
> by the operator is permanently erased during deployment.

IronDeploy automates the repetitive work involved in preparing a new or erased
computer. Boot the target PC into IronDeploy WinPE, choose the Windows image,
target disk, drivers, software, computer name, and optional domain join, then
start the deployment.

IronAPI acts as the control centre: it manages the available deployment content,
builds the deployment plan, receives progress throughout the process, and keeps
the final results searchable in a web dashboard. WinPE stays lightweight and
performs the actual deployment on the target computer.

![How IronDeploy WinPE and IronAPI work together](Core/Docs/screenshots/irondeploy-overview.webp)

## How it works

1. **Prepare the deployment server**
   Configure IronAPI and add Windows images, driver packages, post-install
   software, and deployment settings.

2. **Boot the target computer**
   Start the generated IronDeploy WinPE image through WDS/PXE, USB, or ISO.

3. **Choose the deployment**
   Select the Windows image, target disk, drivers, optional software, computer
   name, and whether the computer should join Active Directory.

4. **Deploy Windows**
   IronDeploy prepares the disk, applies the Windows image, injects drivers,
   writes the Windows answer file, applies Offline Domain Join when selected,
   and prepares the system for its first boot.

5. **Complete and review**
   Windows installs the selected post-install software after the first boot.
   Progress, timings, warnings, and final results are reported to IronAPI.

## Key features

- Windows 10 and Windows 11 deployment from WinPE;
- WIM and ESD image support, including ESD-to-WIM conversion;
- offline driver injection using centrally managed driver packages;
- unattended post-install software;
- configurable computer-name formats and automatic name suggestions;
- Offline Domain Join for Active Directory environments;
- searchable deployment history, stage timings, warnings, and software results;
- network link-speed and deployment-path diagnostics;
- per-user access control and dedicated WinPE authorization;
- fully on-premises operation.

## Project scope

IronDeploy focuses on deploying Windows to physical computers. It covers the
bare-metal deployment part of tools such as Microsoft Deployment Toolkit without
requiring the surrounding MDT toolchain.

It does **not** capture or clone an already configured reference computer, and it
is not intended to replace every MDT component. IronDeploy uses standard Windows
installation images and applies the selected configuration during deployment.

## Interface overview

### WinPE deployment interface

The operator can review the detected hardware, choose the target physical disk,
select the available deployment options, and follow the current stage.

![IronDeploy WinPE computer name, Windows image, and target disk selection](<Core/Docs/screenshots/WINPE_choiseName and disk.png>)

### Deployment dashboard

The dashboard keeps recent runs searchable by computer, hardware, image, status,
stage, and start time. Each deployment includes stage timings, warnings, network
diagnostics, and post-install software results.

![IronDeploy dashboard showing deployment status and recent runs](Core/Docs/screenshots/dashboard.PNG)

<details>
<summary>More interface screenshots</summary>

#### Deployment details

![IronDeploy deployment details showing hardware, image, disk, and completed stages](Core/Docs/screenshots/DeplomentDetails.png)

#### Post-install software results

![IronDeploy post-install software results with durations and exit codes](Core/Docs/screenshots/post-installProgramResult.png)

#### Network diagnostics

![IronDeploy network diagnostics showing adapter traffic, latency, API requests, and SMB connectivity](Core/Docs/screenshots/G_NETWORKDETAILS.png)

#### Access control

![IronDeploy user permissions for dashboard, images, software, drivers, settings, and WinPE deployment](Core/Docs/screenshots/user_permishion.PNG)

</details>

## Prerequisites

Install these Microsoft components on the deployment server:

- [Windows Assessment and Deployment Kit (Windows ADK)](https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install)
- [Windows PE add-on for the Windows ADK](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/download-winpe--windows-pe)

You also need:

- Windows PowerShell 5.1;
- Python 3.14.7 (current primary tested version); Python 3.11.7 is also
  supported;
- a way to boot the generated x64 WinPE image, such as WDS/PXE, ISO, or USB.

IronDeploy does not redistribute Windows ADK, WinPE, Windows installation images,
or Windows licences.

## Installation

Download or clone the repository, open an elevated Windows PowerShell session in
its root, and run scripts 1 through 5 in order.

### 1. Prepare the WinPE working tree

```powershell
& ".\1. Prepare-IronDeployWinPE.ps1"
```

Initializes the WinPE working tree. It does not rebuild the WIM or ISO.

### 2. Prepare IronAPI

```powershell
& ".\2. Prepare-IronAPI.ps1"
```

Creates the IronAPI Python virtual environment when needed and installs its
dependencies.

### 3. Configure IronDeploy

```powershell
& ".\3. Start-IronDeploySetupWeb.ps1"
```

Starts the local SetupWeb configuration page. Configure IronAPI, the SMB share,
Active Directory connection settings, WinPE access, and the first administrator
account.

### 4. Test IronAPI in the foreground

```powershell
& ".\4. Start-IronAPI.ps1"
```

Verify that IronAPI starts correctly, then stop it with `Ctrl+C`.

### 5. Install the IronAPI Windows service

```powershell
& ".\5. Install-IronAPIService.ps1"
```

Installs IronAPI as an automatically started Windows service under a dedicated
account that already exists. Use a domain account when Active Directory or
Offline Domain Join is enabled, or a dedicated local account for deployments
without Active Directory. Built-in identities and administrator accounts are
rejected.

### Optional: remove the Windows service

```powershell
& ".\6. Delete-IronAPIService.ps1"
```

This removes only the service registration. Configuration, the database,
deployment content, logs, and repository files are preserved.

## After installation

After IronAPI is running:

1. Sign in to the IronAPI web interface.
2. Configure the allowed computer-name formats under `/image-config`.
3. Add the Windows images, driver packages, and post-install software you need.
4. Build the WinPE WIM or ISO from the Image configuration page.
5. Publish the WIM through WDS/PXE, or boot the generated ISO or USB media.

You can also build WinPE manually with `Core\Tools\Build-IronDeployWinPE.ps1`.
See [WinPE build and deployment runtime](Core/Docs/WINPE.md) for the available
build targets and detailed instructions.

## Before the first deployment

- Publish only `Core\Share` as the SMB share. Do not share the repository root.
- Give WinPE a separate read-only SMB account. Do not grant it write, change,
  administrator, or interactive logon rights.
- Run IronAPI under a dedicated identity. Do not reuse the WinPE SMB account.
- When Active Directory is enabled, delegate only the read and computer-account
  permissions required in the intended OU. Domain Admin membership is not
  required.
- Protect the local IronDeploy repository with NTFS permissions so only
  administrators, the IronAPI service identity, and authorized maintainers can
  access it.

## Deployment content delivery

New installations use the `staged` strategy for Windows images and drivers.
Both are copied to the target disk and validated locally before DISM uses them.
Windows images are verified against their manifest SHA-256; staged driver
packages are validated against their expected total size, file count, and INF
count. The `direct` strategy remains available when DISM should read the content
from SMB instead. These choices are managed by IronAPI and do not require
rebuilding the WinPE image.

## Security essentials

IronDeploy handles deployment tokens, SMB credentials, Active Directory
operations, and destructive disk actions. Before using it outside a test
environment:

- use SMB 3.x and require **SMB signing**;
- prefer HTTPS for IronAPI and enable certificate validation;
- use separate least-privilege identities for IronAPI and WinPE file access;
- never commit `.env`, databases, ODJ blobs, Windows images, generated WIM/ISO
  files, driver packages, or program installers;
- verify the target machine and the selected disk's number, model, and size
  before confirming the permanent erase.

Require SMB signing on the SMB server from an elevated PowerShell session:

```powershell
Set-SmbServerConfiguration -RequireSecuritySignature $true -Force
```

SMB encryption is optional. It hides file contents in transit but may reduce
deployment speed; SMB signing is the minimum recommended integrity protection.

See [SECURITY.md](SECURITY.md) for vulnerability reporting and additional
security guidance.

## Current Alpha limitations

- the deployment runtime targets x64 UEFI/GPT systems;
- the selected disk is fully erased and repartitioned;
- existing partitions are not yet previewed in WinPE;
- ESD images can be deployed directly, but WIM is recommended for regular use;
- hardware, firmware, networks, drivers, and Windows images vary, so the complete
  workflow must be validated in your own environment;
- during Alpha, only the latest version on the default branch is supported.

## Documentation

- [Architecture](Core/Docs/ARCHITECTURE.md)
- [WinPE build and deployment runtime](Core/Docs/WINPE.md)
- [IronAPI configuration and API behaviour](Core/Docs/API.md)
- [Initial configuration with SetupWeb](Core/Docs/SETUPWEB.md)
- [Roadmap](Core/Docs/ROADMAP.md)
- [Security policy](SECURITY.md)
- [Contributing and bug reports](CONTRIBUTING.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Community

Use [GitHub Discussions](https://github.com/Syrinoxsis/IronDeploy/discussions)
for questions, deployment experiences, feature ideas, and general or commercial
inquiries. Report bugs through GitHub Issues.

Do not report vulnerabilities publicly. Follow the private reporting process in
[SECURITY.md](SECURITY.md).

## License

IronDeploy is licensed under the Apache License, Version 2.0. See
[LICENSE](LICENSE) and [NOTICE](NOTICE).

The interface includes modified icon designs from Lucide Icons and Feather
Icons. Their applicable notices and licence terms are provided in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
