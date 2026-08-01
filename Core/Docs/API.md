# IronAPI

IronAPI is IronDeploy's control plane. It authorizes browser users and WinPE,
validates deployment selections, integrates with Active Directory, serves
protected configuration templates, and stores deployment history.

It does not stream large Windows images, driver packages, or installers to
WinPE. Those payloads are read from SMB after IronAPI authorizes the deployment
and returns the connection details.

## Starting IronAPI

SetupWeb should create `Core\Api\.env` before the first start. From the repository
root, step 3 runs IronAPI in the foreground:

```powershell
& ".\3. Start-IronAPI.ps1"
```

The launcher reads `Core\Api\.env`, checks `Core\Api\.venv`, creates the `Core\Data`,
`Core\ODJ\pending`, and `Core\Logs` directories, and starts uvicorn. Stop a foreground
test with `Ctrl+C`.

After verifying the configuration, step 4 can install the persistent Windows
service:

```powershell
& ".\4. Install-IronAPIService.ps1"
```

The recommended service identity is a dedicated domain account. LDAP searches
and `djoin.exe` both run as the IronAPI process identity. IronDeploy stores no
separate LDAP or ODJ credentials; the password entered during service
installation is handed to Windows Service Control Manager for service logon.
The identity needs read access to the application and the delegated AD rights
required to create or reuse computer objects.

## Configuration

IronAPI reads `Core\Api\.env`. Its settings are grouped by responsibility:

| Group | Examples |
| --- | --- |
| Listener | access mode, bind address, port, access log, allowed client networks |
| Deployment | authorization and deployment timeouts |
| SMB | share path and configured account returned to authorized WinPE; the account must be read-only in SMB and NTFS |
| Storage | SQLite database and temporary ODJ directory |
| Naming and LDAP | name prefix/range, domain controller, base DN, LDAP TLS |
| Offline Domain Join | domain, target OU, `djoin.exe`, timeout, blob lifetime |
| Driver uploads | file, depth, path, concurrency, lifetime, and free-space limits |

Loopback clients are always allowed. Other clients must belong to a CIDR in
`IRONAPI_ALLOWED_CLIENT_NETWORKS`; rejected clients receive HTTP 403.

## Data sources

IronAPI answers requests from several sources rather than one central catalog:

| Information | Source |
| --- | --- |
| Accounts, permissions, deployments, stages, inventory | `Core\Data\irondeploy.db` |
| Images and indexes | `Core\Share\Images` plus server-side image metadata |
| Driver packages | `Core\Share\Drivers` |
| Programs, arguments, sizes, and hashes | `Core\Share\Programs` and its metadata file |
| SMB access | server-side `Core\Api\.env` |
| Unattend and post-install files | `Core\ServerTemplates` |
| Computer-name availability | SQLite history plus LDAP when configured |
| ODJ result | `djoin.exe`, Active Directory, and `Core\ODJ\pending` |
| WinPE runtime settings | `Core\WinPE\Runtime\deploy.config.ps1` |

The manifest endpoint validates the current selection against the current
server catalog and returns image/index details, driver-package metadata,
selected programs, and post-install settings. WinPE checks the image size, checks the driver package's total size and INF
count, and compares selected program installers with their expected SHA-256
after copying. Post-install checks the hash again and refuses to execute a
mismatched installer. IronDeploy does not
currently calculate a content hash for Windows images or driver packages.

## Browser interface

The IronAPI browser interface provides:

- deployment dashboard, details, stage history, errors, and network summaries;
- browser user and permission management;
- WinPE authorization policy;
- WIM/ESD image upload, WIM rename, index selection, and ESD-to-WIM conversion;
- program upload, rename, arguments, hash metadata, and removal;
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
| `GET /api/deploy/catalog` | Available images, indexes, programs, hashes, and driver-package metadata | `Core\Share` and its server-side metadata |
| `POST /api/deploy/begin` | Deployment ID and bound bearer state | Submitted hardware/selection data and SQLite |
| `POST /api/deploy/{id}/manifest` | Validated server-approved deployment plan | Current catalog and image settings |
| `GET /api/deploy/{id}/smb-credentials` | Configured SMB connection details; the account must be read-only | `Core\Api\.env` |
| Unattend and post-install routes | Per-deployment answer file and scripts | `Core\ServerTemplates` and image settings |
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
and binds the bearer to it. WinPE then submits the selection to the manifest
endpoint, which validates it against the current catalog and returns the
server-approved plan.

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
- WinPE error and network-diagnostic reports.

The server controls which routes are available in the WinPE and post-install
phases. The installed machine uses the persisted deployment state only to
submit its post-install results and final completion.

## Deployment state

IronAPI stores deployment identity and status, the selected image name,
domain-join choice, stages, errors, computer inventory, program results, and
aggregate diagnostics in SQLite. Stale active
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
