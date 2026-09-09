#!/usr/bin/env python3
"""Offline limits/monitoring contracts using a temporary DB and explicit samplers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from db import DB
import bot_manager as bot
from runtime_settings import RuntimeSettings, RuntimeSettingsError, environment_defaults
from shop_resources import ProcReader, ShopResources

MIB = 1024 * 1024


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xianyu-resources-")
        self.root = Path(self.temp.name)
        self.db = DB(str(self.root / "control.db"))
        with self.db._lock, self.db.con:
            for uid, role in ((1, "admin"), (2, "owner")):
                self.db.con.execute("INSERT INTO users(id, username, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                                    (uid, f"owner-{uid}", "not-a-login-password", role, time.time()))
        self.first = self.db.ensure_default_shop_account(1)
        self.other = self.db.ensure_default_shop_account(2)
        self.settings = RuntimeSettings(self.db)

    def tearDown(self):
        self.db.con.close()
        self.temp.cleanup()

    def save(self, revision=0, **overrides):
        data = {"max_shop_accounts": 20, "max_running_workers": 3, "worker_memory_mib": 400, **overrides}
        return self.settings.save(1, expected_revision=revision, **data)


class SettingsContracts(Fixture):
    def test_environment_defaults_are_not_written_and_saved_values_win(self):
        with patch.dict(os.environ, {"SAAS_MAX_BOTS": "3", "SAAS_BOT_MEM_MB": "512"}):
            before = self.db.con.total_changes
            self.assertEqual(self.settings.read()["max_running_workers"], 3)
            self.assertEqual(self.settings.read()["worker_memory_mib"], 512)
            self.assertEqual(self.db.con.total_changes, before)
            saved = self.save(worker_memory_mib=768)
        with patch.dict(os.environ, {"SAAS_MAX_BOTS": "1", "SAAS_BOT_MEM_MB": "128"}):
            reopened = DB(str(self.root / "control.db"))
            try:
                self.assertEqual(reopened.get_runtime_settings(), saved)
            finally:
                reopened.con.close()
        self.assertEqual(saved["source"], "saved")
        self.assertEqual(saved["revision"], 1)

    def test_invalid_values_revisions_and_non_admin_do_not_change_policy(self):
        for field in ("max_shop_accounts", "max_running_workers", "worker_memory_mib"):
            for bad in (0, -1, True, "3", 1.5, None, 1000000):
                with self.subTest(field=field, bad=bad), self.assertRaises(RuntimeSettingsError):
                    self.save(**{field: bad})
        for revision in (True, -1, "0"):
            with self.assertRaises(RuntimeSettingsError):
                self.save(revision)
        with self.assertRaises(RuntimeSettingsError) as denied:
            self.settings.save(2, expected_revision=0, max_shop_accounts=5, max_running_workers=2, worker_memory_mib=256)
        self.assertEqual(denied.exception.code, "admin_required")
        self.assertEqual(self.settings.read()["revision"], 0)
        self.save()
        with self.assertRaises(RuntimeSettingsError) as conflict:
            self.save(0)
        self.assertEqual(conflict.exception.code, "resource_revision_conflict")

    def test_lower_count_preserves_existing_and_counts_disabled_shops(self):
        second = self.db.create_shop_account(2, "second")
        self.db.update_shop_account(2, second["id"], enabled=False)
        self.save(max_shop_accounts=1)
        self.assertEqual(self.db.resource_accounts_page(2)[2], 2)
        with self.assertRaises(RuntimeSettingsError) as full:
            self.db.create_shop_account(2, "third")
        self.assertEqual(full.exception.code, "shop_limit_reached")
        self.assertIsNotNone(self.db.get_shop_account(2, account_id=second["id"]))
        self.save(1, max_shop_accounts=3)
        self.assertIsNotNone(self.db.create_shop_account(2, "third"))

    def test_two_connections_cannot_create_beyond_limit(self):
        self.save(max_shop_accounts=2)
        conns = [DB(str(self.root / "control.db")) for _ in range(2)]
        gate, outcomes = threading.Barrier(2), []
        def create(index):
            gate.wait(timeout=5)
            try:
                conns[index].create_shop_account(2, f"shop-{index}")
                outcomes.append("created")
            except RuntimeSettingsError as error:
                outcomes.append(error.code)
        threads = [threading.Thread(target=create, args=(i,)) for i in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertCountEqual(outcomes, ["created", "shop_limit_reached"])
            self.assertEqual(self.db.resource_accounts_page(2)[2], 2)
        finally:
            for conn in conns:
                conn.con.close()

    def test_audit_and_policy_commit_atomically(self):
        self.db.con.execute("CREATE TRIGGER fail_resource_audit BEFORE INSERT ON audit_log BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(Exception):
            self.save()
        self.assertEqual(self.settings.read()["revision"], 0)
        self.db.con.execute("DROP TRIGGER fail_resource_audit")
        self.save()
        row = self.db.con.execute("SELECT * FROM audit_log WHERE event_type='platform.resource_settings_changed'").fetchone()
        self.assertEqual(json.loads(row["metadata_json"])["worker_memory_mib"], 400)
        self.assertNotIn("password", row["metadata_json"])

    def test_emergency_access_requires_explicit_server_only_authorization(self):
        values = {"max_shop_accounts": 20, "max_running_workers": 3, "worker_memory_mib": 400}
        with self.assertRaises(RuntimeSettingsError):
            self.db.save_runtime_settings(0, 0, values)
        with self.assertRaises(RuntimeSettingsError):
            self.db.save_runtime_settings(2, 0, values, emergency_admin=True)
        saved = self.db.save_runtime_settings(0, 0, values, emergency_admin=True)
        self.assertEqual(saved["revision"], 1)
        row = self.db.con.execute("SELECT actor_user_id FROM audit_log WHERE event_type='platform.resource_settings_changed'").fetchone()
        self.assertEqual(row[0], 0)

    def test_invalid_environment_fails_closed_not_unlimited(self):
        for value in ("", "0", "-1", "none", "999999"):
            with self.assertRaises(RuntimeSettingsError):
                environment_defaults({"SAAS_MAX_BOTS": value})


class FakeReader:
    def __init__(self):
        self.value = {"identity": "500", "cpu_seconds": 12.0, "rss_bytes": 32 * MIB,
                      "vms_bytes": 80 * MIB, "uptime_seconds": 10.0, "memory_limit_bytes": 400 * MIB}
        self.reads = 0
        self.after = None

    def identity(self, _pid):
        return self.value["identity"]

    def read(self, _pid):
        self.reads += 1
        if self.after:
            self.after()
        return dict(self.value)


class SamplingContracts(Fixture):
    def setUp(self):
        super().setUp()
        self.now = 100.0
        self.reader = FakeReader()
        self.process = {"pid": 1234, "generation": 1, "identity_token": 42, "mode": "rules", "state": "running"}
        self.valid = True
        self.lookups = []
        def lookup(uid, key):
            self.lookups.append((uid, key))
            return dict(self.process)
        self.monitor = ShopResources(self.db, self.settings, lookup, lambda *_: self.valid,
            reader=self.reader, clock=lambda: self.now, wall_clock=lambda: 1000 + self.now)

    def one(self):
        return self.monitor.page(1)["accounts"][0]

    def test_cpu_delta_first_sample_cache_and_memory_units(self):
        first = self.one()
        self.assertEqual(first["metrics_state"], "sampling")
        self.assertIsNone(first["cpu_percent"])
        self.assertEqual(first["rss_bytes"], 32 * MIB)
        self.now += 1
        self.one()
        self.assertEqual(self.reader.reads, 1)
        self.now += 4
        self.reader.value["cpu_seconds"] += 3
        second = self.one()
        self.assertEqual(second["cpu_percent"], 60)
        self.assertEqual(second["metrics_state"], "ready")
        self.assertEqual(second["memory_limit_bytes"], 400 * MIB)
        text = json.dumps(second)
        self.assertNotIn("identity_token", text)
        self.assertNotIn('"pid"', text)

    def test_changed_setting_does_not_fake_changed_live_process_limit(self):
        self.one()
        self.save(worker_memory_mib=768)
        changed = self.one()
        self.assertEqual(changed["memory_limit_bytes"], 400 * MIB)
        self.assertEqual(changed["configured_memory_limit_bytes"], 768 * MIB)
        self.assertIs(changed["pending_restart"], True)
        self.assertEqual(self.process["pid"], 1234)

    def test_pid_reuse_and_generation_change_drop_cached_metrics(self):
        self.one()
        self.reader.value["identity"] = "999"
        self.valid = False
        unavailable = self.one()
        self.assertEqual(unavailable["metrics_state"], "unavailable")
        self.assertEqual(unavailable["worker_state"], "unknown")
        self.assertIsNone(unavailable["rss_bytes"])
        self.valid = True
        self.process["generation"] = 2
        restarted = self.one()
        self.assertIsNone(restarted["cpu_percent"])
        self.assertEqual(restarted["metrics_state"], "sampling")

    def test_mid_read_account_change_returns_unavailable(self):
        def change():
            with self.db._lock, self.db.con:
                self.db.con.execute("UPDATE shop_accounts SET generation=generation+1 WHERE id=?", (self.first["id"],))
        self.reader.after = change
        self.assertEqual(self.one()["metrics_state"], "unavailable")

    def test_stopping_is_not_zero_and_unregistered_pid_is_not_sampled(self):
        self.process["state"] = "stopping"
        self.assertEqual(self.one()["rss_bytes"], 32 * MIB)
        self.process = {"pid": None, "state": "stopped", "generation": 2}
        with self.db._lock, self.db.con:
            self.db.con.execute("UPDATE worker_runtimes SET pid=9999 WHERE account_id=?", (self.first["id"],))
        self.assertEqual(self.one()["metrics_state"], "unavailable")
        with self.db._lock, self.db.con:
            self.db.con.execute("UPDATE worker_runtimes SET pid=NULL WHERE account_id=?", (self.first["id"],))
        self.assertEqual(self.one()["rss_bytes"], 0)

    def test_pagination_only_owned_shops_and_no_passive_creation(self):
        self.db.create_shop_account(1, "second")
        before = self.db.con.total_changes
        page = self.monitor.page(1, limit=1)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(self.monitor.page(1, cursor=page["next_cursor"], limit=1)["accounts"]), 1)
        self.assertTrue(all(uid == 1 for uid, _key in self.lookups))
        self.assertEqual(self.monitor.page(999)["total"], 0)
        self.assertEqual(self.db.con.total_changes, before)
        for bad in (-1, "0", True):
            with self.assertRaises(ValueError):
                self.monitor.page(1, cursor=bad)

    def test_proc_reader_parses_names_with_parentheses_and_real_page_units(self):
        root = self.root / "proc"
        (root / "1234").mkdir(parents=True)
        fields = ["0"] * 22
        fields[0], fields[11], fields[12], fields[19], fields[20], fields[21] = "S", "200", "50", "1000", str(80 * MIB), "256"
        (root / "1234/stat").write_text("1234 (worker (name)) " + " ".join(fields))
        (root / "uptime").write_text("100.0 10.0")
        (root / "1234/limits").write_text(f"Max address space {400 * MIB} {400 * MIB} bytes\n")
        with patch.object(os, "sysconf", create=True, side_effect=lambda key: 100 if key == "SC_CLK_TCK" else 4096):
            value = ProcReader(root).read(1234)
        self.assertEqual(value["cpu_seconds"], 2.5)
        self.assertEqual(value["rss_bytes"], MIB)
        self.assertEqual(value["uptime_seconds"], 90)
        self.assertEqual(value["memory_limit_bytes"], 400 * MIB)

    @unittest.skipUnless(os.name == "posix", "real managed process sampling needs the isolated Linux QA container")
    def test_real_registered_process_is_visible_only_to_its_shop(self):
        from account_storage import AccountStorage
        storage = AccountStorage(self.root / "tenants")
        account_root = storage.ensure_account_dir(1, "default")
        worker_root = self.root / "worker"
        worker_root.mkdir()
        script = worker_root / "sample_worker.py"
        script.write_text("import time; time.sleep(15)\n", encoding="utf-8")
        env = {**os.environ, "XIAN_YU_DATA_DIR": str(account_root),
               "PRODUCTS_CONFIG_FILE": str(account_root / "products_config.json"),
               "REPLY_RULES_FILE": str(account_root / "reply_rules.json")}
        process = subprocess.Popen([sys.executable, str(script)], cwd=str(worker_root), env=env, preexec_fn=bot._limit(400))
        names = ("_procs", "_tokens", "_log_files", "_modes", "_generations", "_desired_running")
        saved = {name: dict(getattr(bot, name)) for name in names}
        try:
            with patch.object(bot, "TENANTS_ROOT", str(storage.root)), patch.object(bot, "BOT_ROOT", worker_root), \
                    patch.object(bot, "BOT_MAIN", str(script)), patch.object(bot, "BOT_PYTHON", sys.executable):
                with bot._lock:
                    bot._register_process_locked((1, "default"), process, None, None, "rules")
                self.assertTrue(bot._expected_worker_pid(1, process.pid, "default"))
                self.assertFalse(bot._expected_worker_pid(2, process.pid, "default"))
                monitor = ShopResources(self.db, self.settings, bot.managed_resource_snapshot, bot._expected_worker_pid)
                owned = monitor.page(1)["accounts"][0]
                self.assertEqual(owned["metrics_state"], "sampling")
                self.assertGreater(owned["rss_bytes"], 0)
                self.assertEqual(owned["memory_limit_bytes"], 400 * MIB)
                with self.db._lock, self.db.con:
                    self.db.con.execute("UPDATE worker_runtimes SET pid=? WHERE account_id=?", (process.pid, self.other["id"]))
                self.assertIsNone(monitor.page(2)["accounts"][0]["rss_bytes"])
        finally:
            process.terminate()
            process.wait(timeout=5)
            for name, previous in saved.items():
                getattr(bot, name).clear()
                getattr(bot, name).update(previous)

    @unittest.skipUnless(os.name == "posix", "real proc sampling is tested in the isolated Linux QA container")
    def test_real_proc_read_and_captured_address_space_limit(self):
        value = ProcReader().read(os.getpid())
        self.assertGreater(value["rss_bytes"], 0)
        child = subprocess.run([sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])"],
            capture_output=True, text=True, preexec_fn=bot._limit(256), timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(int(child.stdout.strip()), 256 * MIB)


class ProcessContracts(Fixture):
    def setUp(self):
        super().setUp()
        self.saved = {name: dict(getattr(bot, name)) for name in ("_procs", "_tokens", "_log_files", "_modes", "_generations", "_desired_running", "_transitions", "_start_reservations")}
        self.provider = bot._resource_limits_provider
        for name in self.saved:
            getattr(bot, name).clear()
        self.started = []
        self.next_pid = 7000
        def spawn(uid, mode, key, limits):
            self.next_pid += 1
            proc = SimpleNamespace(pid=self.next_pid, returncode=None, _saas_resource_limits=dict(limits))
            proc.poll = lambda: proc.returncode
            self.started.append((uid, mode, key, dict(limits), proc))
            return proc, None, None
        self.spawn_patch = patch.object(bot, "_spawn_process", side_effect=spawn)
        self.spawn_patch.start()
        self.save(max_running_workers=1)
        def provider():
            self.assertFalse(bot._lock._is_owned(), "policy DB reads must occur outside process lock")
            return self.settings.read()
        bot.configure_resource_limits(provider)

    def tearDown(self):
        self.spawn_patch.stop()
        bot.configure_resource_limits(self.provider)
        for name, previous in self.saved.items():
            getattr(bot, name).clear()
            getattr(bot, name).update(previous)
        super().tearDown()

    def test_global_capacity_and_settings_only_affect_new_starts(self):
        self.assertEqual(bot.start(1), (True, "started"))
        self.assertEqual(bot.start(2), (False, "max_bots_reached"))
        self.save(1, max_running_workers=2, worker_memory_mib=768)
        self.assertEqual(bot.start(1), (True, "already_running"))
        self.assertEqual(bot.start(2), (True, "started"))
        self.assertEqual(self.started[0][3]["worker_memory_mib"], 400)
        self.assertEqual(self.started[1][3]["worker_memory_mib"], 768)
        self.save(2, max_running_workers=1)
        self.assertEqual(bot.running_count(), 2)
        self.assertEqual(bot.start(3), (False, "max_bots_reached"))
        self.assertEqual(bot.running_count(1), 1)

    def test_mode_switch_keeps_existing_slot_while_old_worker_stops(self):
        bot.start(1)
        attempted = []
        def terminate(proc):
            attempted.append(bot.start(2))
            proc.returncode = 0
            return True, "stopped"
        with patch.object(bot, "_terminate_process", side_effect=terminate), patch.object(bot, "_revoke_token_value"):
            self.assertEqual(bot.start(1, "rules_ai"), (True, "started"))
        self.assertEqual(attempted, [(False, "max_bots_reached")])
        self.assertEqual(bot.running_count(), 1)
        self.assertFalse(bot._start_reservations)

    def test_stop_during_policy_read_cancels_pending_start(self):
        def cancelled_policy():
            bot.stop(1)
            return self.settings.read()
        bot.configure_resource_limits(cancelled_policy)
        self.assertEqual(bot.start(1), (False, "transition_in_progress"))
        self.assertEqual(self.started, [])

    def test_explicit_stop_cancels_a_mode_switch_reservation(self):
        bot.start(1)
        def terminate(proc):
            # Equivalent to the first locked stage of a concurrent public stop.
            with bot._lock:
                bot._start_reservations.pop((1, "default"), None)
                bot._desired_running[(1, "default")] = False
            proc.returncode = 0
            return True, "stopped"
        with patch.object(bot, "_terminate_process", side_effect=terminate), patch.object(bot, "_revoke_token_value"):
            self.assertEqual(bot.start(1, "rules_ai"), (False, "start_cancelled"))
        self.assertEqual(len(self.started), 1)
        self.assertEqual(bot.running_count(), 0)

    def test_unavailable_policy_never_stops_existing_or_spawns_unbounded(self):
        bot.start(1)
        def broken():
            raise RuntimeError("do not expose storage internals")
        bot.configure_resource_limits(broken)
        with patch.object(bot, "stop") as stopped:
            with self.assertRaises(OSError):
                bot.start(1, "rules_ai")
            stopped.assert_not_called()
        with self.assertRaises(OSError):
            bot.start(2)
        self.assertEqual(len(self.started), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
