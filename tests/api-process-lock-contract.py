#!/usr/bin/env python3
"""Contract for the single API-supervisor process invariant."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "backend" / ".venv" / "bin" / "python"
IMPORT_APP = "import app"
HOLD_APP = "import app, sys; print('READY', flush=True); sys.stdin.read()"


def environment(database: Path, tenants: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT / "backend"),
            "SAAS_DB": str(database),
            "SAAS_TENANTS_DIR": str(tenants),
            "SAAS_RESTORE_WORKERS": "0",
            "SAAS_COOKIE_SECURE": "0",
            "SAAS_TESTING": "1",
            "SAAS_PLATFORM_AI_BASE_URL": "",
            "SAAS_PLATFORM_AI_MODEL": "",
            "SAAS_PLATFORM_AI_KEY": "",
        }
    )
    return env


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="xianyu-api-lock-") as root:
        root_path = Path(root)
        shared_env = environment(root_path / "shared.db", root_path / "tenants-shared")
        owner = subprocess.Popen(
            [str(PYTHON), "-u", "-c", HOLD_APP],
            cwd=ROOT / "backend",
            env=shared_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert owner.stdout is not None
            assert owner.stdout.readline().strip() == "READY"

            duplicate = subprocess.run(
                [str(PYTHON), "-c", IMPORT_APP],
                cwd=ROOT / "backend",
                env=shared_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert duplicate.returncode != 0
            assert "another xianyu-saas API supervisor" in duplicate.stderr

            isolated_env = environment(root_path / "isolated.db", root_path / "tenants-isolated")
            isolated = subprocess.run(
                [str(PYTHON), "-c", IMPORT_APP],
                cwd=ROOT / "backend",
                env=isolated_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert isolated.returncode == 0, isolated.stderr
        finally:
            if owner.stdin is not None:
                owner.stdin.close()
            owner.wait(timeout=30)

    print("api process lock contract: duplicate shared supervisor rejected")


if __name__ == "__main__":
    main()
