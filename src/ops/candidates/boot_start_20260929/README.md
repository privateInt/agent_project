# Initial boot start — inactive management candidate

This controller is not imported by the worker. It reuses the pinned stopped
transition runtime without changing either earlier candidate or active code.
There is no production CLI. Do not copy candidate files into ops_platform.

It verifies a committed paused boot transition, current binding and current
settings readback, then records an armed receipt, enables only the owned link,
and submits one explicit start. Acceptance needs a new manager invocation, live
worker identity, the consumed receipt, and a fresh accepted evaluation. Stop
wins; unknown starts are not repeated. Explicit check observes a prior intent;
cancel uses the existing owned stop and verifies exit/disable. Audit and recovery
are preserved. New arming after cancellation/recovery remains a separate step.

Only run the isolated checker:

```bash
python3 -B ops_tools/check_boot_start.py
```

Local: 32 checker cases and 5 copy-boundary cases passed; the checker passed
under default and 0002 masks. Host child processes, files, locks and boot receipt
consumption are real. Manager/Docker/API metadata is synthetic. No production
state/key read, API call, Docker/systemctl command, build, restart, inference,
linger change or production worker is requested. Server checker acceptance and
native unit/Docker transition acceptance remain pending. This does not verify
logout, reboot, long-running behavior or present production health.

See [the contract](../../../../docs/history/ops-evidence-catalog.md#auto-refresh-boot-start-20260929).
