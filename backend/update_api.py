"""Persistent update orchestration; no network or worker starts at import time."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time

import platform_update as protocol
from update_maintenance import maintenance_active


TERMINAL_STATUSES = frozenset({"succeeded", "rolled_back", "failed", "recovery_failed"})
PUBLIC_OPERATION_FIELDS = (
    "operation_id", "action", "version", "channel", "deployment", "manifest_sha256",
    "status", "phase", "current_version", "error_code", "created_at", "updated_at", "release_notes",
)


class UpdateAPI:
    def __init__(self, db, current_version, *, channel="release", backend=protocol,
                 is_paused=maintenance_active, clock=time.time):
        self.db = db
        self._version = current_version
        self.channel = channel
        self.backend = backend
        self.is_paused = is_paused
        self.clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._lifecycle_lock = threading.Lock()
        self._publish_lock = threading.Lock()

    def current_version(self):
        return str(self._version() if callable(self._version) else self._version)

    @staticmethod
    def public_operation(row):
        if row is None:
            return None
        row = dict(row)
        return {key: row[key] for key in PUBLIC_OPERATION_FIELDS if key in row}

    def _session(self, user_id, session):
        if int(user_id) <= 0 or not session or self.db.get_token_user(session) != int(user_id):
            raise protocol.PlatformUpdateError("confirmation_invalid")
        return hashlib.sha256(session.encode()).hexdigest()

    def _record(self, operation_id, version, action, user_id, session):
        digest = self._session(user_id, session)
        if not protocol.OPERATION_ID_RE.fullmatch(str(operation_id)):
            raise protocol.PlatformUpdateError("update_operation_invalid")
        row = self.db.get_update_operation(operation_id)
        if row is None:
            raise protocol.PlatformUpdateError("update_not_staged")
        row = dict(row)
        if (row["version"] != version or row["action"] != action
                or row["requested_by"] != int(user_id) or row["session_digest"] != digest):
            raise protocol.PlatformUpdateError("confirmation_invalid")
        return row

    def capabilities(self, check=None):
        result = self.backend.update_capabilities()
        mode = result.get("deployment")
        assets = (check or {}).get("installer_assets")
        if isinstance(assets, dict) and assets.get(mode) is False and result.get("download"):
            result = {**result, "download": False, "reason": "release_assets_missing"}
            staged = self.db.latest_update_operation(status="staged")
            if staged is None or staged["action"] != "apply":
                result["apply"] = False
        return result

    def _require_available(self, action, *, operation_id=""):
        if self.is_paused():
            raise protocol.PlatformUpdateError("update_maintenance_active")
        result = self.backend.update_capabilities()
        capability = "download" if action == "download" else action
        if result.get(capability) is not True:
            raise protocol.PlatformUpdateError(result.get("reason") or "update_installation_unsupported")
        self.reconcile()
        active = self.db.active_update_operation()
        if active is not None and active["operation_id"] != operation_id:
            raise protocol.PlatformUpdateError("update_busy")
        # Keep deployments with an old v1 request fail-closed until it completes.
        legacy = self.db.active_platform_update()
        if active is None and legacy is not None:
            # Another submit may have committed the operation and its legacy
            # mirror between the two reads. A retry of that same operation is
            # still allowed; an actual legacy request remains fail-closed.
            active = self.db.active_update_operation()
            if active is None or active["operation_id"] != operation_id:
                raise protocol.PlatformUpdateError("update_busy")
        return result

    def _validate(self, row):
        current = self.current_version()
        if row["expected_current_version"] != current:
            raise protocol.PlatformUpdateError("update_current_version_mismatch")
        if row["action"] == "apply":
            if protocol.SemVer.parse(row["version"]).compare(protocol.SemVer.parse(current)) <= 0:
                raise protocol.PlatformUpdateError("update_downgrade_rejected")
            if row["deployment"] == "docker":
                self.backend.validate_docker_candidate(row["operation_id"], row["version"], row["manifest_sha256"])
            else:
                self.backend.validate_candidate(row["candidate_path"], row["version"], row["manifest_sha256"], require_maintenance=True)
        else:
            candidates = self.backend.available_rollback_versions(current)
            if {"version": row["version"], "manifest_sha256": row["manifest_sha256"]} not in candidates:
                raise protocol.PlatformUpdateError("rollback_version_unavailable")

    def _reusable_stage(self, version, action, mode, current, session_digest):
        existing = self.db.staged_update_operation(version, action, self.channel, mode, current, session_digest)
        if existing is not None:
            try:
                self._validate(dict(existing))
            except protocol.PlatformUpdateError:
                return None  # Lost/damaged candidates can be safely downloaded again.
            return self.public_operation(existing)
        return None

    def prepare(self, version, action, user_id, session, *, ensure_owned=lambda: None):
        session_digest = self._session(user_id, session)
        if action not in {"apply", "rollback"}:
            raise protocol.PlatformUpdateError("update_operation_invalid")
        protocol.SemVer.parse(version)
        capability = self._require_available("download" if action == "apply" else "rollback")
        current = self.current_version()
        mode = capability["deployment"]
        if mode not in {"docker", "systemd"}:
            raise protocol.PlatformUpdateError("update_installation_unsupported")
        operation_id = secrets.token_hex(16)
        if action == "rollback":
            target = next((x for x in self.backend.available_rollback_versions(current)
                           if x["version"] == version), None)
            if target is None:
                raise protocol.PlatformUpdateError("rollback_version_unavailable")
            ensure_owned()
            reusable = self._reusable_stage(version, action, mode, current, session_digest)
            if reusable is not None:
                return reusable
            staged = {**target, "candidate_path": "", "release_id": "", "release_notes": ""}
        else:
            release = self.backend.fetch_release(self.channel, current, deployment=mode)
            if release is None:
                raise protocol.PlatformUpdateError("update_not_available")
            if release.version != version:
                raise protocol.PlatformUpdateError("update_version_changed")
            ensure_owned()
            reusable = self._reusable_stage(version, action, mode, current, session_digest)
            if reusable is not None:
                return reusable
            if mode == "docker":
                staged = self.backend.stage_docker_release(release, self.channel, current, operation_id)
            else:
                staged = self.backend.stage_release(release, self.channel, current, require_maintenance=True,
                                                    operation_id=operation_id)
        ensure_owned()
        if self.is_paused():
            raise protocol.PlatformUpdateError("update_maintenance_active")
        row = self.db.create_update_operation(
            operation_id=operation_id, action=action, version=version, channel=self.channel,
            deployment=mode, manifest_sha256=staged["manifest_sha256"],
            candidate_path=staged.get("candidate_path", ""), release_id=staged.get("release_id", ""),
            release_notes=staged.get("release_notes", ""), expected_current_version=current,
            requested_by=int(user_id), session_digest=session_digest,
        )
        return self.public_operation(row)

    def confirm(self, operation_id, version, action, user_id, session):
        row = self._record(operation_id, version, action, user_id, session)
        self._require_available(action, operation_id=operation_id)
        if row["status"] != "staged":
            raise protocol.PlatformUpdateError("update_not_staged")
        self._validate(row)
        try:
            token = self.db.create_admin_confirmation(
                user_id, "update." + action, ttl_seconds=180, session_token=session,
                version=version, manifest_sha256=row["manifest_sha256"], operation_id=operation_id,
            )
        except ValueError as error:
            raise protocol.PlatformUpdateError("confirmation_invalid") from error
        return {"confirmation_token": token, "expires_in": 180, "operation_id": operation_id,
                "version": version, "manifest_sha256": row["manifest_sha256"]}

    def submit(self, operation_id, version, action, token, user_id, session, *, ensure_owned=lambda: None):
        row = self._record(operation_id, version, action, user_id, session)
        self.reconcile(operation_id)
        row = dict(self.db.get_update_operation(operation_id))
        if row["status"] == "staged":
            self._require_available(action, operation_id=operation_id)
            self._validate(row)
        ensure_owned()
        # Consuming confirmation and committing the recoverable outbox entry
        # happen in the same transaction. A retry must present the same token.
        row = self.db.queue_update_operation(operation_id, token, user_id, session,
                                             action=action, version=version)
        if row is None:
            raise protocol.PlatformUpdateError("confirmation_invalid")
        row = dict(row)
        if row["status"] in TERMINAL_STATUSES:
            return {"queued": False, **self.public_operation(row)}
        ensure_owned()
        self._publish(row)
        return {"queued": True, "status": "queued", "operation_id": operation_id,
                "action": action, "version": version}

    def reconcile(self, operation_id=None):
        rows = ([self.db.get_update_operation(operation_id)] if operation_id
                else self.db.list_update_operations(pending_only=True))
        for row in rows:
            if row is None or row["status"] == "staged":
                continue
            try:
                status = self.backend.read_operation_status(dict(row))
                if status is not None:
                    self.db.observe_update_operation(row["operation_id"], status)
            except protocol.PlatformUpdateError as error:
                self.db.note_update_operation_error(row["operation_id"], error.code)

    def latest_operation(self):
        self.reconcile()
        return self.public_operation(self.db.active_update_operation() or self.db.latest_update_operation())

    def _publish(self, row):
        if row.get("requested_at") is None or not row.get("confirmation_digest"):
            raise protocol.PlatformUpdateError("confirmation_invalid")
        with self._publish_lock:
            # A root-owned status proves the executor accepted this exact id;
            # do not recreate consumed requests after a process/container restart.
            try:
                status = self.backend.read_operation_status(dict(row))
                if status is not None:
                    self.db.observe_update_operation(row["operation_id"], status)
            except protocol.PlatformUpdateError as error:
                self.db.note_update_operation_error(row["operation_id"], error.code)
                raise
            row = dict(self.db.get_update_operation(row["operation_id"]))
            if row["published_at"] or row["executor_updated_at"] or row["status"] in TERMINAL_STATUSES:
                return
            try:
                caps = self.backend.update_capabilities()
                if caps.get(row["action"]) is not True or caps.get("deployment") != row["deployment"]:
                    raise protocol.PlatformUpdateError(caps.get("reason") or "update_installation_unavailable")
                self._validate(row)
                if row["deployment"] == "docker":
                    self.backend.write_docker_update_request(row)
                else:
                    self.backend.write_update_intent(
                        row["action"], row["version"], channel=row["channel"],
                        requested_by=row["requested_by"], candidate_path=row["candidate_path"],
                        manifest_sha256=row["manifest_sha256"], operation_id=row["operation_id"],
                        expected_current_version=row["expected_current_version"], requested_at=row["requested_at"],
                    )
                self.db.mark_update_operation_published(row["operation_id"])
            except protocol.PlatformUpdateError as error:
                # Keep the confirmed outbox record, never undo token consumption
                # or pretend a DB commit failure means that no request exists.
                self.db.note_update_operation_error(row["operation_id"], error.code)
                raise

    def recover_pending(self):
        self.reconcile()
        for row in self.db.list_update_operations(pending_only=True):
            if row["status"] != "staged" and not row["published_at"]:
                try:
                    self._publish(dict(row))
                except (protocol.PlatformUpdateError, OSError, sqlite3.Error):
                    continue

    def _run(self):
        while not self._stop.is_set():
            try:
                self.recover_pending()
            except (OSError, sqlite3.Error, protocol.PlatformUpdateError):
                pass
            self._stop.wait(3.0)

    def start(self):
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="update-api-recovery")
            self._thread.start()

    def stop(self, timeout=3.0):
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
