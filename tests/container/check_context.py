"""Exercise the real Docker ignore filter using only inert temporary files.

Run explicitly in the Docker CI job, not daemon-free unittest discovery. No
images are pulled, containers started, or checkout contents sent to the daemon.
"""

import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = {
    ".env", "local-state.db", "local-record.json", ".git/config",
    ".venv/credentials.py", ".superpowers/record.json", ".omo/record.json",
    "mail_notifier/.env", "mail_notifier/local-state.db", "mail_notifier/local-record.json",
    "mail_notifier/__pycache__/config.pyc", "mail_notifier/backup.py/private.env",
    "tests/.env", "tests/local-state.db", "tests/local-record.json", "tests/private/profile.env",
    "tests/backup.py/private.env", "tests/container/.env", "tests/container/local-state.db",
    "tests/container/local-record.json", "tests/container/backup.py/private.env",
    "web/.env", "web/local-record.json", "web/credentials/.env", "web/credentials/local-state.db",
    "web/credentials/v1/private.env", "web/credentials/v1/local-state.db",
    "web/credentials/v1/local-record.json", "web/credentials/v1/backup.mjs/private.env",
    "web/credentials/v1/backup.html/private.env", "web/credentials/v1/backup.css/private.env",
    "web/credentials/v2/private.env",
}


class DockerContextTests(unittest.TestCase):
    def test_only_required_files_survive_the_real_docker_filter(self):
        required = {"pyproject.toml", "README.md", "LICENSE", "__init__.py", "plugin.yaml",
                    "tests/container/Dockerfile", "web/credentials/v1/index.html",
                    "web/credentials/v1/app.mjs", "web/credentials/v1/crypto.mjs",
                    "web/credentials/v1/messages.mjs", "web/credentials/v1/styles.css"}
        for folder in ("mail_notifier", "tests", "tests/container"):
            required.update(p.relative_to(ROOT).as_posix() for p in (ROOT / folder).glob("*.py"))
        self.assertTrue(all((ROOT / name).is_file() for name in required))
        with tempfile.TemporaryDirectory(prefix="mail-context-check-", dir=ROOT.parent) as directory:
            context = Path(directory) / "context"
            output = Path(directory) / "output"
            context.mkdir()
            for name in required | FORBIDDEN:
                path = context / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("inert Docker context sentinel: " + name + "\n")
            (context / ".dockerignore").write_bytes((ROOT / ".dockerignore").read_bytes())
            (context / "Dockerfile").write_text("FROM scratch\nCOPY . /\n")
            command = ["docker", "build", "--network", "none", "--progress", "plain",
                       "--output", f"type=local,dest={output}", str(context)]
            print(shlex.join(command), flush=True)
            subprocess.run(command, check=True, timeout=90)
            included = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
            print(json.dumps({"required_files": len(required), "included_files": len(included),
                              "included_forbidden": sorted(included & FORBIDDEN)}), flush=True)
            self.assertEqual(required - included, set(), "Required build inputs were excluded")
            self.assertEqual(included, required, "Docker included non-allowlisted local files")


if __name__ == "__main__":
    unittest.main(verbosity=2)
