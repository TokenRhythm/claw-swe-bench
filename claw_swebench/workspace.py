
import logging
import subprocess
import time
from dataclasses import dataclass

from claw_swebench import netlock
from claw_swebench.config import (
    CONTAINER_MEMORY,
    NETWORK_WHITELIST,
    CONTAINER_PIDS_LIMIT,
    GIT_USER_EMAIL,
    GIT_USER_NAME,
    GITIGNORE_PATTERNS,
    instance_id_to_container,
    instance_id_to_image,
    instance_id_to_image_sweagent,
)

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:

    stdout: str
    stderr: str
    exit_code: int


class SWEBenchWorkspace:

    def __init__(self, instance_id: str, adapter):
        self.instance_id = instance_id
        self.adapter = adapter
        self.image_name = self._resolve_image(instance_id)
        self.container_name = instance_id_to_container(adapter.name, instance_id)
        self._started = False

    @staticmethod
    def _resolve_image(instance_id: str) -> str:
        for name_fn in (instance_id_to_image, instance_id_to_image_sweagent):
            candidate = name_fn(instance_id)
            result = subprocess.run(
                ["docker", "image", "inspect", candidate],
                capture_output=True,
            )
            if result.returncode == 0:
                return candidate
        return instance_id_to_image(instance_id)

    def start(self) -> str:
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
        )

        logger.info("Starting container %s from image %s",
                     self.container_name, self.image_name)
        net_args = netlock.docker_args(NETWORK_WHITELIST)
        cmd = [
            "docker", "run", "-d",
            *net_args,
            "--name", self.container_name,
            "--pids-limit", str(CONTAINER_PIDS_LIMIT),
            "--memory", CONTAINER_MEMORY,
            "--memory-swap", CONTAINER_MEMORY,
            "--add-host", "host.docker.internal:host-gateway",
        ]
        cmd.extend(self.adapter.container_run_args(self.instance_id))
        cmd.extend([self.image_name, "tail", "-f", "/dev/null"])

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to start container: {result.stderr.strip()}"
            )
        self._started = True

        self.adapter.post_container_start(self)
        logger.info("Container %s started.", self.container_name)
        return self.container_name

    def run_in_container(self, cmd: str, timeout: int = 300) -> ExecResult:
        try:
            result = subprocess.run(
                ["docker", "exec", self.container_name, "bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ExecResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Command timed out after %ds: %s", timeout, cmd[:100])
            return ExecResult(stdout="", stderr="TIMEOUT", exit_code=-1)

    def get_container_ip(self) -> str:
        result = subprocess.run(
            ["docker", "inspect", "-f",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
             self.container_name],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def copy_from_container(self, container_path: str, host_path: str) -> bool:
        result = subprocess.run(
            ["docker", "cp", f"{self.container_name}:{container_path}", host_path],
            capture_output=True,
        )
        return result.returncode == 0

    def copy_to_container(self, host_path: str, container_path: str) -> bool:
        result = subprocess.run(
            ["docker", "cp", host_path, f"{self.container_name}:{container_path}"],
            capture_output=True,
        )
        return result.returncode == 0

    def prepare_instance(
        self, base_commit: str, setup_gitignore: bool = False
    ) -> None:
        logger.info("Preparing instance %s at commit %s",
                     self.instance_id, base_commit[:12])

        r = self.run_in_container(f"cd /testbed && git reset --hard {base_commit}")
        if r.exit_code != 0:
            raise RuntimeError(f"git reset failed: {r.stderr}")

        r = self.run_in_container("cd /testbed && git clean -fd")
        if r.exit_code != 0:
            logger.warning("git clean failed: %s", r.stderr)

        cleanup_script = (
            'cd /testbed && '
            f'TARGET_TS=$(git show -s --format=%ct {base_commit}) && '
            'git for-each-ref --format="%(refname)" '
            'refs/heads refs/remotes refs/tags | '
            'while read ref; do '
            '  CT=$(git log -1 --format=%ct "$ref" 2>/dev/null) || continue; '
            '  if [ -n "$CT" ] && [ "$CT" -gt "$TARGET_TS" ]; then '
            '    git update-ref -d "$ref" 2>/dev/null || true; '
            '  fi; '
            'done; '
            'git reflog expire --expire=now --all && '
            'git gc --prune=now >/dev/null 2>&1 && '
            'AFTER=$(date -d "@$((TARGET_TS + 1))" "+%Y-%m-%d %H:%M:%S") && '
            'COUNT=$(git log --oneline --all --since="$AFTER" 2>/dev/null | wc -l) && '
            '[ "$COUNT" -eq 0 ]'
        )
        r = None
        for attempt in (1, 2):
            r = self.run_in_container(cleanup_script, timeout=180)
            if r.exit_code == 0:
                break
            logger.warning(
                "Future-commit cleanup attempt %d/2 failed for %s (exit=%d): %s",
                attempt, self.instance_id, r.exit_code, r.stderr[:200],
            )
            if attempt == 1:
                time.sleep(30)
        if r.exit_code != 0:
            raise RuntimeError(
                f"Future-commit cleanup failed twice for {self.instance_id} "
                f"(exit={r.exit_code}) — refusing to run a potentially leaky "
                f"container: {r.stderr[:300]}"
            )
        logger.info("Stripped future commits/tags/branches from %s", self.instance_id)

        self.run_in_container(
            f'git config --global user.email "{GIT_USER_EMAIL}" && '
            f'git config --global user.name "{GIT_USER_NAME}"'
        )

        if setup_gitignore:
            patterns = "\n".join(GITIGNORE_PATTERNS)
            self.run_in_container(
                f"cat > /root/.gitignore_global << 'GITIGNORE'\n"
                f"{patterns}\n"
                f"GITIGNORE\n"
                f"git config --global core.excludesfile /root/.gitignore_global"
            )
            logger.info("Injected global gitignore for build artifacts.")

    def is_repo_clean(self) -> bool:
        r = self.run_in_container("cd /testbed && git status --porcelain")
        return r.exit_code == 0 and r.stdout.strip() == ""

    def reset_repo(self, base_commit: str) -> None:
        self.run_in_container(f"cd /testbed && git reset --hard {base_commit}")
        self.run_in_container("cd /testbed && git clean -fd")

    def get_git_diff(self, base_commit: str) -> str:
        r = self.run_in_container("cd /testbed && git add -A")
        if r.exit_code != 0:
            logger.warning("git add -A failed: %s", r.stderr)

        self.run_in_container(
            'cd /testbed && '
            'for f in $(git diff --cached --name-only); do '
            '  if file "/testbed/$f" 2>/dev/null | grep -q "binary"; then '
            '    git reset HEAD -- "$f" 2>/dev/null; '
            '  fi; '
            'done'
        )

        r = self.run_in_container(
            f"cd /testbed && git diff --no-color --cached {base_commit}",
            timeout=60,
        )
        if r.exit_code != 0:
            logger.warning("git diff failed: %s", r.stderr)
            return ""

        return r.stdout

    def cleanup(self) -> None:
        if not self._started:
            return
        logger.info("Cleaning up container %s", self.container_name)
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
        )
        self._started = False
