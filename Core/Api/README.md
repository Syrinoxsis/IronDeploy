# IronAPI

FastAPI service for IronDeploy automation.

Requests are accepted from loopback and CIDRs listed in
`IRONAPI_ALLOWED_CLIENT_NETWORKS`. Other clients receive `403 Forbidden`.

Deployment state is stored through SQLAlchemy. The default database is
`..\Data\irondeploy.db` (SQLite). Set `IRONAPI_DATABASE_URL` to use another
SQLAlchemy-supported database:

```dotenv
IRONAPI_DATABASE_URL=sqlite:///{IRONDEPLOY_ROOT}/Data/irondeploy.db
```

For PostgreSQL, install a PostgreSQL SQLAlchemy driver and use a URL such as
`postgresql+psycopg://user:password@server/database`.

Each request is written to the console log with its client address, HTTP
method, endpoint, response status and duration. Rejected requests are logged
as well.

## Setup

Run the interactive configurator:

```powershell
.\Setup-IronAPI.ps1
```

On the first run it creates `.env` from `.env.example` and opens the full setup
wizard. Later runs preserve the existing configuration, show current values,
and offer a menu for editing one section at a time. Enter keeps the current
value; `-` clears an optional value.

The configurator writes `.env` atomically, stores ignored backups under
`..\Logs\ConfigBackups`, creates the ODJ directory, and can validate paths, LDAP
TCP connectivity, and the current run-as identity. It never runs
`djoin /provision`.

```powershell
.\Setup-IronAPI.ps1 -InitialSetup
.\Setup-IronAPI.ps1 -Validate
```

LDAP searches and ODJ provisioning both run as the IronAPI process identity;
no separate domain password is configured or stored. That identity needs AD
read permission and the delegated rights to create and reuse computer accounts.

Install dependencies from `Api`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Start the application in the foreground from the repository root. Listener
settings come from `.env` and can be edited by SetupWeb:

```powershell
& ".\3. Start-IronAPI.ps1"
```

The root script forwards to `Core\Tools\Start-IronAPI.ps1`, which reads `.env`,
verifies the virtual environment, creates the `Data`, `ODJ\pending`, and
`Logs` folders, and runs uvicorn. Use `-BindHost`, `-Port`, or `-AccessLog` to
override `.env` for a single run.

After verifying the foreground start, stop it with `Ctrl+C` and optionally
install IronAPI as an automatically started Windows Service:

```powershell
& ".\4. Install-IronAPIService.ps1"
```

The installer recommends a dedicated `DOMAIN\user` identity, securely requests
its password, grants **Log on as a service**, and configures restart recovery.
It never selects `LocalSystem` by default: that high-privilege identity is
available only after an explicit warning and typed confirmation. At the end,
the installer asks whether to start the service.

The selected identity must be able to read the application, virtual
environment, and base Python installation and modify `Core\Data`, `Core\ODJ`,
and `Core\Logs`. It also performs LDAP searches and `djoin.exe` domain
operations. Service output is appended to `Core\Logs\IronAPI-service.log`.

## API

Open `http://<ironapi-host>:8000/` in a browser to view the read-only deployment
dashboard. It refreshes every 10 seconds and supports local search and status
filtering. Click a deployment ID to open `/dashboard/<deployment_id>`, a wide
detail view with hardware identity, timing, stage history, post-install results,
errors, and WinPE network diagnostics.

WinPE sends each completed `image_apply`, `driver_injection`, and
`postinstall_copy` network aggregate immediately with
`PUT /api/deploy/<deployment_id>/network-diagnostics/stages/<stage>`. Each
stage report and the final aggregate report use up to three attempts: the first
is immediate, then retries wait 5 and 10 seconds. Before WinPE exits or reports
a terminal error it sends the overall network report with
`PUT /api/deploy/<deployment_id>/network-diagnostics`. The final report
contains the SMB and IronAPI route adapters, the real SMB connection
duration/error, and timing for existing IronAPI requests. A stage that was
already accepted is not resent in the final report; a stage whose immediate
report failed remains available as a final-report fallback. Per-second ping
samples remain only in WinPE memory; the database stores aggregates only.

### Browser accounts and permissions

IronAPI browser pages require a signed-in account. Configure the initial
superadmin login and password locally in SetupWeb, save the configuration, and
then restart IronAPI. SetupWeb stores only a salted PBKDF2-SHA256 password hash
in `Data\auth-bootstrap.json`; the password is never written to `Api\.env` or
returned to the browser.

The superadmin exclusively manages `/users` and `/access-control`. The API Web
interface supports Russian and English; the selector is stored in browser
local storage and does not change API values. Regular
accounts have no roles and receive exact permissions for **Dashboard**,
**Windows images**, **Post-install software**, and **Image settings**. Every protected HTML route and its
backend API enforce the same permission; hiding a sidebar item is not treated
as authorization. Blocking an account, changing its password, or changing its
permissions revokes its existing sessions.

Browser sessions are stored server-side in the IronAPI database. The cookie is
`HttpOnly` and `SameSite=Strict`, with an eight-hour idle timeout and seven-day
absolute lifetime. SetupWeb offers two explicit access modes. Direct HTTP uses
the selected network bind address and `IRONAPI_COOKIE_SECURE=false`. HTTPS
through an external reverse proxy forces IronAPI to HTTP on `127.0.0.1:8000`
and sets `IRONAPI_COOKIE_SECURE=true`. IronDeploy does not install or configure
the reverse proxy and its server certificate, and it does not infer HTTPS from
`X-Forwarded-Proto`.

WinPE first reads `/api/deploy/auth/policy`. The superadmin selects one of three
authorization modes on **Access control**: a deployment-only account and
password (the default), a shared 6-10 digit PIN, or no operator credentials.
The PIN is stored only as a salted PBKDF2 hash. Five incorrect attempts from
one client address cause a 15-minute lockout. The credential-free mode carries
an explicit warning because anyone who can boot the image can start a disk-0
deployment.

Every mode still issues one random, short-lived bearer that can create and
control exactly one deployment. PIN and credential-free requests use a hidden
internal principal that cannot log in to API Web and is not returned by the
user-management API. The token is stored hashed in the database, changes
capabilities from WinPE to post-install, and is revoked on completion, failure,
or timeout. Account-mode tokens are also revoked on account blocking, password
change, or permission change. Deployment endpoints enforce
`IRONAPI_ALLOWED_CLIENT_NETWORKS` and never use browser cookies. The policy and
PIN remain server-side and are never written into `deploy.config.ps1` or the
WinPE image.

By default, the bearer must be consumed by `/begin` within 10 minutes. After
`/begin`, both the bearer and deployment share one hard 90-minute deadline
covering WinPE, Windows Setup, and post-install. API activity and the
post-install phase transition do not extend it. SetupWeb **Additional** can
configure the authorization window from 5–30 minutes and the total deployment
deadline from 30–240 minutes.

The read-only SMB username and password stay server-side in `Api\.env`. They
are returned only after `/begin` binds the bearer to a deployment. The final
`unattend.xml` is generated for that deployment and returned through the same
authenticated API instead of being read directly by WinPE. SetupComplete and
`postinstall.ps1` are likewise served from `ServerTemplates\PostInstall`
through deployment-owned endpoints; they are not exposed by SMB.
For HTTPS reverse-proxy mode, SetupWeb lets the operator either keep the
backward-compatible certificate-validation bypass or enable validation with an
uploaded X.509 certificate. Self-signed mode trusts the uploaded server
certificate; CA mode trusts the uploaded root/private CA certificate. SetupWeb
rejects malformed, not-yet-valid, and expired certificates. The selected
certificate is embedded in the ignored WinPE configuration, installed in the
WinPE LocalMachine Root store before deployment API calls, and carried into the
installed Windows LocalMachine Root store before the post-install completion
call. Normal TLS validation still checks the server name and certificate chain.

### Image settings

Open `http://<ironapi-host>:8000/image-config`, or follow **Image settings** from
the dashboard, to edit the
typical Windows image defaults from a browser:

* local administrator account name and password,
* built-in Administrator password,
* enable built-in Administrator / disable setup local admin policy,
* Windows time zone.

The page writes the same files SetupWeb creates —
`WinPE\Runtime\deploy.config.ps1` (post-install account policy) and
`ServerTemplates\Unattend\unattend-win11-template.xml` (time zone plus the
`localadmin` account name and plain-text password). Only the affected
lines/elements are changed, and each file is backed up under
`Logs\ConfigBackups` before it is replaced. SMB credentials are unaffected
because they live in `Api\.env`.

The local administrator password is stored in plain text inside the server-only
unattend template (`<Password><Value>`); it is not set anywhere else. The file
is outside the SMB share. IronAPI substitutes the computer name and returns the
final unattend only to the deployment that owns the Bearer. Leaving a password
field blank keeps the currently saved value.

The built-in Administrator password is written to the unattend
`<AdministratorPassword>` element (created on demand, so an empty field never
writes a placeholder). Windows applies it during OOBE and scrubs the value from
the deployed `C:\Windows\Panther` copy afterwards. Setting it closes the case
where enabling the built-in Administrator would otherwise activate an account
with a blank password; the page shows a warning when that account is enabled
without a password on record.

The page and its `GET`/`POST /api/image-config` endpoints are reachable from the
same configured client networks as the dashboard. `POST` additionally requires
a same-origin request and the `X-Requested-With: IronDeploy` header sent by the
page, which blocks cross-site (CSRF) writes.

The page also provides separate **Rebuild WIM** and **Rebuild ISO** buttons.
They start `Tools\Build-IronDeployWinPE.ps1 -Target Wim` or `-Target Iso` as a
single background job; a second build cannot start while one is running.
IronAPI must run as Administrator because DISM and ISO creation require an
elevated process. Build output is written under `Logs\WinPEBuilds`.

### Windows images

Open `http://<ironapi-host>:8000/images`, or follow **Windows images** from the
dashboard, to manage the contents of `Share\Images`. The page accepts streamed
uploads of `.wim` and `.esd` files, discovers files copied into the directory
outside IronAPI when **Refresh** is pressed, and uses DISM to show every image
index and Windows edition name.

Before an ESD upload starts, the page asks whether to keep it as ESD only or
automatically start the longer ESD-to-WIM conversion after upload. This choice,
rebuild confirmations, and file renaming all use in-page controls rather than
browser alert dialogs. Status notifications are fixed to the viewport and do
not move the image cards.

Browser uploads and ESD conversions use temporary names and publish the final
`.wim`/`.esd` name atomically only after completion. Once image metadata exists,
the deployment API hides any directly copied image until Refresh has completed
a successful DISM inspection and the file size matches the inspected size.
Both an active browser upload and an active ESD-to-WIM conversion can be
cancelled from the page; their incomplete temporary files are removed.

Each image can have its own default deployment index. IronAPI stores the index
map in `Share\Images\.irondeploy-images.json` and returns it to WinPE through
the authenticated catalog and final deployment manifest.

WIM files can also be renamed from their image card. Renaming preserves the
saved default index, requires the `.wim` extension, and never overwrites an
existing image.

The dashboard, Windows images, Post-install software, Drivers, and Image
settings pages share a persistent, collapsible left navigation panel. The top
action bar remains fixed while the page scrolls.

### Post-install software

Open `http://<ironapi-host>:8000/programs`, or follow **Post-install software**
from the dashboard, to manage the contents of `Share\Programs`. The page
accepts streamed uploads of `.exe` and `.msi` installers and stores an
optional ordered launch-argument array for each one. EXE arguments are opaque:
values such as `/S`, `--quiet`, `install`, `ALLUSERS=1`, and
`URL=https://example.test` are accepted without a switch-name whitelist. MSI
public properties are stored separately in `msi_properties`; an MSI argument
containing `=` is always rejected. This includes malformed property-like values
such as `BAD-NAME=1`, `1PROPERTY=1`, and `=VALUE`.

MSI property names are normalized to uppercase and must match
`^[A-Z_][A-Z0-9_.]*$`; names are not selected from a whitelist. In the first
format version, each argument and each property value is one command-line item,
so whitespace, quotes, NUL, CR, and LF are rejected. A program can have at most
100 arguments of at most 512 characters each and 4096 characters in total.
MSI properties have the same item count and total limits; names are limited to
72 characters and values to 512. Arguments and properties can be edited, and
programs can be renamed or deleted from their cards. The upload form can assign
a different file name before the upload starts. Files copied into
`Share\Programs` manually are discovered on **Refresh**.
Uploads are limited to 5 GiB per program.

The argument, MSI-property, and SHA-256 map is stored in
`Share\Programs\.irondeploy-programs.json`. WinPE gets names, sizes, types,
arguments, MSI properties, and hashes from the authenticated API catalog; the
deployment GUI lists every available program as a checkbox under
**Post-install software**. Metadata version 3 writes `arguments` as an array
and `msi_properties` as an object:

```json
{
  "version": 3,
  "programs": {
    "agent.msi": {
      "arguments": ["/qn", "/norestart"],
      "msi_properties": {
        "ALLUSERS": "1",
        "REBOOT": "ReallySuppress"
      }
    }
  }
}
```

Version 2 records with a string `arguments` value remain readable. IronAPI
splits that legacy value only on whitespace, validates every resulting item,
and writes the structured version on the next metadata save.

The final manifest fixes the selected image/index, selected programs, and
post-install account flags. SetupComplete and the generic post-install script
are downloaded through the same authenticated HTTP(S) API. Selected program
bytes are copied from SMB to `C:\IronDeploy\Programs` together with a
`programs.json` manifest during the `postinstall_copy` stage, and
`postinstall.ps1` installs them during Windows SetupComplete — `.msi` files
as `msiexec.exe /i <package> <arguments> <NAME=VALUE properties>` and `.exe`
files directly with their stored argument arrays. The script revalidates the
manifest and joins only validated items with spaces for Windows PowerShell 5.1
`Start-Process -ArgumentList`. No shell evaluates the resulting command line.
WinPE verifies SHA-256 after copying,
and postinstall verifies it again immediately before execution. Exit codes 0
and 3010 are treated as success. A hash failure is reported as status `failed`
with reason `hash_mismatch`, displayed as **SHA-256 mismatch**, and the
installer is not started.

### Drivers

Open `http://<ironapi-host>:8000/drivers`, or follow **Drivers** in the
navigation, to manage `Share\Drivers`. Vendors are top-level directories and
can be created, renamed, or deleted. Each package is stored as
`Share\Drivers\<vendor>\<model>` and can also be renamed or deleted.

The upload form uses a browser folder picker and preserves every uploaded
file's relative path. Files are streamed into a hidden staging directory; the
model package becomes visible only after all files arrive and the upload is
finalized. Empty uploads and folders without an INF file are rejected. Package
cards show total size, file count, and recursive INF count.

SetupWeb **Additional** configures file-count, directory-depth, final-path,
unfinished-upload, TTL, and free-space limits. Changes apply after restarting
IronAPI. Superadmins can open **Info** to see the active limits, free space on
the `Share\Drivers` volume, and unfinished uploads. An upload becomes
**abandoned** when its `updatedAt` is older than the configured TTL; it is never
removed automatically and can be deleted explicitly from the Info page.

The authenticated WinPE catalog contains packages with at least one INF file.
The deployment GUI presents them in a scrollable third-step tree grouped by
expandable vendor headings. Packages use mutually exclusive checkboxes:
checking one clears every other package and **Do not install drivers**.
The final manifest returns either one validated `vendor\model` package or
`null`; WinPE verifies its size and INF count, then runs
`DISM /Add-Driver /Recurse` only against that package. A null selection skips
`driver_injection`, so deployments without drivers remain supported.

An ESD can be converted to a WIM from its row or immediately after upload. The
conversion exports every ESD index in a background job and writes output under
`Logs\ImageConversions`. The source ESD is retained. IronAPI must run as
Administrator because both image inspection and conversion use DISM.

Deployments that reach their configured total deadline while still in `begin`
are automatically marked `failed`. The failure timestamp is fixed at exactly
that deadline after `started_at`, and any running stage is failed at the same
timestamp, so live timers stop instead of continuing indefinitely.

```http
GET /api/deploy/suggest-name?serial_number=PF4ABC12&mac_address=AA%3ABB%3ACC%3ADD%3AEE%3AFF
```

The response contains `last_domain_name`, `suggested_name`, and
`known_computer_names` when the same serial number or MAC address was seen in
previous deployments. The API only provides hints; WinPE asks the operator for
the final computer name.

Default naming scheme is `pc` + five digits, for example `pc00485`.

If LDAP is not configured or unavailable, the endpoint returns `503 Service
Unavailable` without suggesting a name.

When LDAP is configured, it searches computer objects by `cn` and
`sAMAccountName`, finds the largest matching number, and returns the next name.
The configured start value, default `pc00001`, is returned only after a
successful LDAP search finds no matching computers.

LDAP binds through Windows ADSI without an explicit username or password, so
the search uses the Windows identity running IronAPI. In service mode this is
the configured service account; in foreground mode it is the account that
started `3. Start-IronAPI.ps1`.

LDAP is also used before ODJ provisioning to determine whether the exact
computer account already exists. The configured base DN must include every OU
that can contain managed computer accounts.

### Begin deployment

Except for the policy and authorization calls, every `/api/deploy/...` request shown below includes
`Authorization: Bearer <deployment-token>`.

```http
GET /api/deploy/auth/policy
```

Account mode uses the existing endpoint:

```http
POST /api/deploy/auth/login
Content-Type: application/json

{
  "username": "winpe-operator",
  "password": "..."
}
```

PIN and credential-free modes use:

```http
POST /api/deploy/auth/authorize
Content-Type: application/json

{"mode": "pin", "pin": "123456"}
```

or `{"mode": "none"}`. The returned token can call `/begin` once. A second
deployment requires a new authorization.

```http
POST /api/deploy/begin
Content-Type: application/json

{
  "computer_name": "pc00042",
  "serial_number": "PF4ABC12",
  "model": "ThinkPad T14 Gen 2",
  "mac_address": "AA:BB:CC:DD:EE:FF",
  "domain_join": true
}
```

WinPE reads `serial_number` from `Win32_BIOS` and falls back to
`Win32_ComputerSystemProduct.IdentifyingNumber`. `model` comes from
`Win32_ComputerSystem.Model`, falling back to `Win32_ComputerSystemProduct` and
`Win32_BaseBoard.Product`; it is optional, so images built before hardware-model
reporting keep working and their deployments show an empty model. The API
records the request source address as `ip_address`, creates a deployment with
status `begin`, and returns its sequential `deployment_id`:

```json
{
  "deployment_id": 1,
  "status": "begin",
  "started_at": "2026-06-29T12:00:00Z",
  "completed_at": null
}
```

### Complete deployment

```http
POST /api/deploy/1/complete
```

The endpoint changes a deployment that is still within its configured total
window (90 minutes by default) to `completed` and records `completed_at`. Any
stage still marked as running is completed at the same server timestamp.
After completion the Bearer is revoked for every other API operation. For five
minutes, an exact repeat of `POST /api/deploy/{same-id}/complete` with that same
Bearer can only return the existing terminal deployment as a delivery receipt;
it cannot replace the report or restore WinPE or post-install permissions.

### Catalog, final manifest, and WinPE errors

WinPE registers the deployment before the SMB share and Windows image are
selected, so early share and image-selection failures can be recorded. It gets
available images, indexes, programs, argument arrays, MSI properties, and
hashes from:

```http
GET /api/deploy/catalog
```

After the operator chooses an image and programs, WinPE requests the final plan:

```http
POST /api/deploy/1/manifest

{
  "image_name": "win11.esd",
  "program_names": ["agent.msi", "browser.exe"]
}
```

The response contains the selected image size/format/readiness/indexes/default
index, each selected program's size/type/arguments/msi_properties/SHA-256, and
the postinstall local-account name and enablement flags. The manifest request
also updates the deployment and computer inventory with the selected image.

If WinPE stops with an error after registration, it reports the message with:

```http
POST /api/deploy/1/error
```

The API stores the message on the deployment and, when a stage is provided, on
the failed stage row. The dashboard displays both the failed stage and the
stored error text.

### Computers inventory

IronAPI keeps an aggregate `computers` inventory separate from the deployment
journal. Existing deployments are backfilled into this table during database
initialization, and future `/api/deploy/begin` and `/api/deploy/{id}/image`
requests update the matching computer row.

```http
GET /api/computers
```

The dashboard has a Computers view with searchable cells and click-to-copy
values.

### Offline Domain Join

For deployments registered with `"domain_join": true`, WinPE uses this flow:

```http
POST /api/deploy/1/domain-join/provision
GET  /api/deploy/1/domain-join/blob
POST /api/deploy/1/domain-join/acknowledge
```

Provisioning runs the equivalent of:

```text
djoin.exe /provision
  /domain example.test
  /machine <computer_name>
  /machineou "OU=Workstations,OU=Clients,DC=example,DC=test"
  /savefile "<IronDeploy>\ODJ\pending\<computer_name>.txt"
```

For a new computer account, IronAPI includes the configured `/machineou`.
For an existing account, it instead adds `/reuse` and omits `/machineou`, so
the existing AD object remains in its current OU. Reuse resets the computer
account password and requires the IronAPI process identity to have the
appropriate rights on that existing object. If LDAP cannot reliably determine
whether the account exists, provisioning stops without running `djoin.exe`.

LDAP search and `djoin.exe` use the same IronAPI process identity. It needs AD
read permission and the delegated rights to create computer accounts in the
configured OU and reuse existing managed accounts. The domain, OU, blob
directory, executable path, and timeout can be changed with the
`IRONAPI_ODJ_*` settings shown in `.env.example`.

The endpoints are restricted to the IP address that registered the deployment.
WinPE downloads the blob to its RAM drive and verifies that it is non-empty.
WinPE then creates a temporary offlineServicing unattend file containing the
blob and applies it with `dism /Image:C:\ /Apply-Unattend`. After DISM
succeeds, WinPE acknowledges the blob and IronAPI deletes it.

A blob is a computer-account secret, so it is never kept indefinitely when the
acknowledgement does not arrive. IronAPI also deletes it when the deployment
fails, when it times out, and when it completes without having acknowledged.
Anything still left in `ODJ\pending` past `IRONAPI_ODJ_BLOB_MAX_AGE_MINUTES` is
purged as an orphan, which covers clients that stopped reporting entirely.
This is a short provision-to-download window, independent of the total
deployment timeout. The default is five minutes.
Within that window a retry reuses the existing blob instead of re-running
`djoin.exe`; past it, IronAPI re-provisions, because every provision resets the
computer account password and an expired blob may no longer be valid.

On the client side the blob and the temporary unattend that embeds it are
registered as secret artifacts and deleted from the WinPE RAM drive on every
exit path, including failures between download and application.

### WinPE deployment stages

```http
POST /api/deploy/1/stages/image_apply/start
POST /api/deploy/1/stages/image_apply/complete
POST /api/deploy/1/stages/image_apply/fail
POST /api/deploy/1/stages/domain_join/skip
```

WinPE reports stage boundaries on a best-effort basis. IronAPI records
`started_at` and `completed_at` using server time, so deployment duration does
not depend on the WinPE clock.

Supported stages are:

```text
disk_partitioning
image_apply
driver_injection
deployment_state
unattend_generation
unattend_apply
domain_join
postinstall_copy
boot_files
windows_setup
```

Stage events are idempotent. Starting a new stage completes any older running
stage at the new stage's start time. This recovers from a lost `complete`
response without interrupting deployment. Stage reporting failures never stop
the WinPE script.

### List deployments

```http
GET /api/deployments?limit=500
```

Returns the newest deployments, summary counts (including `failed`), stage
history, and the fields displayed by the browser dashboard. Before reading,
the endpoint finalizes deployments whose one-hour deadline has passed.

### Get one deployment

```http
GET /api/deployments/{deployment_id}
```

Returns the same deployment fields, stage history, and post-install results used
by the detail dashboard. A missing deployment returns `404`.
