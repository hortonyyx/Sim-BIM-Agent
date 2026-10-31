"""Native GLM subscription launcher; credentials only enter the child process."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agent_runtime.providers import (
    GLM_SUBSCRIPTION_ANTHROPIC, subscription_credentials,
)


def main():
    base_url, key = subscription_credentials(ROOT / ".env", provider=GLM_SUBSCRIPTION_ANTHROPIC)
    executable = shutil.which("claude")
    if executable is None:
        raise SystemExit("Claude Code is not installed or is missing from PATH")
    env = {name: value for name, value in os.environ.items()
           if name not in {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}}
    env.update(ANTHROPIC_BASE_URL=base_url, ANTHROPIC_AUTH_TOKEN=key,
               ANTHROPIC_MODEL=env.get("GLM_MODEL", "glm-5.3"),
               ANTHROPIC_SMALL_FAST_MODEL=env.get("GLM_SMALL_MODEL", "glm-5.3"))
    return subprocess.call([executable, *sys.argv[1:]], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
