"""Own exactly one ephemeral profile volume; never touch other Docker state."""

import subprocess
import sys
from uuid import uuid4


def main() -> int:
    volume = "mail-onboarding-smoke-" + str(uuid4())
    created = False
    result = 1
    print("Offline smoke: Telegram/IMAP/model delivery are fake; no live support proof.", flush=True)
    try:
        subprocess.run(["docker", "volume", "create", volume], check=True)
        created = True
        for phase in ("write", "resume"):
            subprocess.run(["docker", "run", "--rm", "--network", "none",
                            "--mount", f"type=volume,src={volume},dst=/profile",
                            "mail-onboarding-smoke", phase], check=True)
        result = 0
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Docker smoke unperformed or failed: {exc}", file=sys.stderr)
    finally:
        if created:
            try:
                subprocess.run(["docker", "volume", "rm", volume], check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"Owned volume cleanup failed ({volume}): {exc}", file=sys.stderr)
                result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
