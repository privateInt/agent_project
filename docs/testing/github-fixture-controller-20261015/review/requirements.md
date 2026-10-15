# Fixture controller task C01

User authorizes implementation in this separate development workspace only.
Workspace: /tmp/agent-controller-dev-ur5octch/workspace
Original repository and previous evidence roots are read-only task inputs.
Parent source pin: e1eff75b47cdf3fae1088d51d35aaea701f81477 (PR #13).
Source provenance and preservation baseline are in the parent directory.

Required behavior:
- Reuse unchanged github_fixture_collector.collect_fixture_assignment and all
  existing github_fixture_ledger APIs for assignment/artifact storage.
- Connect assignment -> fixed fixture collection -> artifact seal -> readback.
- Persist a definitive pre-execution preparation failure with no artifact, and
  observe it after closing/reopening DBs. Terminal failure cannot become success
  merely because a subsequent call supplies a repaired directory.
- Distinguish ambiguous invocation/DB response loss from confirmed preparation
  failure. Never infer that a failed COMMIT response means no committed write.
- Repeated calls and uncertain prior claims must never silently re-execute.
- Existing collector/ledger/source tests stay byte-for-byte unchanged. Do not
  rerun R03, R04, R15 or existing whole suites. New integration coverage is
  specifically for controller behavior and cannot be replaced by old results.
- Stdlib, Python 3.10 compatibility, own temporary DBs only. No network, keys,
  CLI, launcher/head mode, daemon, scheduler, worker activation or installation.
- All existing trust/native/merge flags remain false. Stored artifact is not PASS.

Proposed minimal contract for test_agent/reviewer to refine before implementation:
- New src/ops/tools/github_fixture_controller.py. Existing ledger schema1 rejects
  any extra tables, so controller state uses a separate caller-supplied SQLite
  connection/DB. Do not migrate or extend the existing artifact ledger schema.
- Caller explicitly initializes both empty dedicated DBs. No DB path inputs,
  connection creation, automatic schema repair or module import side effects.
- Public initialize_fixture_controller(control_connection),
  run_fixture_job(ledger_connection, control_connection, state_dir_fd,
                  expected_assignment_sha256, assignment_bytes), and
  read_fixture_controller_job(ledger_connection, control_connection, job_id,
                              expected_assignment_sha256).
- Ledger validates/assigns immutable input; controller durably claims each job
  once BEFORE collecting. Only a newly committed claim may collect. Repeat
  claims (including crash between claim and collection) remain outcome-unknown,
  never trigger a second child. Claim write outcome unknown prevents execution.
- State DB stores job+assignment pin, CLAIMED or PREPARATION_FAILED and a small
  allowlisted reason. Valid committed artifact in existing ledger is authoritative
  for artifact storage confirmation. Reads do not execute or silently repair.
- Confirmed preparation failures are only collector outcomes that unambiguously
  precede Popen, with artifact=None. Classify SPAWN_FAILED, cleanup/IO/unexpected
  failures and malformed collector responses conservatively as outcome unknown.
- On artifact record uncertainty retain the original artifact bytes in the caller
  result; a later independent read can observe a committed artifact. If not
  committed, keep unknown and do not re-collect. No generic retry loop.
- Existing ledger enforces pin/revision; new state API must preserve conflicts,
  reject caller transactions/foreign schema/unsafe connection flags, and use
  bounded parameterized reads/transactions with no silent retry.
- Returned report is small, fixed reasons only; raw paths/exceptions/SQL/private
  input never appear on stdout/stderr. Return original bytes only to caller.

D29 workflow: test_agent authors versioned acceptance plan; independent reviewer
reviews exact plan SHA before implementation. Reviewer writes no production or
test code. After approval coordinator implements module; test_agent implements
and executes only agreed new tests. Reviewer selects at most two meaningful D38
targets, then reviews actual pinned diff and evidence independently. No delegated
task may create additional threads, change model/settings, access credentials,
touch original checkout or rerun held sandbox investigation.

Expected changed files: one controller module, adjacent new controller tests,
one concise contract/test-plan and small new execution JSON in the separate
workspace. Existing preparation docs in original repository remain untouched.
