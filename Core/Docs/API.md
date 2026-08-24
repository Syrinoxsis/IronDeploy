# IronAPI

IronAPI is IronDeploy's control plane. It authorizes browser users and WinPE,
validates deployment selections, integrates with Active Directory, serves
protected configuration templates, and stores deployment history.

It does not stream large Windows images, driver packages, or installers to
WinPE. Those payloads are read from SMB after IronAPI authorizes the deployment
and returns the connection details.

## Starting IronAPI

SetupWeb should create `Core\Api\.env` before the first start. From the repository
root, step 4 runs IronAPI in the foreground:

```powershell
& ".\4. Start-IronAPI.ps1"
```

The launcher reads `Core\Api\.env`, checks `Core\Api\.venv`, creates the `Core\Data`,
`Core\ODJ\pending`, and `Core\Logs` directories, and starts uvicorn. Stop a foreground
test with `Ctrl+C`.

After verifying the configuration, step 5 can install the persistent Windows
service:

```powershell
& ".\5. Install-IronAPIService.ps1"
```

The installer accepts a dedicated domain account or, for deployments without
Active Directory, an existing dedicated local account. Built-in identities such
as `LocalSystem` and administrator accounts are rejected: the installer resolves
the entered name and refuses well-known SIDs, the `BUILTIN` domain, and any
account whose RID is 500.

A domain account is required whenever LDAP name checks or Offline Domain Join
are enabled, because both run as the IronAPI process identity. IronDeploy stores
no separate LDAP or ODJ credentials; the password entered during service
installation is handed to Windows Service Control Manager for service logon.
The identity needs read access to the application and the delegated AD rights
required to create or reuse computer objects.

Leaving `IRONAPI_ODJ_DOMAIN` and `IRONAPI_ODJ_MACHINE_OU` empty disables Offline
Domain Join. `POST /api/deploy/begin` then rejects any deployment that requests
a domain join, before WinPE erases the target disk.

## Configuration

IronAPI reads `Core\Api\.env`. Its settings are grouped by responsibility:

| Group | Examples |
| --- | --- |
| Listener | access mode, bind address, port, access log, allowed client networks |
| Deployment | authorization and deployment timeouts, image-apply strategy |
| SMB | share path and configured account returned to authorized WinPE; the account must be read-only in SMB and NTFS |
| Storage | SQLite database and temporary ODJ directory |
| Naming and LDAP | name prefix/range, domain controller, base DN, LDAP TLS |
| Offline Domain Join | domain, target OU, `djoin.exe`, timeout, blob lifetime |
| Driver uploads | file, depth, path, concurrency, lifetime, and free-space limits |

Loopback clients are always allowed. When `IRONAPI_ALLOWED_CLIENT_NETWORKS`
contains CIDRs, other clients must belong to one of them or receive HTTP 403.
The variable must be present in `Core\Api\.env`; an empty value written as
`IRONAPI_ALLOWED_CLIENT_NETWORKS=` allows clients from every network.

## Data sources

IronAPI answers requests from several sources rather than one central catalog:

| Information | Source |
| --- | --- |
| Accounts, permissions, deployments, stages, inventory | `Core\Data\irondeploy.db` |
| Images, indexes, and SHA-256 hashes | `Core\Share\Images` plus server-side image metadata |
| Driver packages | `Core\Share\Drivers` |
| Programs, arguments, sizes, and hashes | `Core\Share\Programs` and its metadata file |
| Post-PowerShell payloads and profile policy | Private `Core\Library\PostPowerShell` storage plus SQLite profile bindings |
| SMB access and image apply strategy | server-side `Core\Api\.env` |
| Unattend and post-install files | `Core\ServerTemplates` |
| Computer-name availability | SQLite history plus LDAP when configured |
| ODJ result | `djoin.exe`, Active Directory, and `Core\ODJ\pending` |
| WinPE bootstrap settings | `Core\WinPE\Runtime\deploy.config.ps1` |
| Default post-install account policy | SQLite deployment profile |

The manifest endpoint validates the current selection against the current
server catalog and returns image/index/hash details, `imageApplyMode`,
`driverApplyMode`, driver-package metadata, selected programs, selected or
automatic PowerShell scripts, and post-install settings. WinPE
checks the image size, checks the driver package's total size and INF count,
and compares selected program installers with their expected SHA-256 after
copying. In staged mode WinPE also verifies the downloaded image against the
manifest SHA-256 before invoking DISM. Post-install checks program hashes again
and refuses to execute a mismatched installer. Driver packages are validated
by their server-approved relative path, total size, and INF count rather than
a content hash.

Post-PowerShell scripts use authenticated IronAPI HTTP(S) routes rather than
SMB. WinPE verifies each download against the manifest SHA-256, and
post-install verifies it again immediately before execution. Profile bindings
hold automatic/operator policy, before/after-software phase, raw arguments,
and a timeout from 1 second through 3 hours. Failures never change the
deployment terminal status. IronAPI
stores up to 20 MiB of raw output per execution under
`Core\Logs\PostPowerShell` and loads it lazily on deployment details.

`imageApplyMode` accepts `direct` or `staged`; new installations default to
`staged`. IronAPI refuses to issue a staged manifest when the selected image
has no valid SHA-256. No additional endpoint is used: WinPE reads the value
once from `POST /api/deploy/{id}/manifest` for the current deployment. A WinPE
runtime receiving a missing or unsupported value falls back to `direct`.

`driverApplyMode` uses the same `direct` or `staged` values and the same
manifest endpoint, with `staged` as the new-installation default. Direct mode
keeps DISM on the selected SMB package. Staged mode copies the package locally
and validates its byte, file, and INF counts before offline injection. A WinPE
runtime receiving a missing or unsupported value falls back to `direct`.

## Browser interface

The IronAPI browser interface provides:

- deployment dashboard, details, stage history, errors, and network summaries;
- browser user and permission management;
- WinPE authorization policy;
- WIM/ESD image upload, WIM rename, index selection, and ESD-to-WIM conversion;
- program upload, rename, arguments, hash metadata, and removal;
- Post-PowerShell upload, arguments, phase, selection mode, timeout, and removal;
- driver vendor/package upload and cleanup;
- WinPE and image configuration;
- WIM or ISO rebuild controls.

Browser pages require a signed-in account and enforce permissions per area.
The special WinPE deployment permission cannot be combined with browser/admin
permissions.

## API groups

The HTTP routes fall into a small number of behavioral groups.

Key WinPE-facing routes are:

| Route or group | Result | Main source |
| --- | --- | --- |
| `GET /api/deploy/auth/policy` | Active WinPE authorization mode | SQLite authorization policy |
| `POST /api/deploy/auth/login` and `/authorize` | Short-lived deployment bearer | IronAPI accounts or the server-owned PIN/credential-free policy |
| `GET /api/deploy/suggest-name` | Suggested and previously used computer names | Naming configuration, LDAP, and SQLite inventory |
| `GET /api/deploy/catalog` | Available images, indexes, programs, PowerShell choices, hashes, and driver-package metadata | `Core\Share`, `Core\Library`, SQLite profiles, and server-side metadata |
| `POST /api/deploy/begin` | Deployment ID and bound bearer state | Submitted hardware, target-disk snapshot, selection data, and SQLite |
| `POST /api/deploy/{id}/manifest` | Validated server-approved deployment plan | Current catalog and image settings |
| `GET /api/deploy/{id}/smb-credentials` | Configured SMB connection details; the account must be read-only | `Core\Api\.env` |
| `PUT /api/deploy/{id}/network-diagnostics/adapters` | Early API/SMB adapter, IP, route relationship, and negotiated link-speed snapshot | WinPE network interfaces and SQLite |
| Unattend and post-install routes | Per-deployment answer file and scripts | `Core\ServerTemplates` and image settings |
| Post-PowerShell routes | Authenticated `.ps1` downloads plus per-script status and bounded raw output | Resolved profile manifest, SQLite, private `Core\Library\PostPowerShell` storage, and `Core\Logs` |
| Domain-join routes | ODJ provisioning, download, and acknowledgement | Active Directory and `Core\ODJ\pending` |
| Stage, error, diagnostics, and completion routes | Deployment progress and final result | SQLite deployment state |

### Health and browser sessions

Health indicates that the process is responding. Browser authentication uses
the configured local IronAPI accounts, signed session cookies, and per-page
permissions. The initial superadmin is imported from SetupWeb's bootstrap file
during API startup.

### Administration and catalog management

Administrative routes manage users, permissions, WinPE authorization, images,
drivers, programs, and image settings. Upload routes validate names and keep
payloads inside their owning directories. Long-running ESD conversion and
WinPE build operations expose their current state separately.

### WinPE authorization

WinPE first reads the active policy, then uses one of three modes:

- a dedicated account with deployment-only permission;
- a server-configured numeric PIN;
- explicitly enabled credential-free authorization.

All modes issue the same kind of short-lived deployment bearer. PIN and
credential-free modes use an internal deployment-only principal. Repeated bad
PIN attempts are temporarily locked.

### Deployment planning

An authorized WinPE client can request a name suggestion and catalog. When the
operator confirms the selection, `/api/deploy/begin` creates the deployment
and binds the bearer to it. Current WinPE submits `target_disk_number`,
`target_disk_model`, and `target_disk_size_bytes`; the fields remain nullable
for deployment history and compatibility with older WinPE images. WinPE then
submits the selection to the manifest endpoint, which validates it against the
current catalog and returns the server-approved plan.

The hardware serial number is optional. WinPE first tries the BIOS serial and
then the system-product identifying number. Empty values, firmware placeholders
such as `To Be Filled By O.E.M.`, malformed values, and WMI read failures are
reported as `null` and never block preflight or deployment. Name-history lookup
and computer inventory fall back to the normalized MAC address. A later missing
serial does not erase a valid serial already stored for that MAC address.

The bound bearer cannot be reused to begin a second deployment. The manifest
response is generated when requested; it is not stored as a separate immutable
snapshot.

### Deployment execution

During execution, routes provide:

- the configured SMB connection details;
- the per-deployment unattend file;
- ODJ provision/download/acknowledgement operations;
- SetupComplete and post-install script content;
- stage start, completion, skip, and failure events;
- an early adapter-only network snapshot before disk partitioning;
- WinPE error, per-stage, and final aggregate network-diagnostic reports.

After `/api/deploy/begin` binds a deployment ID, WinPE identifies the routes
used for IronAPI and SMB and sends their adapter names, local IP addresses, and
negotiated `link_speed_bps` values to the adapter snapshot endpoint. The call
is best effort and occurs before SMB validation, manifest retrieval, and disk
partitioning. This lets deployment details expose 100/1000 Mbps link speed even
if the machine is later powered off abruptly.

The adapter snapshot creates a partial `deployment_network_summaries` row whose
final aggregate fields remain null. The existing final
`PUT /api/deploy/{id}/network-diagnostics` request fills those metrics and
overwrites the adapter fields in that same row. Per-stage diagnostic requests
continue to store only their completed measurement windows.

Direct deployments retain the existing `image_apply` stage and its SMB network
measurement. Staged deployments report `image_download` separately from
`image_apply`; network bytes, duration, and throughput belong only to the
download window, while local DISM progress and duration belong to the apply
stage.

Driver deployments follow the same measurement boundary. Direct mode measures
SMB activity during `driver_injection`. Staged mode reports network activity in
`driver_download`; the following local `driver_injection` stage records time
without attributing network bytes to the local DISM work.

The server controls which routes are available in the WinPE and post-install
phases. The installed machine uses the persisted deployment state only to
submit its post-install results and final completion.

## Deployment state

IronAPI stores deployment identity and status, the selected image name and
image-apply strategy, the operator-confirmed target disk number/model/size
snapshot, domain-join choice, stages, errors, computer inventory, program
results, early adapter data, and aggregate diagnostics in SQLite. An adapter
snapshot may exist before the aggregate report is complete. Stale active
deployments are expired according to the configured timeout.

The deployment bearer progresses through authorization, bound WinPE work, and
post-install. It is revoked when the deployment completes or fails. A
short-lived completion receipt allows the final acknowledgement to be retried
without reopening the deployment.

## Offline Domain Join

When ODJ is selected:

1. IronAPI validates the requested computer name.
2. `djoin.exe /provision` runs as the IronAPI process identity.
3. The temporary blob is written under `Core\ODJ\pending`.
4. Only the bound deployment can download it.
5. WinPE applies it to offline Windows and acknowledges success.
6. IronAPI deletes the blob.

Cleanup also runs when a deployment fails, expires, or completes without a
successful acknowledgement. A maximum-age purge removes any remaining stale
blob. ODJ files contain computer-account secrets and must never be committed or
placed on the SMB share.

## SQLite and migrations

IronAPI currently supports SQLite. Startup applies numbered migrations and
records completed versions in `schema_migrations`.

Migration 6 makes final-only columns in `deployment_network_summaries` nullable
so an adapter snapshot can be stored before aggregate measurements finish.
Existing completed diagnostic rows are copied without changing their values.
Migration 7 creates the default deployment profile that owns post-install
account policy. It intentionally starts with Alpha defaults instead of reading
legacy values from the WinPE runtime configuration.

Before migrating a file-backed database, IronAPI:

1. runs `PRAGMA quick_check`;
2. creates a timestamped `irondeploy.db.<timestamp>.bak` beside the database;
3. validates the backup with `PRAGMA integrity_check`;
4. applies all pending migrations in one transaction under a writer lock.

A failure rolls back the transaction and stops startup. To restore, stop
IronAPI, keep the failed database for investigation, replace
`Core\Data\irondeploy.db` with the backup named in the startup error, run
`PRAGMA integrity_check` against the restored copy, and start IronAPI again.
Backups are retained until the operator removes them according to the local
retention policy.

## OpenAPI surface

The current FastAPI application exposes `/docs` and `/openapi.json`. They are
useful for inspecting the exact schema of the running version, but they are not
the architectural documentation or a promise that every route is a public
integration API. Deployments should not depend on those pages being enabled;
they may be restricted or disabled later.

For the WinPE-side sequence, see [WINPE.md](WINPE.md). For initial
configuration, see [SETUPWEB.md](SETUPWEB.md).
