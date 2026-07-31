# IronDeploy architecture

IronDeploy has four main components. Each one owns a distinct part of the
deployment process:

| Component | Responsibility |
| --- | --- |
| SetupWeb | Writes the initial IronDeploy configuration on the deployment server. |
| IronAPI | Authorizes operators, builds the deployment plan, stores state, and integrates with Active Directory. |
| WinPE | Selects a deployment, erases the target disk, and applies Windows offline. |
| Post-install | Finishes the deployment inside the newly installed Windows system. |

WDS/PXE or bootable media only deliver WinPE. SMB carries large payloads such
as Windows images, drivers, and installers. IronAPI is the control plane.

## System flow

```text
                           Active Directory
                                  ^
                                  |
SetupWeb ---- configuration ----> IronAPI ----> SQLite
     |                            ^   ^
     |                            |   |
     +------ WinPE config ------> |   +------ browser management
                                  |
WDS / ISO ---- boot ----> WinPE --+------ deployment control
                            |
SMB share ---- payloads -----+
                            |
                            v
                    offline Windows
                            |
                            v
                       post-install
                            |
                            +----------> IronAPI completion report
```

WinPE asks IronAPI what may be deployed and reports progress. IronAPI returns
the authorized catalog, validated manifest, SMB connection details, answer file,
and optional Offline Domain Join data. WinPE reads the large files named in the
manifest directly from SMB.

## Ownership and storage

| Location | Owner | Contents |
| --- | --- | --- |
| `Core\Api\.env` | IronAPI | Listener, SMB, LDAP, ODJ, and timeout settings. |
| `Core\WinPE\Runtime\deploy.config.ps1` | WinPE | API address, payload paths, certificate trust, and offline account policy. |
| `Core\Data\irondeploy.db` | IronAPI | Accounts, permissions, deployments, stages, and inventory. |
| `Core\ODJ\pending` | IronAPI | Short-lived Offline Domain Join blobs. |
| `Core\Share` | SMB data plane | Windows images, driver packages, and installers. |
| `Core\ServerTemplates` | IronAPI | Authorized unattend and post-install templates. |
| `Core\.work` | WinPE build tools | Mutable Windows ADK working tree. |
| `Core\dist` | WinPE build tools | Replaceable WIM and ISO delivery artifacts. |

Generated state is not source code. The source copied into a WinPE image lives
under `Core\WinPE\Runtime`.

## Secret boundaries

- SetupWeb writes the SMB credential only to server-side `Core\Api\.env`.
- WinPE receives the SMB credential from IronAPI for an authorized deployment;
  it is not embedded in `deploy.config.ps1`.
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
