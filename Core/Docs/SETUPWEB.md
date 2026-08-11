# IronDeploy SetupWeb

SetupWeb is the local initial-configuration UI. It reads the current
IronDeploy settings, validates operator input, and writes configuration files
to the components that own them.

SetupWeb is not the IronAPI management interface. It does not run a deployment,
start IronAPI, install the Windows service, or rebuild WinPE.

## Starting SetupWeb

From the repository root, run step 3:

```powershell
& ".\3. Start-IronDeploySetupWeb.ps1"
```

The launcher forwards to `Core\SetupWeb\Start-IronDeploySetupWeb.ps1`. It asks
for administrator approval through Windows UAC, then creates
`Core\SetupWeb\.venv` on demand, installs its requirements, selects a random
localhost port, and opens the default browser.

Useful launcher options are:

```powershell
& ".\3. Start-IronDeploySetupWeb.ps1" -NoBrowser
& ".\3. Start-IronDeploySetupWeb.ps1" -SkipDependencyInstall
```

`-NoBrowser` prints the URL instead of opening it. Dependency installation can
be skipped only when the SetupWeb virtual environment is already current.

## Local session security

SetupWeb listens only on `127.0.0.1` and accepts only loopback clients. Every
start creates:

- a random port;
- a one-time bootstrap token with a short lifetime;
- an HttpOnly, SameSite session cookie;
- a CSRF token required for state-changing requests.

Host and Origin headers are restricted to the selected localhost port. The
bootstrap token is consumed when the first valid browser session is created.
Finishing setup destroys the session and normally stops the SetupWeb process.

## What SetupWeb configures

The UI owns the first-time settings needed by both IronAPI and WinPE:

- initial IronAPI superadmin username and password;
- listener/access mode, address, port, and allowed client networks;
- SMB share path and configured account details; the administrator must create
  the share and account separately and grant read-only access in both SMB and
  NTFS permissions;
- computer naming, LDAP, and Offline Domain Join settings. Active Directory is
  optional: leaving the LDAP server and base DN empty disables name checks, and
  leaving the Offline Domain Join domain and OU empty disables domain joins.
  SetupWeb keeps these fields empty rather than substituting the example values;
- deployment and authorization timeouts;
- driver-upload safety limits;
- WinPE API address and certificate trust;
- WinPE image, driver, program, and drive-letter paths;
- local administrator policy for post-install;
- Windows time zone in the unattend template.

`Allowed client networks` is part of the main **Service endpoint** settings.
Enter comma-separated IPv4 or IPv6 CIDRs to restrict access. Leaving the field
empty writes `IRONAPI_ALLOWED_CLIENT_NETWORKS=` and allows clients from every
network; the `(?)` help beside the field repeats this behavior. Loopback is
always allowed even when CIDRs are configured.

The later IronAPI `/image-config` page owns the operational image-apply choice.
It stores `direct` or `staged` as `IRONAPI_IMAGE_APPLY_MODE` in
`Core\Api\.env`. SetupWeb preserves that server-side setting when it rewrites
the initial configuration and never copies it into `deploy.config.ps1`.

The UI loads existing values when reopened. Leaving a password field empty
keeps the current stored hash or secret where the form explicitly supports
that behavior.

## Files written

| File | Content |
| --- | --- |
| `Core\Api\.env` | IronAPI listener, SMB, image-apply strategy, LDAP, ODJ, storage, and timeout settings. |
| `Core\WinPE\Runtime\deploy.config.ps1` | Credential-free WinPE runtime settings. |
| `Core\ServerTemplates\Unattend\unattend-win11-template.xml` | Server-side Windows answer-file settings. |
| `Core\Data\auth-bootstrap.json` | Initial superadmin name and PBKDF2-SHA256 password hash. |

Existing `.env`, WinPE config, and unattend files are backed up under
`Core\Logs\ConfigBackups` before replacement. Writes use temporary files followed
by atomic replacement so an interrupted save does not leave a partially
written configuration.

`auth-bootstrap.json` does not contain the plaintext password. IronAPI imports
the bootstrap identity into SQLite at startup.

## Ownership rules

SetupWeb writes settings to the component that consumes them:

- SMB credentials stay in `Core\Api\.env`; they are never embedded in WinPE.
- The image-apply strategy stays in `Core\Api\.env`; IronAPI returns it in the
  deployment manifest instead of SetupWeb embedding it in WinPE.
- SetupWeb can publish the fixed local `Core\Share` folder and grant read access
  to an existing `SERVER\user` or `DOMAIN\user` account. It does not create the
  account or configure shares on remote servers. The saved UNC path may use the
  current server's hostname, FQDN, or IPv4 address.
- WinPE stores the API address and trust policy, not deployment authorization
  credentials.
- Unattend remains under `Core\ServerTemplates`, where IronAPI can return it only to
  an authorized deployment.
- Generated configuration and backups remain ignored by Git.

These boundaries are summarized in [ARCHITECTURE.md](ARCHITECTURE.md).

## Validation

The Validate action runs `Core\Tools\Test-IronDeploy.ps1` without starting IronAPI.
It checks required files and configuration consistency and returns PowerShell
stdout, stderr, and the exit code to the browser.

Validation proves that the local files agree; it cannot prove that a future
target machine can boot, reach the server, access Active Directory, or apply a
specific Windows image and driver package.

## After setup

Finish closes the local SetupWeb session. The next flow step is the foreground
IronAPI test:

```powershell
& ".\4. Start-IronAPI.ps1"
```

After the foreground test succeeds, IronAPI may be installed as a service with
step 5. See [API.md](API.md) for startup, service identity, and runtime
behavior.

## Reopening configuration

Run step 3 again whenever configuration must change. SetupWeb reads the current
files and creates backups on save. Restart IronAPI after changing settings it
loads at process startup. Rebuild the WinPE artifact only when a setting copied
into `deploy.config.ps1` must be delivered to new boot media.
