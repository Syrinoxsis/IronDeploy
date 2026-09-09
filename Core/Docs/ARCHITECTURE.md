# IronDeploy architecture

For hardware-aware AUTO driver selection, import-time indexing, provider contracts,
and the WinPE/TAR flow, start with [DYNAMIC_DRIVERS.md](DYNAMIC_DRIVERS.md).

IronDeploy has four main components. Each one owns a distinct part of the
deployment process:

| Component | Responsibility |
| --- | --- |
| SetupWeb | Writes the initial IronDeploy configuration on the deployment server. |
| IronAPI | Authorizes operators, builds the deployment plan, stores state, and integrates with Active Directory. |
| WinPE | Collects operator choices, creates the deployment through IronAPI, erases the target disk, and applies Windows offline. |
| Post-install | Finishes the deployment inside the newly installed Windows system. |

WDS/PXE or bootable media only deliver WinPE. SMB carries large payloads such
as Windows images, drivers, and installers. IronAPI is the control plane.

## System flow

Configuration:

```text
SetupWeb
  +--> Core\Api\.env ----------------> IronAPI
  +--> deploy.config.ps1 --> WinPE build --> WIM / ISO
```

Deployment runtime:

```text
Browser management --> IronAPI <--> SQLite
                           ^
                           |
Active Directory <---------+
                           |
WDS / PXE / ISO --> WinPE -+---- deployment control
                      |
SMB share -- payloads-+
                      |
                      v
               offline Windows
                      |
                      v
                 post-install
                      |
                      +--------> IronAPI completion report
```

WinPE asks IronAPI what may be deployed and reports progress. IronAPI returns
the authorized catalog, validated manifest, SMB connection details, answer file,
and optional Offline Domain Join data. Drivers and installers are read from
SMB. Profile-approved post-PowerShell scripts use authenticated IronAPI
HTTP(S) routes and are verified twice with the manifest SHA-256.
The manifest also carries the server-owned image- and driver-apply strategies:
WinPE either lets DISM read each payload directly from SMB or stages and
validates a local copy before invoking DISM. In staged driver mode, IronAPI
builds a fresh deployment-scoped uncompressed TAR while WinPE handles the
image; WinPE later transfers that single file over SMB and extracts it with the
bundled x64 7-Zip runtime.

Immediately after a deployment ID is created, WinPE reports the API/SMB route
adapters, local addresses, and negotiated link speeds. This happens before the
target disk is modified. The later aggregate network report updates the same
SQLite summary with completed measurements and its final adapter values.

## Ownership and storage

| Location | Owner | Contents |
| --- | --- | --- |
| `Core\Api\.env` | IronAPI | Listener, SMB, image- and driver-apply strategies, LDAP, ODJ, and timeout settings. |
| `Core\WinPE\Runtime\deploy.config.ps1` | WinPE | API address, payload paths, certificate trust, and WinPE UI behavior. |
| `Core\Data\irondeploy.db` | IronAPI | Accounts, permissions, deployment profiles, deployments, stages, and inventory. |
| `Core\ODJ\pending` | IronAPI | Short-lived Offline Domain Join blobs. |
| `Core\Share` | IronAPI / SMB data plane | Windows images, driver packages, bounded temporary driver TAR files, and installers managed by IronAPI and read by WinPE through SMB. |
| `Core\Library\PostPowerShell` | IronAPI | Private managed `.ps1` library delivered only through authenticated HTTP(S). |
| `Core\Logs\PostPowerShell` | IronAPI | Bounded raw stdout/stderr captured for deployment details. |
| `Core\ServerTemplates` | IronAPI | Authorized unattend and post-install templates. |
| `Core\.work` | WinPE build tools | Mutable Windows ADK working tree. |
| `Core\dist` | WinPE build tools | Replaceable WIM and ISO delivery artifacts. |

Generated state is not source code. The source copied into a WinPE image lives
under `Core\WinPE\Runtime`.

## Secret boundaries

- SetupWeb writes the SMB credential only to server-side `Core\Api\.env`.
- WinPE receives the SMB credential from IronAPI for an authorized deployment;
  it is not embedded in `deploy.config.ps1`.
- The image- and driver-apply strategies are stored with IronAPI configuration
  and returned in the existing final deployment manifest; they are not embedded
  in WinPE.
- Post-install account policy belongs to the default deployment profile in
  SQLite. IronAPI returns it in the manifest; WinPE does not carry a fallback
  copy in `deploy.config.ps1`.
- Post-PowerShell availability and automatic/operator policy belong to the
  deployment profile. Each deployment retains a snapshot of the resolved
  script plan and its results.
- Browser sessions and WinPE deployment tokens are separate authorization
  mechanisms.
- ODJ blobs exist only long enough to provision, download, apply, and
  acknowledge the domain join.
- The deployment token continues into post-install only so the installed
  machine can submit its final result.

Detailed behavior belongs to the component documents:

- [WinPE](WINPE.md)
- [IronAPI](API.md)
- [SetupWeb](SETUPWEB.md)
- [Roadmap](ROADMAP.md)
