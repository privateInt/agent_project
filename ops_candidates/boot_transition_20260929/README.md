# Stopped code/policy transition — inactive candidate

This directory is not imported by the running worker. The installed runtime and
the previous boot_lifecycle_20260929 candidate remain unchanged. There is no
production transition CLI. Do not copy candidate files into ops_platform.

transition.py implements a fixed reviewed source transaction using the existing
owned stop adapter, exit/cgroup/lease proofs, private before/after backups,
paused commit, explicit finish and rollback. It never starts a service or enables
a link. The new runtime candidate gates incomplete transactions and refuses boot
start/resume until a future explicit arming/activation adapter exists.

Rollback keeps a binding mismatch until original code/service bytes and the
terminal journal are durable; the worker binding is restored last. Unknown or
manually edited bytes, different Python/daemon/Ops identity, pending work and new
stop requests are not silently accepted.

Isolated checker command for reference (the user server completed 33/33;
no rerun is required for the result-recording patch):

```bash
python3 -B ops_tools/check_boot_transition.py
```

It verifies hashes before copying and runs 33 real file/process/lease tests with
synthetic manager/Docker metadata. No production state/key read, Docker/systemctl
control, application API, build, inference, restart or linger change occurs.
Local tests passed under default and 0002 masks. The user server reported all
33 selected transition cases passed on Python 3.10.19, with empty failures and
unavailable lists, active source preservation and temporary cleanup passed.
The paused_and_disabled result refers to isolated test installations, not the
production service. Actual unit/Docker acceptance and initial arming remain
pending. The checker does not read production health or establish uptime.
No execution timestamp was supplied; source and manifest stay pinned.
The pinned base has 60 runtime files; the candidate has 61. All runtime dependencies
remain in the worker code hash. The external controller is an inactive management
tool, not an untracked worker dependency.

See [the guide](../../docs/history/ops-evidence-catalog.md#auto-refresh-boot-transition-20260929).
Next: verified paused-to-armed explicit start, then isolated native unit/Docker
acceptance. Actual production activation and logout/reboot remain separate.
