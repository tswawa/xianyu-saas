"""Stable manager errors and process exit codes."""

from __future__ import annotations

import re


_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")

EXIT_CODES = {
    "manager_cli_invalid": 2,
    "manager_linux_required": 3,
    "manager_root_required": 4,
    "manager_architecture_unsupported": 5,
    "manager_service_invalid": 6,
    "manager_systemctl_failed": 7,
    "manager_public_key_missing": 8,
    "manager_public_key_invalid": 9,
    "manager_self_hash_failed": 10,
    "manager_updater_unavailable": 11,
    "manager_updater_failed": 12,
    "manager_install_not_implemented": 13,
    "manager_install_failed": 14,
    "manager_install_file_invalid": 14,
    "manager_install_path_unsafe": 14,
    "manager_install_path_occupied": 14,
    "manager_install_file_failed": 14,
    "manager_install_link_failed": 14,
    "manager_install_permissions_failed": 14,
    "manager_install_identity_invalid": 14,
    "manager_install_incomplete": 14,
    "manager_install_version_conflict": 14,
    "manager_install_os_unsupported": 14,
    "manager_systemd_unavailable": 14,
    "manager_nginx_unavailable": 14,
    "manager_useradd_unavailable": 14,
    "manager_install_disk_check_failed": 14,
    "manager_install_disk_insufficient": 14,
    "manager_install_port_in_use": 14,
    "manager_template_missing": 14,
    "manager_public_key_conflict": 14,
    "manager_environment_invalid": 14,
    "manager_command_rejected": 14,
    "manager_command_failed": 14,
    "manager_download_url_rejected": 14,
    "manager_download_too_large": 14,
    "manager_download_failed": 14,
    "manager_release_version_invalid": 14,
    "manager_release_metadata_invalid": 14,
    "manager_release_asset_missing": 14,
    "manager_release_asset_mismatch": 14,
    "manager_artifacts_signature_invalid": 14,
    "manager_artifacts_invalid": 14,
    "manager_bootstrap_identity_invalid": 14,
    "manager_asset_hash_mismatch": 14,
    "manager_manifest_signature_invalid": 14,
    "manager_manifest_invalid": 14,
    "manager_runtime_metadata_invalid": 14,
    "manager_archive_path_invalid": 14,
    "manager_archive_path_collision": 14,
    "manager_archive_invalid": 14,
    "manager_archive_special_file": 14,
    "manager_archive_manifest_mismatch": 14,
    "manager_archive_too_large": 14,
    "manager_archive_hash_mismatch": 14,
    "manager_account_creation_failed": 14,
    "manager_account_conflict": 14,
    "manager_install_health_failed": 14,
    "manager_legacy_layout_unsafe": 14,
    "manager_migration_not_implemented": 14,
    "manager_internal_invalid": 15,
    "manager_failed": 70,
}


class ManagerError(RuntimeError):
    """An expected failure with a machine-stable public code."""

    def __init__(self, code: str, message: str = "manager operation failed"):
        safe = str(code)
        if not _ERROR_CODE.fullmatch(safe):
            safe = "manager_failed"
        super().__init__(message)
        self.code = safe

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, EXIT_CODES["manager_failed"])
