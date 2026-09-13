#!/usr/bin/env python3
"""Offline probe contracts: temporary SQLite, fake clocks, events, no business data."""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import queue
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from db import DB  # noqa: E402
from update_probe import (  # noqa: E402
    DEFAULT_INTERVAL_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    ProbeCoordinator,
    ProbeError,
    _version_key,
)


class Clock:
    def __init__(self, now=1700000000.0):
        self.now = float(now)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds
            return self.now

    def set(self, now):
        with self.lock:
            self.now = float(now)


def release_payload(current, target="2.0.0", **extra):
    return {
        "current_version": current,
        "channel": "release",
        "version": target,
        "available": bool(target),
        "status": "available" if target else "no_release",
        "release_notes": "Synthetic release notes",
        "published_at": "2023-11-14T00:00:00Z",
        "error_code": "",
        **extra,
    }


class SourceFailure(RuntimeError):
    def __init__(self, code="update_source_unavailable", retry_after=None):
        super().__init__("synthetic-private-error-not-for-public-output")
        self.code = code
        self.retry_after = retry_after


class Fetcher:
    def __init__(self, *responses):
        self.responses = responses or ("2.0.0",)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, version):
        with self.lock:
            index = len(self.calls)
            self.calls.append(version)
            response = self.responses[min(index, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(version)
        if isinstance(response, dict):
            return {**response}
        return release_payload(version, response)


class BlockedFetcher(Fetcher):
    def __init__(self, target="2.0.0", failure=None):
        self.entered = threading.Event()
        self.release = threading.Event()

        def block(version):
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("test did not release fake inspector")
            if failure is not None:
                raise failure
            return release_payload(version, target)

        super().__init__(block)


class StepWait:
    """Advance a selected background thread only when a test grants a step."""

    def __init__(self, name):
        self.name = name
        self.waiting = queue.Queue()
        self.steps = threading.Semaphore(0)

    def __call__(self, event, seconds):
        if threading.current_thread().name != self.name:
            return event.wait(seconds)
        self.waiting.put(seconds)
        while not event.is_set():
            if self.steps.acquire(timeout=0.01):
                return event.is_set()
        return True

    def ready(self):
        return self.waiting.get(timeout=5)

    def step(self):
        self.steps.release()


@contextlib.contextmanager
def no_network():
    with contextlib.ExitStack() as stack:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection"):
            stack.enter_context(patch(target, side_effect=AssertionError("network is forbidden")))
        yield


def process_check(path, barrier, entered, release, count, results, now):
    """Spawn-safe worker; its only data access is the supplied temporary DB."""
    database = coordinator = None
    try:
        with no_network():
            database = DB(path)

            def inspect(version):
                with count.get_lock():
                    count.value += 1
                entered.set()
                if not release.wait(15):
                    raise AssertionError("process fake inspector was not released")
                return release_payload(version)

            coordinator = ProbeCoordinator(database, inspect, "1.0.0", clock=lambda: now)
            barrier.wait(timeout=15)
            result = coordinator.check(force=True, raise_errors=True)
            results.put({"status": result["status"], "checking": result["update_probe"]["checking"]})
    except BaseException as exc:
        results.put({"worker_error": repr(exc)})
    finally:
        if coordinator is not None:
            coordinator.stop()
        if database is not None:
            database.con.close()


class ProbeContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xianyu-probe-contract-")
        self.path = Path(self.directory.name) / "probe.sqlite3"
        self.clock = Clock()
        self.databases = []
        self.coordinators = []
        self.threads = []
        self.releases = []
        self.db = self.new_db()

    def tearDown(self):
        for event in self.releases:
            event.set()
        for coordinator in self.coordinators:
            coordinator.stop()
        for thread in self.threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "a fake fetch thread did not stop")
        for coordinator in self.coordinators:
            coordinator.stop()
        for database in self.databases:
            database.con.close()
        self.directory.cleanup()

    def new_db(self):
        database = DB(str(self.path))
        self.databases.append(database)
        return database

    def coordinator(self, fetcher=None, *, database=None, current="1.0.0", **kwargs):
        fetcher = fetcher or Fetcher()
        if isinstance(fetcher, BlockedFetcher):
            self.releases.append(fetcher.release)
        coordinator = ProbeCoordinator(
            database or self.db, fetcher, current, clock=self.clock, **kwargs,
        )
        self.coordinators.append(coordinator)
        return coordinator

    def launch(self, coordinator, **kwargs):
        output = []
        done = threading.Event()

        def run():
            try:
                output.append(coordinator.check(**kwargs))
            except BaseException as exc:
                output.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run, name="contract-check", daemon=True)
        self.threads.append(thread)
        thread.start()
        return output, done

    def finished(self, output, done):
        self.assertTrue(done.wait(5), "fake check did not finish")
        self.assertEqual(len(output), 1)
        if isinstance(output[0], BaseException):
            raise output[0]
        return output[0]

    def legacy(self, payload):
        with patch("db.time.time", return_value=self.clock()):
            return self.db.save_platform_update_check("release", payload)

    def test_import_and_construction_have_no_threads_network_or_database(self):
        script = (
            "import sys,threading,socket,sqlite3; from unittest.mock import patch; "
            "sys.path.insert(0,sys.argv[1]); "
            "a=patch('threading.Thread.start',side_effect=AssertionError('thread')); a.start(); "
            "b=patch('sqlite3.connect',side_effect=AssertionError('database')); b.start(); "
            "c=patch('socket.socket.connect',side_effect=AssertionError('network')); c.start(); "
            "import update_probe; "
            "update_probe.ProbeCoordinator(object(),lambda version: {},'1.0.0'); "
            "assert not {'app','fastapi','platform_update','requests'} & set(sys.modules); "
            "print('portable import OK')"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(ROOT / "backend")],
            check=False, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("portable import OK", result.stdout)

    def test_snapshot_is_read_only_and_no_cache_is_distinct(self):
        fetcher = Fetcher()
        coordinator = self.coordinator(fetcher)
        statements = []
        self.db.con.set_trace_callback(statements.append)
        try:
            snapshot = coordinator.snapshot()
        finally:
            self.db.con.set_trace_callback(None)
        self.assertEqual(snapshot["status"], "unchecked")
        self.assertFalse(snapshot["available"])
        self.assertFalse(snapshot["update_probe"]["has_success"])
        self.assertIsNone(snapshot["update_probe"]["last_attempt_at"])
        self.assertFalse(self.db.get_platform_update_probe("release")["initialized"])
        self.assertFalse(fetcher.calls)
        self.assertTrue(all(item.lstrip().upper().startswith("SELECT") for item in statements))

    def test_start_without_cache_is_async_and_idempotent(self):
        fetcher = BlockedFetcher()
        coordinator = self.coordinator(fetcher)
        coordinator.start()
        self.assertTrue(fetcher.entered.wait(5))
        coordinator.start()
        snapshot = coordinator.snapshot()
        self.assertTrue(snapshot["update_probe"]["checking"])
        self.assertEqual(len(fetcher.calls), 1)
        fetcher.release.set()
        coordinator.stop()

    def test_background_six_hour_schedule_and_fresh_legacy_cache(self):
        expected = self.legacy(release_payload("1.0.0"))
        fetcher = Fetcher("2.1.0")
        waiter = StepWait("update-probe")
        coordinator = self.coordinator(fetcher, wait=waiter)
        coordinator.start()
        waiter.ready()
        self.assertFalse(fetcher.calls)
        self.assertEqual(coordinator.snapshot()["checked_at"], expected["checked_at"])
        self.assertEqual(coordinator.snapshot()["update_probe"]["next_check_at"], self.clock() + 21600)
        self.clock.advance(DEFAULT_INTERVAL_SECONDS - 1)
        waiter.step()
        waiter.ready()
        self.assertFalse(fetcher.calls)
        self.clock.advance(1)
        waiter.step()
        waiter.ready()
        self.assertEqual(fetcher.calls, ["1.0.0"])
        self.assertEqual(coordinator.snapshot()["version"], "2.1.0")
        self.assertEqual(coordinator.snapshot()["update_probe"]["next_check_at"], self.clock() + 21600)
        coordinator.stop()
        coordinator.stop()

    def test_expired_legacy_cache_is_checked_on_start(self):
        self.legacy(release_payload("1.0.0", "1.1.0"))
        self.clock.advance(21601)
        fetcher = Fetcher("2.0.0")
        waiter = StepWait("update-probe")
        coordinator = self.coordinator(fetcher, wait=waiter)
        coordinator.start()
        waiter.ready()
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(coordinator.snapshot()["version"], "2.0.0")

    def test_legacy_error_or_corrupt_cache_is_not_a_success(self):
        malformed = (
            "{invalid", "[]", '{"status":"error","checked_at":10}', '{}',
            json.dumps({**release_payload("1.0.0", "bad-version"), "checked_at": self.clock()}),
            json.dumps({**release_payload("bad-current", "2.0.0"), "checked_at": self.clock()}),
            json.dumps({**release_payload("1.0.0", "2.0.0", status="no_release"), "checked_at": self.clock()}),
            json.dumps({**release_payload("1.0.0", "2.0.0", status=[]), "checked_at": self.clock()}),
            json.dumps({**release_payload("1.0.0", "2.0.0"), "checked_at": "NaN"}),
        )
        for raw in malformed:
            with self.subTest(raw=raw):
                self.db.con.execute("DELETE FROM platform_update_probes")
                self.db.con.execute(
                    "INSERT OR REPLACE INTO platform_update_checks VALUES ('release', ?, ?)",
                    (raw, self.clock()),
                )
                self.db.con.commit()
                fetcher = Fetcher()
                coordinator = self.coordinator(fetcher)
                self.assertFalse(coordinator.snapshot()["update_probe"]["has_success"])
                self.assertTrue(coordinator.check()["available"])
                self.assertEqual(len(fetcher.calls), 1)

    def test_force_cooldown_shared_and_persisted_including_epoch_zero(self):
        self.clock.set(0)
        first_fetcher = Fetcher("2.0.0")
        coordinator = self.coordinator(first_fetcher)
        first = coordinator.check(force=True)
        self.assertEqual(first["update_probe"]["last_attempt_at"], 0)
        self.assertEqual(first["update_probe"]["manual_cooldown_until"], 60)
        second_fetcher = Fetcher("3.0.0")
        other = self.coordinator(second_fetcher, database=self.new_db())
        for _ in range(20):
            self.assertEqual(other.check(force=True)["version"], "2.0.0")
        self.clock.advance(59.999)
        self.assertEqual(other.check(force=True)["version"], "2.0.0")
        self.assertFalse(second_fetcher.calls)
        self.clock.set(60)
        self.assertEqual(other.check(force=True)["version"], "3.0.0")
        self.assertEqual(second_fetcher.calls, ["1.0.0"])
        self.assertEqual(other.snapshot()["update_probe"]["next_check_at"], 21660)

    def test_force_reuses_existing_legacy_recent_attempt(self):
        self.legacy(release_payload("1.0.0"))
        fetcher = Fetcher()
        coordinator = self.coordinator(fetcher)
        coordinator.check(force=True)
        self.assertFalse(fetcher.calls)
        self.clock.advance(60)
        coordinator.check(force=True)
        self.assertEqual(len(fetcher.calls), 1)

    def test_two_connections_and_many_callers_share_one_inflight_probe(self):
        fetcher = BlockedFetcher()
        leader = self.coordinator(fetcher)
        competitor_fetcher = Fetcher("3.0.0")
        other = self.coordinator(competitor_fetcher, database=self.new_db())
        output, done = self.launch(leader, force=True)
        self.assertTrue(fetcher.entered.wait(5))
        followers = [self.launch(other if index % 2 else leader, force=True) for index in range(12)]
        for follower_output, follower_done in followers:
            self.assertTrue(self.finished(follower_output, follower_done)["update_probe"]["checking"])
        self.assertEqual(len(fetcher.calls), 1)
        self.assertFalse(competitor_fetcher.calls)
        fetcher.release.set()
        result = self.finished(output, done)
        self.assertTrue(result["available"])
        self.assertEqual(other.snapshot()["checked_at"], result["checked_at"])

    def test_actual_spawned_processes_share_one_sqlite_flight(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(5)
        entered, release = ctx.Event(), ctx.Event()
        count = ctx.Value("i", 0)
        results = ctx.Queue()
        processes = [ctx.Process(
            target=process_check,
            args=(str(self.path), barrier, entered, release, count, results, self.clock()),
        ) for _ in range(4)]
        try:
            for process in processes:
                process.start()
            barrier.wait(timeout=15)
            self.assertTrue(entered.wait(5))
            merged = [results.get(timeout=10) for _ in range(3)]
            self.assertTrue(all(item == {"status": "unchecked", "checking": True} for item in merged), merged)
            self.assertEqual(count.value, 1)
            release.set()
            self.assertEqual(results.get(timeout=10), {"status": "available", "checking": False})
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(count.value, 1)
        finally:
            release.set()
            for process in processes:
                if process.pid is not None:
                    process.join(timeout=5)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
            results.close()
            results.join_thread()

    def test_failures_retain_success_and_recover_without_leaking_exception(self):
        failure = SourceFailure()
        fetcher = Fetcher("2.0.0", failure, "3.0.0")
        coordinator = self.coordinator(fetcher)
        first = coordinator.check()
        self.clock.advance(60)
        with self.assertRaises(SourceFailure) as raised:
            coordinator.check(force=True, raise_errors=True)
        self.assertIs(raised.exception, failure)
        failed = coordinator.snapshot()
        self.assertTrue(failed["available"])
        self.assertEqual(failed["status"], "available")
        self.assertEqual(failed["version"], "2.0.0")
        self.assertEqual(failed["checked_at"], first["checked_at"])
        self.assertEqual(failed["error_code"], "")
        self.assertEqual(failed["update_probe"]["error_code"], failure.code)
        self.assertEqual(failed["update_probe"]["state"], "error")
        self.assertEqual(failed["update_probe"]["consecutive_failures"], 1)
        self.assertNotIn(str(failure), json.dumps(failed))
        self.assertEqual(self.db.get_platform_update_check("release")["version"], "2.0.0")
        self.assertEqual(coordinator.check(force=True, raise_errors=True)["version"], "2.0.0")
        self.clock.advance(60)
        recovered = coordinator.check()
        self.assertEqual(recovered["version"], "3.0.0")
        self.assertEqual(recovered["update_probe"]["consecutive_failures"], 0)
        self.assertEqual(recovered["update_probe"]["error_code"], "")
        self.assertEqual(recovered["update_probe"]["last_failure_at"], failed["update_probe"]["last_failure_at"])
        self.assertEqual(recovered["update_probe"]["last_success_at"], self.clock())

    def test_failure_without_prior_success_is_not_no_release(self):
        coordinator = self.coordinator(Fetcher(SourceFailure()))
        result = coordinator.check()
        self.assertEqual(result["status"], "unchecked")
        self.assertFalse(result["update_probe"]["has_success"])
        self.assertEqual(result["update_probe"]["state"], "error")
        self.assertIsNone(result["update_probe"]["last_success_at"])
        self.assertEqual(result["update_probe"]["last_failure_at"], self.clock())
        self.assertEqual(self.db.get_platform_update_check("release")["status"], "unchecked")

    def test_no_release_and_incomplete_are_successful_discoveries(self):
        fetcher = Fetcher("", release_payload("1.0.0", status="incomplete", error_code="release_assets_missing"))
        coordinator = self.coordinator(fetcher)
        empty = coordinator.check()
        self.assertEqual(empty["status"], "no_release")
        self.assertTrue(empty["update_probe"]["has_success"])
        self.clock.advance(60)
        incomplete = coordinator.check(force=True)
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertTrue(incomplete["available"])
        self.assertEqual(incomplete["error_code"], "release_assets_missing")
        self.assertEqual(incomplete["update_probe"]["error_code"], "")
        self.assertEqual(incomplete["update_probe"]["consecutive_failures"], 0)

    def test_backoff_is_exponential_bounded_and_restart_safe(self):
        fetcher = Fetcher(SourceFailure())
        coordinator = self.coordinator(fetcher)
        for number in range(1, 13):
            result = coordinator.check(force=True)
            probe = result["update_probe"]
            self.assertEqual(probe["consecutive_failures"], number)
            self.assertEqual(probe["next_check_at"] - self.clock(), min(60 * 2 ** (number - 1), MAX_BACKOFF_SECONDS))
            restarted = self.coordinator(Fetcher(), database=self.new_db())
            self.assertEqual(restarted.snapshot()["update_probe"], probe)
            self.clock.set(probe["next_check_at"] - 0.1)
            restarted.check(force=True)
            self.assertFalse(restarted.inspect_release.calls)
            self.assertEqual(len(fetcher.calls), number)
            self.clock.advance(0.1)

    def test_retry_after_delta_http_date_and_extremes_are_bounded(self):
        now = self.clock()
        values = [
            ("900", 900),
            (format_datetime(datetime.fromtimestamp(now + 1800, timezone.utc), usegmt=True), 1800),
            (10 ** 100, MAX_RETRY_AFTER_SECONDS),
            ("NaN", 60), (float("inf"), 60), (-20, 60), ("bad date", 60), (True, 60),
        ]
        for value, expected in values:
            with self.subTest(retry_after=value):
                self.db.con.execute("DELETE FROM platform_update_probes")
                self.db.con.commit()
                fetcher = Fetcher(SourceFailure("update_source_rate_limited", value))
                coordinator = self.coordinator(fetcher)
                result = coordinator.check(force=True)
                self.assertEqual(result["update_probe"]["next_check_at"], now + expected)
                self.assertEqual(result["update_probe"]["retry_not_before"], now + expected)
                coordinator.check(force=True)
                self.assertEqual(len(fetcher.calls), 1)

    def test_error_payload_retry_after_is_not_persisted_as_success(self):
        coordinator = self.coordinator(Fetcher({
            "status": "error", "error_code": "update_source_rate_limited", "retry_after": 300,
        }))
        result = coordinator.check()
        self.assertFalse(result["update_probe"]["has_success"])
        self.assertEqual(result["update_probe"]["next_check_at"], self.clock() + 300)

    def test_installed_version_changes_reproject_and_refresh_shared_cache(self):
        version = ["1.0.0"]
        fetcher = Fetcher("2.0.0", "2.1.0")
        coordinator = self.coordinator(fetcher, current=lambda: version[0])
        self.assertTrue(coordinator.check()["available"])
        version[0] = "2.0.0"
        updated = coordinator.snapshot()
        self.assertFalse(updated["available"])
        self.assertEqual(updated["status"], "current")
        self.assertTrue(updated["update_probe"]["cache_stale"])
        self.assertEqual(len(fetcher.calls), 1)
        self.clock.advance(60)
        self.assertEqual(coordinator.check()["version"], "2.1.0")
        self.assertEqual(fetcher.calls, ["1.0.0", "2.0.0"])
        version[0] = "3.0.0"
        self.assertFalse(coordinator.snapshot()["available"])
        version[0] = "1.0.0"
        self.assertTrue(coordinator.snapshot()["available"])

    def test_new_process_version_invalidates_unexpired_cache(self):
        self.coordinator().check()
        self.clock.advance(60)
        fetcher = Fetcher("2.1.0")
        coordinator = self.coordinator(fetcher, database=self.new_db(), current="2.0.0")
        self.assertFalse(coordinator.snapshot()["available"])
        self.assertEqual(coordinator.check()["version"], "2.1.0")
        self.assertEqual(fetcher.calls, ["2.0.0"])

    def test_version_change_midflight_discards_old_result_without_error(self):
        version = ["1.0.0"]
        fetcher = BlockedFetcher()
        coordinator = self.coordinator(fetcher, current=lambda: version[0])
        output, done = self.launch(coordinator)
        self.assertTrue(fetcher.entered.wait(5))
        version[0] = "2.0.0"
        fetcher.release.set()
        result = self.finished(output, done)
        self.assertFalse(result["available"])
        self.assertFalse(result["update_probe"]["has_success"])
        self.assertFalse(result["update_probe"]["checking"])
        self.assertEqual(result["update_probe"]["consecutive_failures"], 0)
        self.assertEqual(self.db.get_platform_update_check("release")["status"], "unchecked")

    def test_semver_precedence_and_build_metadata_match_existing_rules(self):
        ordered = [
            "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
            "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0",
        ]
        self.assertEqual(sorted(reversed(ordered), key=_version_key), ordered)
        self.assertEqual(_version_key("1.0.0+build.1"), _version_key("1.0.0+build.2"))
        for version in ("1.02.0", "1.0.0-01", "v1.0.0", "not-a-version"):
            with self.assertRaises(ProbeError):
                _version_key(version)
        coordinator = self.coordinator(Fetcher("1.0.0+build.2"), current="1.0.0+build.1")
        self.assertFalse(coordinator.check()["available"])

    def test_renewal_keeps_a_long_check_single_flight(self):
        fetcher = BlockedFetcher()
        waiter = StepWait("update-probe-renew")
        coordinator = self.coordinator(fetcher, wait=waiter)
        other_fetcher = Fetcher("3.0.0")
        other = self.coordinator(other_fetcher, database=self.new_db())
        output, done = self.launch(coordinator, force=True)
        self.assertTrue(fetcher.entered.wait(5))
        self.assertEqual(waiter.ready(), 40)
        original_until = self.db.get_platform_update_probe("release")["lease_until"]
        self.clock.advance(80)
        waiter.step()
        waiter.ready()
        renewed = self.db.get_platform_update_probe("release")
        self.assertEqual(renewed["lease_until"], original_until + 80)
        self.clock.advance(41)
        self.assertTrue(other.check(force=True)["update_probe"]["checking"])
        self.assertFalse(other_fetcher.calls)
        fetcher.release.set()
        self.assertTrue(self.finished(output, done)["available"])

    def test_expired_lease_cannot_renew_or_write_even_without_new_owner(self):
        claim = self.db.acquire_platform_update_probe("release", "old", "1.0.0", now=self.clock())
        self.clock.advance(120)
        self.assertFalse(self.db.renew_platform_update_probe(
            "release", "old", claim["generation"], now=self.clock(),
        ))
        self.assertFalse(self.db.finish_platform_update_probe(
            "release", "old", claim["generation"], current_version="1.0.0",
            payload=release_payload("1.0.0"), next_check_at=self.clock() + 21600, now=self.clock(),
        ))
        self.assertEqual(self.db.get_platform_update_check("release")["status"], "unchecked")

    def test_generation_fence_rejects_stale_success_failure_and_abandon(self):
        old = self.db.acquire_platform_update_probe("release", "same-owner", "1.0.0", now=self.clock())
        self.clock.advance(121)
        new = self.db.acquire_platform_update_probe("release", "same-owner", "1.0.0", now=self.clock())
        self.assertEqual(new["generation"], old["generation"] + 1)
        self.assertFalse(self.db.renew_platform_update_probe(
            "release", "same-owner", old["generation"], now=self.clock(),
        ))
        self.assertFalse(self.db.abandon_platform_update_probe(
            "release", "same-owner", old["generation"], now=self.clock(),
        ))
        self.assertTrue(self.db.finish_platform_update_probe(
            "release", "same-owner", new["generation"], current_version="1.0.0",
            payload=release_payload("1.0.0", "3.0.0"), next_check_at=self.clock() + 21600, now=self.clock(),
        ))
        for payload in (release_payload("1.0.0", "2.0.0"), None):
            self.assertFalse(self.db.finish_platform_update_probe(
                "release", "same-owner", old["generation"], current_version="1.0.0",
                payload=payload, error_code="update_source_unavailable",
                next_check_at=self.clock() + 60, retry_not_before=self.clock() + 60, now=self.clock(),
            ))
        state = self.db.get_platform_update_probe("release")
        self.assertEqual(state["result"]["version"], "3.0.0")
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(self.db.get_platform_update_check("release")["version"], "3.0.0")

    def test_late_success_and_failure_after_takeover_return_new_shared_result(self):
        for failure in (None, SourceFailure()):
            with self.subTest(failure=bool(failure)):
                self.db.con.execute("DELETE FROM platform_update_probes")
                self.db.con.execute("DELETE FROM platform_update_checks")
                self.db.con.commit()
                fetcher = BlockedFetcher(failure=failure)
                waiter = StepWait("update-probe-renew")
                old = self.coordinator(fetcher, wait=waiter)
                output, done = self.launch(old, force=True, raise_errors=True)
                self.assertTrue(fetcher.entered.wait(5))
                waiter.ready()
                self.clock.advance(121)
                newer = self.coordinator(Fetcher("3.0.0"), database=self.new_db())
                self.assertEqual(newer.check(force=True)["version"], "3.0.0")
                fetcher.release.set()
                late = self.finished(output, done)
                self.assertEqual(late["version"], "3.0.0")
                self.assertEqual(late["update_probe"]["consecutive_failures"], 0)
                self.assertEqual(self.db.get_platform_update_check("release")["version"], "3.0.0")

    def test_renewal_database_error_cancels_even_a_still_live_lease(self):
        fetcher = BlockedFetcher()
        waiter = StepWait("update-probe-renew")
        coordinator = self.coordinator(fetcher, wait=waiter)
        output, done = self.launch(coordinator)
        self.assertTrue(fetcher.entered.wait(5))
        waiter.ready()
        failed_renewal = threading.Event()

        def fail(*args, **kwargs):
            failed_renewal.set()
            raise sqlite3.OperationalError("synthetic renewal error")

        with patch.object(self.db, "renew_platform_update_probe", side_effect=fail):
            waiter.step()
            self.assertTrue(failed_renewal.wait(5))
            # Wait for the actual renewal thread, not just the fake method's entry.
            coordinator._active.renew_thread.join(timeout=5)
            self.assertFalse(coordinator._active.renew_thread.is_alive())
        fetcher.release.set()
        result = self.finished(output, done)
        self.assertFalse(result["update_probe"]["has_success"])
        self.assertEqual(result["update_probe"]["consecutive_failures"], 0)
        self.assertEqual(self.db.get_platform_update_check("release")["status"], "unchecked")

    def test_pause_before_and_during_a_check_never_becomes_a_source_failure(self):
        paused = [True]
        fetcher = BlockedFetcher()
        coordinator = self.coordinator(fetcher, is_paused=lambda: paused[0])
        self.assertEqual(coordinator.check(force=True)["update_probe"]["state"], "paused")
        self.assertFalse(fetcher.calls)
        self.assertFalse(self.db.get_platform_update_probe("release")["initialized"])
        paused[0] = False
        output, done = self.launch(coordinator)
        self.assertTrue(fetcher.entered.wait(5))
        paused[0] = True
        fetcher.release.set()
        result = self.finished(output, done)
        self.assertFalse(result["update_probe"]["has_success"])
        self.assertEqual(result["update_probe"]["state"], "paused")
        self.assertEqual(result["update_probe"]["consecutive_failures"], 0)
        paused[0] = False
        self.clock.advance(60)
        self.assertTrue(coordinator.check()["available"])

    def test_unreadable_pause_condition_fails_closed(self):
        fetcher = Fetcher()

        def unreadable():
            raise OSError("synthetic missing maintenance source")

        coordinator = self.coordinator(fetcher, is_paused=unreadable)
        self.assertTrue(coordinator.check(force=True)["update_probe"]["paused"])
        self.assertFalse(fetcher.calls)

    def test_stop_fences_late_result_and_restart_is_explicit(self):
        fetcher = BlockedFetcher()
        coordinator = self.coordinator(fetcher)
        output, done = self.launch(coordinator)
        self.assertTrue(fetcher.entered.wait(5))
        coordinator.stop()
        self.assertFalse(coordinator.snapshot()["update_probe"]["checking"])
        fetcher.release.set()
        self.assertFalse(self.finished(output, done)["update_probe"]["has_success"])
        coordinator.check(force=True)
        self.assertEqual(len(fetcher.calls), 1)
        self.clock.advance(60)
        waiter = StepWait("update-probe")
        coordinator._wait = waiter
        coordinator.start()
        waiter.ready()
        self.assertEqual(len(fetcher.calls), 2)
        self.assertTrue(coordinator.snapshot()["available"])
        coordinator.stop()

    def test_installer_asset_flags_are_safe_optional_and_legacy_compatible(self):
        payload = release_payload("1.0.0", installer_assets={
            "docker": True, "systemd": "unsafe-truthy-string", "url": "not-retained",
        })
        legacy = self.legacy(payload)
        self.assertEqual(legacy["installer_assets"], {"docker": True, "systemd": False})
        self.clock.advance(60)
        coordinator = self.coordinator(Fetcher(payload))
        result = coordinator.check(force=True)
        self.assertEqual(result["installer_assets"], {"docker": True, "systemd": False})
        self.assertEqual(self.db.get_platform_update_check("release")["installer_assets"], result["installer_assets"])
        self.assertNotIn("not-retained", json.dumps(result))
        self.legacy({"status": "error", "available": False, "error_code": "old_caller_failure"})
        self.assertTrue(coordinator.snapshot()["available"])
        self.assertEqual(coordinator.snapshot()["version"], "2.0.0")

    def test_success_cache_and_probe_commit_are_atomic(self):
        coordinator = self.coordinator()
        self.db.con.execute(
            """CREATE TEMP TRIGGER reject_cache BEFORE INSERT ON platform_update_checks
               BEGIN SELECT RAISE(ABORT, 'synthetic write rejection'); END"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            coordinator.check()
        state = self.db.get_platform_update_probe("release")
        self.assertIsNone(state["last_success_at"])
        self.assertEqual(state["result"]["status"], "unchecked")
        self.assertEqual(self.db.get_platform_update_check("release")["status"], "unchecked")
        self.assertFalse(state["lease_owner"])

    def test_channel_cache_and_probe_lease_do_not_use_installation_lock(self):
        self.assertEqual(self.db.acquire_control_lease("platform-update", "installer", now=self.clock()), "acquired")
        release = self.coordinator()
        stable_fetcher = Fetcher("3.0.0")
        stable = self.coordinator(stable_fetcher, channel="stable")
        self.assertTrue(release.check()["available"])
        self.assertEqual(stable.check()["version"], "3.0.0")
        self.assertEqual(self.db.get_control_lease("platform-update")["owner"], "installer")
        self.assertEqual(self.db.get_platform_update_check("release")["version"], "2.0.0")
        self.assertEqual(self.db.get_platform_update_check("stable")["version"], "3.0.0")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    with no_network():
        unittest.main(verbosity=2)
