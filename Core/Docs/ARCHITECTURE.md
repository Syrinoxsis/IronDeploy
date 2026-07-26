# IronDeploy architecture

## Boundaries

`Api`, `WinPE`, and `Share` are independent runtime boundaries:

- `Api` owns HTTP, LDAP lookups, deployment state, and ODJ provisioning.
- `WinPE\Runtime` owns destructive disk deployment and offline Windows setup.
- `Share` is read-only deployment payload exposed over SMB.

Inside `WinPE\Runtime` the destructive logic is isolated from its user
interface. `IronDeploy.Engine.ps1` performs the deployment and reports through
callbacks; it never prompts and never reboots. `IronDeploy.Gui.ps1` is the
only supported deployment front-end, and `deploy.ps1` launches it and handles
the final reboot. The GUI runs the engine on a background
runspace and marshals log and progress events to the UI thread through a
synchronized queue, so the interface stays responsive during long DISM steps.
The engine is the reboot-free, prompt-free source of truth; the GUI only
collects input and renders progress. If WPF cannot continue, the normal WinPE
console is restored for diagnostics without starting another deployment flow.

Generated state is never source:

- `Data` contains the API database.
- `ODJ\pending` contains short-lived computer-account secrets.
- `.work` contains the mutable ADK tree and mounted-image workspace.
- `dist` contains replaceable delivery artifacts.

## Configuration ownership

IronAPI reads `Api\.env`, including the server-side read-only SMB credentials.
WinPE reads a credential-free `WinPE\Runtime\deploy.config.ps1`. SetupWeb writes
the listener access mode, SMB credentials, API URL, and optional public trust
certificate to their owning configurations. The Windows installation reads the
per-deployment unattend file returned by IronAPI after authorization.

Deployment control data travels through the authenticated HTTP(S) API: image
readiness/indexes, program arguments/hashes, the final selection manifest, SMB
credentials, unattend content, and post-install account flags. SMB contains the
heavy WIM/ESD, driver, script, and installer bytes. HTTP remains supported;
HTTPS can use certificate-validation bypass, a pinned self-signed certificate,
or a trusted CA certificate.

WinPE reads its authorization policy from IronAPI. Account/password, hashed
PIN, and credential-free modes all create the same kind of bearer record;
PIN and credential-free mode use a hidden internal deployment principal.
`/begin` binds that record to exactly one deployment. The same bearer is used
through WinPE and post-install, while the server-side phase controls which
endpoints remain available. Deployment-only permissions are mutually exclusive
with every browser/admin permission and this invariant is enforced by the
backend. No PIN or authorization mode is embedded in the WinPE configuration.

## Path policy

Repository paths are derived from each script's location. Host-specific
absolute paths are limited to explicit configuration and Windows runtime paths
such as `X:\IronDeploy`, `C:\Windows`, the mapped SMB drive, and the installed
Windows ADK.

## Artifact flow

```text
WinPE\Runtime
      │
      ▼
.work\WinPE_amd64\media\sources\boot.wim
      │
      ├──► dist\IronDeploy_PE.wim
      └──► dist\IronDeploy_PE.iso
```

`Share\Images` contains Windows installation images and is not a build output.

## ODJ flow

```text
WinPE ──POST──► IronAPI ──djoin.exe──► Active Directory
  ▲                 │
  │                 ▼
  └──── HTTP ── ODJ\pending
```

The LDAP Credential Manager entry is used for directory searches. `djoin.exe`
runs as the IronAPI process identity, which needs the delegated rights required
to create and reuse computer objects. WinPE does not execute the target
Windows `djoin.exe`; it applies the downloaded ODJ blob to the offline Windows
image through a temporary offlineServicing unattend file and DISM.
