#!/usr/bin/env python3
"""Offline contracts for restart-safe worker PID adoption."""

from __future__ import annotations

import sys
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import bot_manager  # noqa: E402


def main() -> None:
    user_id = 77

    # A verified deterministic worker can be attached without spawning a
    # second process.  The adopted handle participates in normal status code.
    with (
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_expected_worker_pid", return_value=True),
        patch.object(bot_manager, "_proc_start_time", return_value="100"),
        patch.object(bot_manager.os, "pidfd_open", return_value=70),
    ):
        assert bot_manager.adopt(user_id, 45123, "rules") == (True, "adopted")
        assert bot_manager.is_running(user_id) is True
        assert bot_manager.process_id(user_id) == 45123
        assert bot_manager.status(user_id)["running"] is True

    # An adopted handle retains the Linux process start-time identity.  If the
    # PID is later reused, poll treats the old worker as dead and stop must not
    # signal the unrelated replacement process.
    reused_user = 79
    with (
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_expected_worker_pid", return_value=True),
        patch.object(bot_manager, "_proc_start_time", side_effect=("200", "200")),
        patch.object(bot_manager.os, "pidfd_open", return_value=71),
    ):
        assert bot_manager.adopt(reused_user, 45127, "rules") == (True, "adopted")
    with (
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_proc_start_time", return_value="201"),
        patch.object(bot_manager.os, "killpg") as reused_killpg,
    ):
        assert bot_manager.is_running(reused_user) is False
        assert bot_manager.stop(reused_user) == (False, "not_running")
    reused_killpg.assert_not_called()

    # Adopted workers use pidfd_send_signal when available, binding the signal
    # to the original task rather than the reusable numeric PID.
    pidfd_user = 82
    pidfd_key = (pidfd_user, "default")
    pidfd_proc = bot_manager._AdoptedProcess(45129, "300", 77)
    with bot_manager._lock:
        bot_manager._procs[pidfd_key] = pidfd_proc
        bot_manager._modes[pidfd_key] = "rules"
        bot_manager._desired_running[pidfd_key] = True
        bot_manager._generations[pidfd_key] = 1

    def finish_pidfd_wait(timeout=None):
        del timeout
        pidfd_proc.returncode = 0
        return 0

    with (
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_proc_start_time", return_value="300"),
        patch.object(pidfd_proc, "wait", side_effect=finish_pidfd_wait),
        patch.object(bot_manager.signal, "pidfd_send_signal") as pidfd_signal,
        patch.object(bot_manager.os, "killpg") as pidfd_killpg,
        patch.object(bot_manager.os, "close"),
        patch.object(bot_manager, "revoke_token"),
    ):
        assert bot_manager.stop(pidfd_user) == (True, "stopped")
    pidfd_signal.assert_called_once_with(77, bot_manager.signal.SIGTERM, None, 0)
    pidfd_killpg.assert_not_called()

    # Without pidfd, an adopted worker is deliberately not signalled: checking
    # start-time and then killpg would retain an unavoidable PID-reuse TOCTOU.
    fallback_user = 83
    fallback_key = (fallback_user, "default")
    fallback_proc = bot_manager._AdoptedProcess(45130, "400", None)
    with bot_manager._lock:
        bot_manager._procs[fallback_key] = fallback_proc
        bot_manager._modes[fallback_key] = "rules"
        bot_manager._desired_running[fallback_key] = True
        bot_manager._generations[fallback_key] = 1
    with (
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_proc_start_time", return_value="400"),
        patch.object(bot_manager.os, "killpg") as fallback_killpg,
        patch.object(bot_manager, "revoke_token"),
    ):
        assert bot_manager.stop(fallback_user) == (False, "stop_failed")
        assert fallback_key in bot_manager._transitions
    fallback_killpg.assert_not_called()
    fallback_proc.returncode = 0
    with patch.object(bot_manager, "revoke_token"):
        assert bot_manager.stop(fallback_user) == (True, "already_dead")

    # A member worker cannot be adopted because its loopback token was held by
    # the previous API process.  It must be replaced with a fresh process.
    with (
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_expected_worker_pid", return_value=True),
    ):
        assert bot_manager.adopt(user_id, 45123, "rules_ai") == (False, "token_unavailable")

    # A PID that is alive but no longer identifies our worker is never
    # shadowed or signalled automatically.
    bot_manager._procs[(user_id, "default")].returncode = 0
    bot_manager.stop(user_id)
    with (
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_expected_worker_pid", return_value=False),
    ):
        assert bot_manager.adopt(user_id, 45124, "rules") == (False, "pid_mismatch")
        assert bot_manager.terminate_pid(user_id, 45124) == (False, "pid_mismatch")

    # A verified orphan is bound to a pidfd before signalling.  No numeric PID
    # kill fallback is allowed after a separated identity check.
    def finish_orphan_wait(proc, timeout=None):
        del timeout
        proc.returncode = 0
        return 0

    with (
        patch.object(bot_manager, "_expected_worker_pid", return_value=True),
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_proc_start_time", return_value="101"),
        patch.object(bot_manager.os, "pidfd_open", return_value=88),
        patch.object(bot_manager.signal, "pidfd_send_signal") as orphan_pidfd_signal,
        patch.object(bot_manager._AdoptedProcess, "wait", finish_orphan_wait),
        patch.object(bot_manager.os, "killpg") as orphan_killpg,
        patch.object(bot_manager.os, "close"),
    ):
        assert bot_manager.terminate_pid(user_id, 45125) == (True, "stopped")
    orphan_pidfd_signal.assert_called_once_with(88, bot_manager.signal.SIGTERM, None, 0)
    orphan_killpg.assert_not_called()

    with (
        patch.object(bot_manager, "_expected_worker_pid", return_value=True),
        patch.object(bot_manager, "_pid_alive", return_value=True),
        patch.object(bot_manager, "_proc_start_time", return_value="102"),
        patch.object(bot_manager.os, "pidfd_open", side_effect=OSError("unsupported")),
        patch.object(bot_manager.os, "killpg") as unavailable_killpg,
    ):
        assert bot_manager.terminate_pid(user_id, 45126) == (False, "pidfd_unavailable")
    unavailable_killpg.assert_not_called()

    # A second timeout after SIGKILL is a failed stop, not proof of death.  The
    # reservation remains visible and blocks start until a later poll confirms
    # that the exact process has exited.
    class StubbornProcess:
        pid = 45128

        def __init__(self):
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            raise bot_manager.subprocess.TimeoutExpired(["worker"], timeout)

    stubborn_user = 80
    stubborn_key = (stubborn_user, "default")
    stubborn_proc = StubbornProcess()
    with bot_manager._lock:
        bot_manager._procs[stubborn_key] = stubborn_proc
        bot_manager._modes[stubborn_key] = "rules_ai"
        bot_manager._desired_running[stubborn_key] = True
        bot_manager._generations[stubborn_key] = 1
    with (
        patch.object(bot_manager.os, "getpgid", return_value=stubborn_proc.pid),
        patch.object(bot_manager.os, "killpg"),
        patch.object(bot_manager, "revoke_token"),
    ):
        assert bot_manager.stop(stubborn_user) == (False, "stop_timeout")
        assert stubborn_key in bot_manager._transitions
        assert bot_manager.start(stubborn_user, "rules") == (False, "transition_in_progress")
        stubborn_proc.returncode = 0
        assert bot_manager.stop(stubborn_user) == (True, "already_dead")
    assert stubborn_key not in bot_manager._transitions

    # Legacy subscription expiry is compatibility data only. Reconciliation
    # must not stop, replace, downgrade, revoke, reserve, or persist an AI worker.
    class FakeWatchdogProcess:
        pid = 45126
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return self.returncode

    watchdog_user = 78
    watchdog_key = (watchdog_user, "default")
    watchdog_proc = FakeWatchdogProcess()
    with bot_manager._lock:
        bot_manager._procs[watchdog_key] = watchdog_proc
        bot_manager._modes[watchdog_key] = "rules_ai"
        bot_manager._tokens[watchdog_key] = "self-use-ai-token"
        bot_manager._desired_running[watchdog_key] = True
        bot_manager._generations[watchdog_key] = 41

    reserve_transition = Mock()
    persist_transition = Mock()
    release_transition = Mock()
    with (
        patch.object(bot_manager, "revoke_token") as revoke_token,
        patch.object(bot_manager.subprocess, "Popen") as replacement_spawn,
    ):
        assert bot_manager._reconcile_access_modes(
            lambda _uid: 0,
            now=1,
            reserve_transition=reserve_transition,
            persist_transition=persist_transition,
            release_transition=release_transition,
        ) == {}
    reserve_transition.assert_not_called()
    persist_transition.assert_not_called()
    release_transition.assert_not_called()
    revoke_token.assert_not_called()
    replacement_spawn.assert_not_called()
    assert bot_manager.is_running(watchdog_user) is True
    assert bot_manager.status(watchdog_user)["automation_mode"] == "rules_ai"
    assert bot_manager._tokens[watchdog_key] == "self-use-ai-token"
    with (
        patch.object(bot_manager.os, "getpgid", return_value=watchdog_proc.pid),
        patch.object(bot_manager.os, "killpg"),
        patch.object(bot_manager, "revoke_token"),
    ):
        assert bot_manager.stop(watchdog_user) == (True, "stopped")

    # Exercise the API reconciliation decision itself with a tiny fake DB.
    # This catches the dangerous regression where a live-but-unrecognized PID
    # is silently shadowed by a second worker.
    with tempfile.TemporaryDirectory(prefix="xianyu-recovery-app-") as root:
        os.environ.update(
            {
                "SAAS_DB": str(Path(root) / "saas.db"),
                "SAAS_TENANTS_DIR": str(Path(root) / "tenants"),
                "SAAS_RESTORE_WORKERS": "0",
                "SAAS_COOKIE_SECURE": "0",
            }
        )
        # Importing the API is intentionally delayed until its test database
        # points at the temporary directory.
        import app  # noqa: PLC0415
        os.environ["SAAS_RESTORE_WORKERS"] = "1"

        class FakeDB:
            def __init__(self, row):
                self.row = {
                    "desired_state": "running",
                    "generation": 0,
                    "started_at": None,
                    "exit_code": None,
                    **row,
                }
                self.updates = []
                self.started_leases = []

            def list_worker_runtimes(self, desired_state=None):
                assert desired_state is None
                return [self.row]

            def acquire_control_lease(self, key, owner, **_kwargs):
                self.started_leases.append((key, owner))
                return "acquired"

            def release_control_lease(self, key, owner):
                self.started_leases.append(("released", key, owner))
                return True

            def get_user_by_id(self, user_id):
                return {
                    "id": user_id,
                    "expires_at": self.row.get("expires_at", time.time() + 3600),
                }

            def get_shop_account(self, user_id, account_id=None):
                return {"id": account_id, "enabled": 1}

            def persist_worker_runtime(self, user_id, account_id, **fields):
                self.updates.append((user_id, account_id, fields))
                self.row.update(fields)
                return self.row

            def get_worker_runtime(self, user_id, account_id=None):
                return self.row

        # Waiting-login is a durable running intent, not a failed stop. An API
        # restart must preserve it until the account has a verified snapshot.
        waiting_db = FakeDB(
            {
                "user_id": 5,
                "account_id": 1,
                "pid": None,
                "mode": "rules",
                "state": "waiting_login",
            }
        )
        with (
            patch.object(app, "db", waiting_db),
            patch.object(app, "bot_status", return_value={"connected": False}),
            patch.object(app, "bot_start") as waiting_start,
        ):
            app.restore_desired_workers()
        waiting_start.assert_not_called()
        waiting_update = waiting_db.updates[-1][2]
        assert waiting_update["desired_state"] == "running"
        assert waiting_update["state"] == "waiting_login"
        assert waiting_update["pid"] is None

        # A worker-auth marker is authoritative even when a snapshot exists:
        # recovery keeps desired running, waits for reauthorization, and makes
        # no new authentication attempt.
        reauth_db = FakeDB(
            {
                "user_id": 6,
                "account_id": 1,
                "pid": None,
                "mode": "rules",
                "state": "degraded",
            }
        )
        with (
            patch.object(app, "db", reauth_db),
            patch.object(
                app,
                "_read_auth_status",
                return_value={
                    "code": "session_expired",
                    "reauthorization_required": True,
                    "updated_at": 100.0,
                },
            ),
            patch.object(app, "bot_status", return_value={"connected": True}),
            patch.object(app, "bot_adopt") as reauth_adopt,
            patch.object(app, "bot_start") as reauth_start,
        ):
            app.restore_desired_workers()
        reauth_adopt.assert_not_called()
        reauth_start.assert_not_called()
        reauth_update = reauth_db.updates[-1][2]
        assert reauth_update["desired_state"] == "running"
        assert reauth_update["state"] == "waiting_login"
        assert reauth_update["last_error"] == "session_expired"

        adopted_db = FakeDB(
            {"user_id": 7, "account_id": 1, "pid": 7001, "mode": "rules"}
        )
        persisted = []
        with (
            patch.object(app, "db", adopted_db),
            patch.object(app, "bot_adopt", return_value=(True, "adopted")),
            patch.object(app, "bot_start") as start,
            patch.object(app, "_persist_worker_started", side_effect=lambda *args, **kwargs: persisted.append((args, kwargs))),
        ):
            app.restore_desired_workers()
        assert start.called is False
        assert persisted == [((7, "rules"), {"already_running": True})], persisted

        class StaleListedDB(FakeDB):
            def acquire_control_lease(self, key, owner, **kwargs):
                result = super().acquire_control_lease(key, owner, **kwargs)
                self.row["desired_state"] = "stopped"
                self.row["pid"] = None
                return result

        stale_listed_db = StaleListedDB(
            {"user_id": 15, "account_id": 1, "pid": 15001, "mode": "rules_ai"}
        )
        with (
            patch.object(app, "db", stale_listed_db),
            patch.object(app, "bot_adopt") as stale_adopt,
            patch.object(app, "bot_start") as stale_restore_start,
        ):
            app.restore_desired_workers()
        stale_adopt.assert_not_called()
        stale_restore_start.assert_not_called()

        stopped_pid_db = FakeDB(
            {
                "user_id": 16,
                "account_id": 1,
                "pid": 16001,
                "mode": "rules",
                "desired_state": "stopped",
                "state": "degraded",
            }
        )
        with (
            patch.object(app, "db", stopped_pid_db),
            patch.object(app, "bot_process_id", return_value=None),
            patch.object(app, "bot_stop", return_value=(False, "not_running")),
            patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")) as cleanup_stopped_pid,
        ):
            app.restore_desired_workers()
        cleanup_stopped_pid.assert_called_once_with(16, 16001, "default")
        stopped_cleanup = stopped_pid_db.updates[-1][2]
        assert stopped_cleanup["desired_state"] == "stopped"
        assert stopped_cleanup["state"] == "stopped"
        assert stopped_cleanup["pid"] is None

        failed_stopped_pid_db = FakeDB(
            {
                "user_id": 17,
                "account_id": 1,
                "pid": 17001,
                "mode": "rules",
                "desired_state": "stopped",
                "state": "degraded",
            }
        )
        with (
            patch.object(app, "db", failed_stopped_pid_db),
            patch.object(app, "bot_process_id", return_value=None),
            patch.object(app, "bot_stop", return_value=(False, "not_running")),
            patch.object(
                app, "bot_terminate_pid", return_value=(False, "pidfd_unavailable")
            ) as failed_cleanup,
        ):
            app.restore_desired_workers()
        failed_cleanup.assert_called_once_with(17, 17001, "default")
        failed_cleanup_state = failed_stopped_pid_db.updates[-1][2]
        assert failed_cleanup_state["desired_state"] == "stopped"
        assert failed_cleanup_state["state"] == "degraded"
        assert failed_cleanup_state["pid"] == 17001

        persist_failure_db = FakeDB(
            {"user_id": 14, "account_id": 1, "pid": 14001, "mode": "rules"}
        )
        with (
            patch.object(app, "db", persist_failure_db),
            patch.object(app, "bot_adopt", return_value=(True, "adopted")),
            patch.object(app, "_persist_worker_started", side_effect=RuntimeError("persist failed")),
            patch.object(app, "bot_stop", return_value=(True, "stopped")) as compensated_stop,
            patch.object(app, "bot_process_id", return_value=None),
        ):
            app.restore_desired_workers()
        compensated_stop.assert_called_once_with(14, "default")
        compensation = persist_failure_db.updates[-1][2]
        assert compensation["desired_state"] == "stopped"
        assert compensation["state"] == "degraded"
        assert compensation["pid"] is None

        mismatch_db = FakeDB(
            {"user_id": 8, "account_id": 1, "pid": 8001, "mode": "rules"}
        )
        with (
            patch.object(app, "db", mismatch_db),
            patch.object(app, "bot_adopt", return_value=(False, "pid_mismatch")),
            patch.object(app, "bot_start") as start,
        ):
            app.restore_desired_workers()
        assert start.called is False
        assert mismatch_db.updates[-1][2]["last_error"] == "worker_pid_mismatch"

        ai_db = FakeDB(
            {"user_id": 9, "account_id": 1, "pid": 9001, "mode": "rules_ai"}
        )
        with (
            patch.object(app, "db", ai_db),
            patch.object(app, "bot_adopt", return_value=(False, "token_unavailable")),
            patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")),
            patch.object(app, "bot_status", return_value={"connected": True}),
            patch.object(app, "bot_start", return_value=(True, "started")),
            patch.object(app, "_persist_worker_started") as persisted_ai,
        ):
            app.restore_desired_workers()
        persisted_ai.assert_called_once_with(9, "rules_ai", already_running=False)

        legacy_expired_ai_db = FakeDB(
            {"user_id": 11, "account_id": 1, "pid": 11001, "mode": "rules_ai", "expires_at": 0}
        )
        with (
            patch.object(app, "db", legacy_expired_ai_db),
            patch.object(app, "bot_adopt", return_value=(False, "token_unavailable")) as adopt_legacy_ai,
            patch.object(app, "bot_terminate_pid", return_value=(True, "stopped")) as terminate_legacy_ai,
            patch.object(app, "bot_status", return_value={"connected": True}),
            patch.object(app, "bot_start", return_value=(True, "started")) as start_legacy_ai,
            patch.object(app, "_persist_worker_started") as persist_legacy_ai,
        ):
            app.restore_desired_workers()
        adopt_legacy_ai.assert_called_once_with(11, 11001, "rules_ai", "default")
        terminate_legacy_ai.assert_called_once_with(11, 11001, "default")
        start_legacy_ai.assert_called_once_with(11, "rules_ai", "default")
        persist_legacy_ai.assert_called_once_with(11, "rules_ai", already_running=False)

        dead_ai_db = FakeDB(
            {"user_id": 10, "account_id": 1, "pid": 10001, "mode": "rules_ai"}
        )
        with (
            patch.object(app, "db", dead_ai_db),
            patch.object(app, "bot_adopt", return_value=(False, "pid_dead")),
            patch.object(app, "bot_terminate_pid") as terminate_dead,
            patch.object(app, "bot_status", return_value={"connected": True}),
            patch.object(app, "bot_start", return_value=(True, "started")),
            patch.object(app, "_persist_worker_started") as persisted_dead_ai,
        ):
            app.restore_desired_workers()
        terminate_dead.assert_not_called()
        persisted_dead_ai.assert_called_once_with(10, "rules_ai", already_running=False)

        class LookupFailureDB(FakeDB):
            def __init__(self):
                super().__init__({"user_id": 12, "account_id": 1, "pid": None, "mode": "rules"})
                self.rows = [
                    {
                        "user_id": 12,
                        "account_id": 1,
                        "pid": None,
                        "mode": "rules",
                        "desired_state": "running",
                        "generation": 0,
                    },
                    {
                        "user_id": 13,
                        "account_id": 1,
                        "pid": None,
                        "mode": "rules",
                        "desired_state": "running",
                        "generation": 0,
                    },
                ]

            def list_worker_runtimes(self, desired_state=None):
                assert desired_state is None
                return self.rows

            def get_worker_runtime(self, user_id, account_id=None):
                return next(
                    (
                        row
                        for row in self.rows
                        if row["user_id"] == user_id and row["account_id"] == account_id
                    ),
                    None,
                )

            def get_user_by_id(self, user_id):
                if user_id == 12:
                    raise RuntimeError("lookup failed")
                return {"id": user_id, "expires_at": time.time() + 3600}

        lookup_db = LookupFailureDB()
        with (
            patch.object(app, "db", lookup_db),
            patch.object(app, "bot_status", return_value={"connected": True}),
            patch.object(app, "bot_start", return_value=(True, "started")) as continued_start,
            patch.object(app, "_persist_worker_started") as continued_persist,
        ):
            app.restore_desired_workers()
        released_keys = [entry[1] for entry in lookup_db.started_leases if entry[0] == "released"]
        assert "worker-control:12:1" in released_keys
        assert "worker-control:13:1" in released_keys
        continued_start.assert_called_once_with(13, "rules", "default")
        continued_persist.assert_called_once_with(13, "rules", already_running=False)

    print("worker-recovery contract: safe adoption, token replacement, lookup isolation and PID reuse protection passed")


if __name__ == "__main__":
    main()
