# IronDeploy Plan/Roadmap

## Local administrator safety

- Add a post-install safety guard before disabling the setup local admin account.
- The guard should verify that the machine is domain-joined and that an alternate administrator path exists.
- If domain join is disabled or failed, keep the setup local admin account enabled to avoid locking out the machine.
- Surface this state clearly in ConfigureMaster and SetupWeb before allowing an aggressive cleanup profile.
- Define how the built-in Administrator password is set or managed when that account is enabled.