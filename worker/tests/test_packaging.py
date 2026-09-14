import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent


def _checked_utf8(raw: bytes, relative_path: str) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(f"{relative_path} must not contain a UTF-8 BOM")
    if b"\x00" in raw:
        raise AssertionError(f"{relative_path} contains NUL bytes")
    return raw.decode("utf-8")


def read_utf8(relative_path: str) -> str:
    return _checked_utf8((ROOT / relative_path).read_bytes(), relative_path)


def read_repo_utf8(relative_path: str) -> str:
    return _checked_utf8((REPO_ROOT / relative_path).read_bytes(), relative_path)


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


def dockerfile_copy_sources(dockerfile: str) -> set[str]:
    sources: set[str] = set()
    for line in dockerfile.splitlines():
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
            "products_config.json",
            "prompts/default_prompt_example.txt",
        )
        for path in required_paths:
            with self.subTest(path=path):
                self.assertFalse(is_git_ignored(path), f"{path} must be versionable")

    def test_root_docker_context_excludes_worker_runtime_secrets(self):
        patterns = {
            line.strip()
            for line in read_repo_utf8(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected = {
            "**/.env",
            "**/.env.*",
            "worker/**/redeem_codes.json*",
            "worker/**/redeem_sent.json*",
            "worker/**/trial_codes.json*",
            "worker/**/trial_sent.json*",
            "worker/**/pan_links.json*",
            "worker/**/pan_sent.json*",
            "worker/**/reply_rules.json*",
            "worker/**/legacy_delivery_ledger.json*",
            "worker/**/delivery_state.db*",
            "worker/data",
            "worker/runtime-data",
            "worker/prompts/*_prompt.txt",
            "**/*.db.*",
            "**/*.sqlite",
            "**/*.sqlite-*",
            "**/*.sqlite.*",
            "**/*.sqlite3",
            "**/*.sqlite3-*",
            "**/*.sqlite3.*",
        }
        self.assertTrue(expected.issubset(patterns))
        self.assertNotIn("*.json", patterns)
        self.assertNotIn("*.py", patterns)
        self.assertNotIn("worker/products_config.json", patterns)
        self.assertFalse(any("prompt_example" in pattern for pattern in patterns))

    def test_base_product_mapping_has_no_material_payload(self):
        payload = json.loads(read_utf8("products_config.json"))
        for entry in payload.get("types", []):
            with self.subTest(product=entry.get("id")):
                self.assertNotIn("payload", entry)

    def test_runtime_rule_file_is_not_copied_into_image(self):
        dockerfile = read_repo_utf8("Dockerfile")
        self.assertNotIn("reply_rules.json", dockerfile)


class RuntimeLoggingContractTests(unittest.TestCase):
    def test_worker_defaults_to_safe_operational_logging(self):
        main = read_utf8("main.py")
        self.assertIn('os.getenv("LOG_LEVEL", "INFO")', main)
        self.assertIn("backtrace=False", main)
        self.assertIn("diagnose=False", main)
        self.assertNotIn("exc_info=True", main)


class DockerContractTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = read_repo_utf8("Dockerfile")
        self.sources = dockerfile_copy_sources(self.dockerfile)

    def test_image_contains_control_plane_worker_and_web_runtime(self):
        for source in ("backend/", "worker/", "frontend/", "docker/entrypoint.sh"):
            with self.subTest(source=source):
                self.assertIn(source, self.sources)
        for script in ("main.py", "XianyuAgent.py", "XianyuApis.py", "context_manager.py", "delivery_store.py"):
            self.assertTrue((ROOT / script).is_file(), script)
        self.assertIn("worker/", self.sources)
        self.assertNotIn("COPY . ", self.dockerfile)

    def test_image_uses_audited_prompt_templates(self):
        self.assertIn("worker/", self.sources)
        patterns = {
            line.strip()
            for line in read_repo_utf8(".dockerignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertFalse(any("prompt_example" in pattern for pattern in patterns))

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
        self.assertIn("useradd -u 10001 -g xianyu", self.dockerfile)
        self.assertIn('VOLUME ["/data"]', self.dockerfile)
        user_offset = self.dockerfile.index("USER xianyu:xianyu")
        entrypoint_offset = self.dockerfile.index('ENTRYPOINT ["/app/docker/entrypoint.sh"]')
        self.assertLess(user_offset, entrypoint_offset)
        self.assertIn("SAAS_BOT_ROOT=/app/worker", self.dockerfile)


if __name__ == "__main__":
    unittest.main()
