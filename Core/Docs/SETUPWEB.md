# IronDeploy SetupWeb

SetupWeb is the local initial-configuration UI. It reads the current
IronDeploy settings, validates operator input, and writes configuration files
to the components that own them.

SetupWeb is not the IronAPI management interface. It does not run a deployment,
start IronAPI, install the Windows service, or rebuild WinPE.

## Starting SetupWeb

From the repository root, run step 2:

```powershell
& ".\2. Start-IronDeploySetupWeb.ps1"
```

The launcher forwards to `Core\SetupWeb\Start-IronDeploySetupWeb.ps1`. On
demand, that script creates `Core\SetupWeb\.venv`, installs its requirements,
selects a random localhost port, and opens the default browser.

Useful launcher options are:

```powershell
& ".\2. Start-IronDeploySetupWeb.ps1" -NoBrowser
& ".\2. Start-IronDeploySetupWeb.ps1" -SkipDependencyInstall
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
- SMB share path and read-only credential;
- computer naming, LDAP, and Offline Domain Join settings;
- deployment and authorization timeouts;
- driver-upload safety limits;
- WinPE API address and certificate trust;
- WinPE image, driver, program, and drive-letter paths;
- local administrator policy for post-install;
- Windows time zone in the unattend template.

The UI loads existing values when reopened. Leaving a password field empty
keeps the current stored hash or secret where the form explicitly supports
that behavior.

## Files written

| File | Content |
| --- | --- |
| `Core\Api\.env` | IronAPI listener, SMB, LDAP, ODJ, storage, and timeout settings. |
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
& ".\3. Start-IronAPI.ps1"
```

After the foreground test succeeds, IronAPI may be installed as a service with
step 4. See [API.md](API.md) for startup, service identity, and runtime
behavior.

## Reopening configuration

Run step 2 again whenever configuration must change. SetupWeb reads the current
files and creates backups on save. Restart IronAPI after changing settings it
loads at process startup. Rebuild the WinPE artifact only when a setting copied
into `deploy.config.ps1` must be delivered to new boot media.
