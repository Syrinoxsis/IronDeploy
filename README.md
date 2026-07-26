# IronDeploy

IronDeploy deploys Windows 10/11 from WinPE, injects offline drivers, prepares
Offline Domain Join, and reports deployment progress to IronAPI.

## Deployment interface (WinPE)

When a machine boots the IronDeploy WinPE image, `startnet.cmd` launches
`deploy.ps1`, which presents the supported **graphical interface**:

- A three-step setup wizard first shows the detected serial/MAC, IronAPI name
  suggestion, previous hardware names, Windows image selection, and domain
  join. The second step selects optional post-install software. The third step
  shows the detected hardware model above a scrollable, expandable vendor tree
  with package checkboxes. Checking
  one package clears every other package; **Do not install drivers** is the
  mutually exclusive alternative. The disk-0 wipe confirmation and deployment
  buttons remain fixed on screen.
- A progress screen shows a live, colour-coded log and a stage progress bar
  while the engine wipes, images, injects drivers, applies the unattend file,
  performs Offline Domain Join, and writes the boot files.
- During deployment, the engine uses one in-process background runspace to
  ping the SMB server once per second. It records aggregate ICMP and inbound
  adapter traffic statistics for image apply, driver injection, post-install
  copy, and the complete WinPE run. The monitor opens no extra window and any
  diagnostics failure is non-fatal.
- On success it counts down and reboots into Windows (with a cancel option);
  on failure it shows the error and offers an explicit reboot button.

The GUI is built with **PowerShell + WPF/XAML**. It runs entirely in-process
inside WinPE using only the `.NET Framework` and PowerShell that are already
added to the image (`WinPE-NetFX`, `WinPE-Scripting`, `WinPE-PowerShell`). It
opens no network listener, embeds no browser, and adds no new optional
components — so it introduces no new attack surface — while XAML keeps the
layout and styling fully customisable.

The runtime is split into three files under `Core\WinPE\Runtime`:

- `deploy.ps1` — the WinPE-only launcher and final reboot coordinator.
- `IronDeploy.Engine.ps1` — all deployment logic, driven through callbacks;
  contains no prompts and never reboots.
- `IronDeploy.Gui.ps1` — the WPF front-end. Runs the engine on a background
  runspace and streams log/progress updates to the UI thread.

`deploy.ps1` runs the GUI as the only supported deployment interface. After
WPF initializes, the shared WinPE console is hidden while the GUI is active.
If WPF cannot start or encounters a fatal UI error, the console is restored
with the error message, but no alternative deployment workflow is started.

## Safety

- Never run `Core\WinPE\Runtime\deploy.ps1` on a normal Windows host. It wipes
  disk 0.
- Do not rebuild WinPE while another WIM is mounted.
- Do not commit `.env`, `deploy.config.ps1`, databases, ODJ blobs, WIMs, ISOs,
  Python environments, or driver packages.
- ODJ blobs contain computer-account secrets. Keep `Core\ODJ` local to the
  API server and restrict its ACL.

## First setup: files 1, 2, 3, 4

Run the numbered PowerShell files from the repository root in order. Steps 1
through 3 prepare, configure, and verify IronAPI in the foreground. Step 4
optionally installs the verified application as a persistent Windows Service.

### 1. Prepare IronDeploy

```powershell
& ".\1. Prepare-IronDeploy.ps1"
```

This initializes the WinPE working tree under `Core\.work`, creates
`Core\Api\.venv` when needed, and installs the IronAPI Python dependencies.
The WinPE initializer requests administrator elevation when required. It may
also ask before reinstalling an existing working tree.

Step 1 does not configure IronDeploy, rebuild a WIM or ISO, or start IronAPI.

### 2. Configure IronDeploy

```powershell
& ".\2. Start-IronDeploySetupWeb.ps1"
```

This starts the temporary SetupWeb service on `127.0.0.1`, using a random high
port and a one-time bootstrap token, then opens the setup page in the default
browser. SetupWeb creates its own `Core\SetupWeb\.venv` and writes the
configuration used by IronAPI and WinPE. It does not start IronAPI.

Complete and save the configuration in the browser, then stop SetupWeb with
`Ctrl+C` before continuing. Use `-NoBrowser` to print the local setup URL
without opening it automatically, or `-SkipDependencyInstall` to reuse the
existing SetupWeb environment without running pip.

### 3. Start IronAPI

```powershell
& ".\3. Start-IronAPI.ps1"
```

This starts IronAPI in the foreground through
`Core\Tools\Start-IronAPI.ps1`. The launcher accepts `-BindHost`, `-Port`, and
`-AccessLog`; when they are omitted, listener settings come from
`Core\Api\.env`.

For example:

```powershell
& ".\3. Start-IronAPI.ps1" -Port 8080 -AccessLog
```

Verify that IronAPI starts successfully, then stop the foreground process with
`Ctrl+C` before installing the service.

### 4. Install the IronAPI service

```powershell
& ".\4. Install-IronAPIService.ps1"
```

The installer requests administrator elevation and recommends a dedicated
domain service account such as `DOMAIN\svc_irondeploy`. It securely asks for
and confirms the account password, grants **Log on as a service**, registers
the pywin32-based `IronAPI` service, enables automatic startup, and configures
restart recovery after 5, 15, and 60 seconds. It asks before starting the
service.

`LocalSystem` is available only as an explicit fallback. The installer warns
about its full local-machine privileges and requires the operator to type
`LocalSystem` to confirm; pressing Enter never selects it.

Windows Credential Manager entries are identity-specific. When LDAP is used,
the `IronDeploy-LDAP` credential must exist in the selected service account's
profile. That account also needs read/execute access to the IronDeploy files
and the base Python installation, plus modify access to `Core\Data`,
`Core\ODJ`, and `Core\Logs`. Service output is appended to
`Core\Logs\IronAPI-service.log`.

The numbered files are the normal operator entrypoints. Step 2 can be rerun
when configuration must be changed. Step 3 remains available for foreground
diagnostics; step 4 is used to install or update the persistent service.

For advanced command-line configuration, the guided master remains available:

```powershell
& ".\Core\Tools\ConfigureMaster.ps1" -InitialSetup
```

Validate the complete installation without starting IronAPI or rebuilding
WinPE:

```powershell
& ".\Core\Tools\Test-IronDeploy.ps1"
```

Build only when a new WIM or ISO is explicitly required:

```powershell
& ".\Core\Tools\Build-IronDeployWinPE.ps1"
& ".\Core\Tools\Build-IronDeployWinPE.ps1" -Target Wim
& ".\Core\Tools\Build-IronDeployWinPE.ps1" -Target Iso
& ".\Core\Tools\Build-IronDeployWinPE.ps1" -Target Both
```

After re-initializing the working tree, always run a target that writes the
runtime into `boot.wim` (`-Target Wim`, `-Target Both`, or `-Target Iso`).
`-Target Iso` copies the runtime files into `boot.wim` before packaging it, so
the ISO cannot be built from a freshly initialized (stock) image. Add
`-SkipWimUpdate` only to repackage an already-updated `boot.wim` into an ISO
without touching it — for example right after a `-Target Wim` build.

## Layout

```text
IronDeploy\
├─ 1. Prepare-IronDeploy.ps1          prepare WinPE and IronAPI dependencies
├─ 2. Start-IronDeploySetupWeb.ps1    configure IronDeploy in the local UI
├─ 3. Start-IronAPI.ps1               run IronAPI in the foreground
├─ 4. Install-IronAPIService.ps1      install the persistent Windows Service
└─ Core\
   ├─ Api\                 FastAPI application, dashboard, tests, and .env
   ├─ WinPE\
   │  ├─ Runtime\          files copied into boot.wim
   │  └─ Build\            internal DISM/ADK servicing scripts
   ├─ Share\               root published as the IronDeploy SMB share
   │  ├─ Images\           Windows installation WIMs
   │  ├─ Drivers\          extracted INF driver packages
   │  ├─ Unattend\         Windows answer-file templates
   │  └─ PostInstall\      SetupComplete and post-install reporting
   ├─ Tools\               internal and advanced operator entrypoints
   ├─ Docs\                architecture and recovery procedures
   ├─ Data\                IronAPI SQLite database (ignored)
   ├─ ODJ\pending\         temporary ODJ blobs (ignored)
   ├─ Logs\                service, setup, and build logs (ignored)
   ├─ .work\               mutable ADK/WinPE working tree (ignored)
   └─ dist\                WIM, ISO, hashes, and PXE bundle (ignored)
```

All scripts derive the IronDeploy root from their own location. The directory
can be moved to another drive while IronAPI and build operations are stopped.
Windows Credential Manager entries, service identities, SMB configuration, and
ACLs remain machine-specific and must be validated after a move.

## Configuration

- `Core\Api\.env` controls IronAPI, LDAP, ODJ, the API listener, and the
  server-side read-only SMB credentials.
- `Core\WinPE\Runtime\deploy.config.ps1` contains only non-secret WinPE
  settings: the API URL, optional trusted public certificate, mapped drive,
  and paths.
- `Core\Share\Unattend\unattend-win11-template.xml` controls Windows locale,
  time zone, and the local recovery account.

Typical per-image defaults — the local administrator name and password, the
built-in Administrator policy, and the Windows time zone — can be edited from
IronAPI at `http://<ironapi-host>:8000/image-config` or through the
**Image settings** link on the IronAPI dashboard. Access follows the same
allowed-client network policy as the dashboard.

IronAPI browser access uses local accounts and server-side sessions. The first
superadmin is configured through the localhost-only SetupWeb interface; that
account alone can create users and assign exact Dashboard, Windows images, and
Image settings permissions. A mutually exclusive **WinPE deployment only**
role can authorize one deployment per login. IronAPI issues a random bearer
bound to that deployment; WinPE and post-install never use browser cookies.

That page also exposes separate elevated WIM and ISO rebuild actions. IronAPI
tracks one background build at a time and writes its output under
`Core\Logs\WinPEBuilds`.

Windows installation images can be managed at
`http://<ironapi-host>:8000/images`. The page supports streamed WIM/ESD
uploads, refreshing files copied directly into `Core\Share\Images`, per-image
default DISM indexes with edition names, and background ESD-to-WIM conversion.
Per-image defaults are stored in
`Core\Share\Images\.irondeploy-images.json` and override the legacy global
`$ImageIndex` when WinPE applies the selected image.

Driver vendors and model packages can be managed at
`http://<ironapi-host>:8000/drivers`. A package is stored under
`Core\Share\Drivers\<vendor>\<model>` with its nested directory structure
intact. WinPE receives the chosen package in the final manifest and runs
recursive DISM driver injection only for that package. Choosing **Do not
install drivers** skips the driver-injection stage.

The master writes these consumer-native files consistently. Runtime state uses
portable defaults below the IronDeploy root:

```text
Core\Data\irondeploy.db
Core\ODJ\pending
Core\Logs
```

## Data flow

WinPE receives image/program/driver catalogs, selected indexes, installer
arguments, hashes, and the final deployment manifest from IronAPI. Heavy
Windows images, drivers, unattend, SetupComplete/postinstall scripts, and
installer bytes stay on the SMB `Share`. WinPE and postinstall verify installer
SHA-256 before execution. ODJ never travels through SMB: IronAPI creates the
blob locally in `Core\ODJ\pending`, WinPE downloads it over HTTP(S), applies it
to the offline Windows image with DISM, acknowledges success, and IronAPI
deletes it.

The network monitor identifies the route to SMB instead of choosing the first
active adapter. Ping and received-byte counters use that adapter; existing API
request durations use the route to IronAPI, which may be different. Only final
aggregates are submitted to IronAPI, never the individual one-second samples.
