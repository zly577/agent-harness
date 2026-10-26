"""Validate fresh installs and legacy settings without starting Docker containers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DockerDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="harness-docker-config-")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        (self.root / "deploy").mkdir()
        shutil.copyfile(ROOT / "deploy/init-env.py", self.root / "deploy/init-env.py")
        shutil.copyfile(ROOT / ".env.example", self.root / ".env.example")
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def generate(self):
        return subprocess.run(
            [sys.executable, str(self.root / "deploy/init-env.py")],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )

    def compose(self, *overrides):
        command = ["docker", "compose", "--env-file", str(self.root / ".env"), "-f", str(ROOT / "docker-compose.yml")]
        for filename in overrides:
            command.extend(["-f", str(ROOT / filename)])
        env = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "SANDBOX_"))}
        result = subprocess.run(
            [*command, "config", "--format", "json"], cwd=ROOT,
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fresh_install_starts_all_four_services(self):
        config = self.compose()
        services = config["services"]
        self.assertEqual(set(services), {"web", "backend", "mysql", "opensandbox"})
        backend = services["backend"]
        self.assertEqual(backend["environment"]["SANDBOX_ENABLED"], "true")
        self.assertEqual(backend["environment"]["SANDBOX_DOMAIN"], "opensandbox:8080")
        self.assertEqual(backend["depends_on"]["opensandbox"]["condition"], "service_healthy")
        self.assertEqual(backend["environment"]["SANDBOX_API_KEY"], services["opensandbox"]["environment"]["OPENSANDBOX_SERVER_API_KEY"])
        self.assertTrue(backend["environment"]["SANDBOX_API_KEY"])
        self.assertNotIn("ports", services["opensandbox"])

    def test_legacy_disabled_settings_cannot_disable_builtin_sandbox(self):
        env_file = self.root / ".env"
        text = env_file.read_text(encoding="utf-8")
        env_file.write_text(text.replace("SANDBOX_ENABLED=true", "SANDBOX_ENABLED=false").replace("SANDBOX_DOMAIN=opensandbox:8080", "SANDBOX_DOMAIN=host.docker.internal:8080"), encoding="utf-8")
        config = self.compose()
        self.assertEqual(config["services"]["backend"]["environment"]["SANDBOX_ENABLED"], "true")
        self.assertEqual(config["services"]["backend"]["environment"]["SANDBOX_DOMAIN"], "opensandbox:8080")
        self.assertEqual(config, self.compose("docker-compose.sandbox.yml"))

    def test_env_generation_preserves_existing_secrets(self):
        original = (self.root / ".env").read_bytes()
        result = self.generate()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / ".env").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
