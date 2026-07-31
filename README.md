# IronDeploy

> **Alpha software.** Test IronDeploy on a virtual machine or disposable computer
> before using it on production hardware. The current WinPE workflow permanently
> erases **disk 0**.

IronDeploy is an independent Windows deployment tool for installing Windows 10
and Windows 11 from WinPE.

It performs a complete deployment from a Windows installation image. You provide
a normal WIM image, or import an ESD image and convert it to WIM; IronDeploy does
**not** capture or clone an already configured reference computer.

## What IronDeploy does

From its graphical WinPE interface, an operator can:

- choose a Windows image and edition;
- choose optional post-install programs;
- select one driver package for the detected hardware, or skip drivers;
- let IronAPI suggest the next available computer name from the configured
  sequence, for example `PC00001`, `PC00002`, and show names previously used
  by the same hardware;
- optionally join the installed computer to Active Directory through Offline
  Domain Join;
- follow deployment progress and review the final result in the IronAPI
  dashboard.

During deployment IronDeploy partitions disk 0, applies the selected Windows
image, injects offline drivers, writes the Windows answer file, optionally
applies Offline Domain Join, stages selected programs, and boots into the newly
installed operating system. Post-install tasks then install the selected
software and report their results to IronAPI.

## Prerequisites

Install these Microsoft components on the deployment server:

- [Windows Assessment and Deployment Kit (Windows ADK)](https://learn.microsoft.com/en-us/windows-hardware/get-started/adk-install)
- [Windows PE add-on for the Windows ADK](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/download-winpe--windows-pe)

You also need:

- Windows PowerShell 5.1;
- Python 3;
- `Core\Share` published as an SMB share, plus a separate local or domain
  account with read-only access for WinPE;
- a way to boot the generated x64 WinPE image, such as WDS/PXE, ISO, or USB;
- a dedicated domain account with permission to search computer objects and
  provision Offline Domain Join computer accounts in the intended OU, only if
  Active Directory features are required.

IronDeploy does not redistribute Windows ADK, WinPE, Windows installation
images, or Windows licences.

## Installation

Download or clone the repository, open an elevated Windows PowerShell session
in its root, and run the numbered scripts in order.

### 1. Prepare IronDeploy

```powershell
& ".\1. Prepare-IronDeploy.ps1"
```

Initializes the WinPE working tree, creates the IronAPI Python environment, and
installs its dependencies.

### 2. Configure IronDeploy

```powershell
& ".\2. Start-IronDeploySetupWeb.ps1"
```

Starts the local one-time SetupWeb page. Configure IronAPI, the SMB share,
computer naming, Active Directory settings, WinPE access, and the first
administrator account.

Stop SetupWeb with `Ctrl+C` after saving the configuration.

### 3. Test IronAPI in the foreground

```powershell
& ".\3. Start-IronAPI.ps1"
```

Starts IronAPI in the current console. Verify that it starts correctly, then
stop it with `Ctrl+C`.

### 4. Install the IronAPI Windows service

```powershell
& ".\4. Install-IronAPIService.ps1"
```

Installs IronAPI as an automatically started Windows service. A dedicated
domain service account with only the required Active Directory permissions is
recommended.

After installation, use the IronAPI web interface to manage Windows images,
driver packages, programs, and deployment settings.

## Security must-haves

IronDeploy handles deployment tokens, SMB credentials, Active Directory
operations, and destructive disk actions. Before using it outside a test
environment:

- use SMB 3.x and require **SMB signing** so files cannot be silently modified
  in transit;
- prefer HTTPS for IronAPI and enable certificate validation, because the API
  carries deployment tokens and temporary SMB credentials;
- protect the entire local IronDeploy repository directory with NTFS
  permissions. Allow access only to administrators, the IronAPI service
  identity, and operators who maintain the deployment system;
- when Active Directory integration is enabled, run IronAPI under a dedicated
  domain identity. It needs read/search access to computer objects for name
  checks and only the delegated rights required to create or provision computer
  accounts for Offline Domain Join in the intended OU. It does not need Domain
  Admin membership;
- use a separate local or domain account only for WinPE access to the deployment
  share. Do not reuse the IronAPI service identity;
- share only `Core\Share` and grant that SMB account read-only access in both
  the SMB share permissions and the NTFS permissions. Do not grant write,
  change, full-control, local administrator, or interactive logon rights;
- never commit `.env`, databases, ODJ blobs, WIM/ESD images, generated
  WIM/ISO files, driver packages, or program installers;
- verify the target machine before confirming the permanent erase of disk 0.

SMB encryption is optional. It hides file contents in transit but may reduce
deployment speed; SMB signing is the minimum recommended integrity protection.

## Current Alpha limitations

- the deployment runtime targets x64 UEFI/GPT systems;
- the current disk workflow always erases and partitions **disk 0**;
- multi-disk selection is not implemented;
- ESD files are imported and converted to WIM before deployment;
- hardware, firmware, network, drivers, and Windows images vary, so validate the
  complete workflow in your own environment;
- only the latest release is supported.

## Documentation

Detailed implementation and operator notes live outside this README:

- [Architecture](Core/Docs/ARCHITECTURE.md)
- [Building the WinPE image](Core/Docs/WINPE_ISO.md)
- [IronAPI configuration and API behaviour](Core/Api/README.md)
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
