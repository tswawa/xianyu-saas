import ast
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_utf8(relative_path: str) -> str:
    raw = (ROOT / relative_path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"{relative_path} must not contain a UTF-8 BOM")
    if b"\x00" in raw:
        raise AssertionError(f"{relative_path} contains NUL bytes")
    return raw.decode("utf-8")


def is_git_ignored(relative_path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", relative_path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr.strip() or "git check-ignore failed")
    return result.returncode == 0


def dockerfile_copy_sources(relative_path: str = "Dockerfile") -> set[str]:
    sources: set[str] = set()
    for line in read_utf8(relative_path).splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        tokens = stripped.split()
        if len(tokens) < 3:
            continue
        tokens = tokens[1:]
        if tokens and tokens[0].startswith("--from="):
            tokens = tokens[2:]
        if len(tokens) < 2:
            continue
        sources.update(tokens[:-1])
    return sources


def top_level_local_import_copy_sources(relative_path: str = "main.py") -> set[str]:
    tree = ast.parse(read_utf8(relative_path), filename=relative_path)
    sources: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level != 0 or not node.module:
            continue
        root = node.module.split(".", 1)[0]
        if (ROOT / f"{root}.py").is_file():
            sources.add(f"{root}.py")
        elif (ROOT / root).is_dir():
            sources.add(f"{root}/")
    return sources


class IgnoreContractTests(unittest.TestCase):
    def test_sensitive_runtime_files_are_git_ignored(self):
        sensitive_paths = (
            ".env",
            ".env.production",
            "redeem_codes.json",
            "redeem_codes.json.bak",
            "redeem_sent.json",
            "trial_codes.json",
            "trial_sent.json",
            "pan_links.json",
            "pan_sent.json",
            "reply_rules.json",
            "reply_rules.json.bak",
            "delivery_state.db",
            "delivery_state.db-wal",
            "data/chat_history.db",
            "runtime-data/delivery_state.db",
        )
        for path in sensitive_paths:
            with self.subTest(path=path):
                self.assertTrue(is_git_ignored(path), f"{path} is not ignored")

    def test_required_application_files_are_not_git_ignored(self):
        required_paths = (
            ".env.example",
            "main.py",
            "XianyuAgent.py",
            "XianyuApis.py",
            "context_manager.py",
            "delivery_store.py",
            "tutorial_text.py",
            "products_config.json",
            "prompts/default_prompt_example.txt",
        )
        for path in required_paths:
            with self.subTest(path=path):
                self.assertFalse(is_git_ignored(path), f"{path} must be versionable")

    def test_docker_context_excludes_runtime_secrets(self):
        patterns = {
            line.strip()
            for line in read_utf8(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected = {
            ".env",
            ".env.*",
            "redeem_codes.json*",
            "redeem_sent.json*",
            "trial_codes.json*",
            "trial_sent.json*",
            "pan_links.json*",
            "pan_sent.json*",
            "reply_rules.json*",
            "delivery_state.db*",
            "data/",
            "runtime-data/",
        }
        self.assertTrue(expected.issubset(patterns))
        self.assertNotIn("*.json", patterns)
        self.assertNotIn("*.py", patterns)
        self.assertNotIn("products_config.json", patterns)
        self.assertNotIn("tutorial_text.py", patterns)

    def test_base_product_mapping_has_no_material_payload(self):
        payload = json.loads(read_utf8("products_config.json"))
        for entry in payload.get("types", []):
            with self.subTest(product=entry.get("id")):
                self.assertNotIn("payload", entry)

    def test_runtime_rule_file_is_not_copied_into_image(self):
        dockerfile = read_utf8("Dockerfile")
        self.assertNotIn("reply_rules.json", dockerfile)


class RuntimeLoggingContractTests(unittest.TestCase):
    def test_worker_defaults_to_safe_operational_logging(self):
        main = read_utf8("main.py")
        self.assertIn('os.getenv("LOG_LEVEL", "INFO")', main)
        self.assertIn("backtrace=False", main)
        self.assertIn("diagnose=False", main)
        self.assertNotIn("exc_info=True", main)


class DockerContractTests(unittest.TestCase):
    def test_image_contains_required_read_only_application_files(self):
        dockerfile_sources = dockerfile_copy_sources()
        expected_sources = {
            "main.py",
            "XianyuAgent.py",
            "XianyuApis.py",
            "context_manager.py",
            "delivery_store.py",
            "tutorial_text.py",
            "products_config.json",
            "scripts/list_manual_reviews.py",
            "scripts/resolve_manual_review.py",
            "scripts/manage_inbound_dead_letters.py",
            "scripts/migrate_state.py",
        }
        expected_sources.update(top_level_local_import_copy_sources())
        for source in sorted(expected_sources):
            with self.subTest(source=source):
                self.assertIn(source, dockerfile_sources)
        self.assertIn("utils/", dockerfile_sources)
        dockerfile = read_utf8("Dockerfile")
        self.assertNotIn("COPY . ", dockerfile)

    def test_image_uses_audited_prompt_templates(self):
        dockerfile = read_utf8("Dockerfile")
        for name in ("classify", "price", "tech", "default"):
            self.assertIn(
                f"COPY prompts/{name}_prompt_example.txt ./prompts/{name}_prompt.txt",
                dockerfile,
            )

        combined = "\n".join(
            read_utf8(f"prompts/{name}_prompt_example.txt")
            for name in ("classify", "price", "tech", "default")
        )
        self.assertIn("不参与 Worker 运行时回复", combined)
        for forbidden in (
            "DeepSeek",
            "deepseek-",
            "元/百万 token",
            "[TRIAL]",
            "[TUTORIAL]",
        ):
            self.assertNotIn(forbidden, combined)

    def test_image_runs_as_unprivileged_user_with_one_writable_volume(self):
        dockerfile = read_utf8("Dockerfile")
        self.assertIn("adduser -S -D -H -u 10001", dockerfile)
        self.assertIn("XIAN_YU_DATA_DIR=/app/data", dockerfile)
        self.assertIn('VOLUME ["/app/data"]', dockerfile)
        user_offset = dockerfile.index("USER xianyu-agent:xianyu-agent")
        command_offset = dockerfile.index('CMD ["python", "main.py"]')
        self.assertLess(user_offset, command_offset)

    def test_compose_is_utf8_and_mounts_only_runtime_state_writable(self):
        compose = read_utf8("docker-compose.yml")
        required_fragments = (
            "user: \"10001:10001\"",
            "env_file:",
            "- .env",
            "XIAN_YU_DATA_DIR: /app/data",
            "./runtime-data:/app/data",
            "./prompts:/app/prompts:ro",
            "read_only: true",
            'restart: "on-failure:5"',
            "no-new-privileges:true",
            "cap_drop:",
            "- ALL",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, compose)
        self.assertNotIn(".env:/app/.env", compose)


class SystemdContractTests(unittest.TestCase):
    def test_unit_uses_dedicated_layout_and_data_directory(self):
        unit = read_utf8("systemd/xianyu-autoagent.service")
        required_fragments = (
            "User=xianyu-agent",
            "Group=xianyu-agent",
            "WorkingDirectory=/opt/xianyu-autoagent",
            "EnvironmentFile=/etc/xianyu-autoagent.env",
            "Environment=XIAN_YU_DATA_DIR=/var/lib/xianyu-autoagent",
            "Environment=AUTOMATION_MODE=rules_ai",
            "StateDirectory=xianyu-autoagent",
            "StateDirectoryMode=0700",
            "ReadWritePaths=/var/lib/xianyu-autoagent",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, unit)
        self.assertNotIn("/home/admin", unit)

    def test_unit_enables_required_hardening(self):
        unit = read_utf8("systemd/xianyu-autoagent.service")
        hardening = (
            "NoNewPrivileges=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "PrivateTmp=true",
            "PrivateDevices=true",
            "ProtectProc=invisible",
            "ProtectKernelTunables=true",
            "RestrictSUIDSGID=true",
            "SystemCallArchitectures=native",
            "CapabilityBoundingSet=",
        )
        for directive in hardening:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)


if __name__ == "__main__":
    unittest.main()
