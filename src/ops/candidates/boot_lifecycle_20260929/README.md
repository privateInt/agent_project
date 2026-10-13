# Boot lifecycle candidate — inactive source

These files are reviewed candidate replacements, not an importable deployment.
The active worker code is unchanged. Do not copy this source into ops_platform
or alter either installed binding manually. A guarded stop/code/policy migration
adapter and isolated native acceptance are still required.

Host checker command for reference (the user server has completed 53/53;
no rerun is required for the result-recording patch):

```bash
python3 -B ops_tools/check_boot_candidate.py
```

It verifies the pinned original code/fixtures and candidate bytes, copies them
to a private temporary project, runs fixed synthetic tests and verifies that
the original 60-file code hash stayed unchanged. Temporary source includes the
new module in its 61-file worker hash. Runtime dependencies are not moved outside
the worker hash to bypass binding checks.

The manifest's paths describe future replacement targets. The package installer
only installs this inactive directory and its checker/docs. No service start,
production state/credential access, Docker command, unit/linger change or reboot
is part of applying or checking this candidate.

Core supports a consumed boot receipt, verified clean exit, different-boot
reentry, same-boot refusal, preserved durable stop/recovery and bounded metadata
readiness. Policy transition, initial arming and guarded binding migration are
not implemented here. The current active persistence diagnostics retain their
existing implementation-gap results correctly.

The user server reported all 53 selected cases passed on Python 3.10.19,
including the native private Unix socket case unavailable locally. Failures and
unavailable lists are empty, temporary cleanup passed, and the original code was
preserved. Boot identities, manager and Docker metadata remain synthetic; this
does not verify native systemd/Docker boot, production health or activation.
The supplied output has no execution timestamp. Source and manifest stay pinned.

See [the contract and server record](../../../../docs/history/ops-evidence-catalog.md#auto-refresh-boot-candidate-20260929).
Guarded policy/code/binding migration and native lifecycle acceptance remain next.
