
import logging

from claw_swebench.config import SETUP_FILES_TO_REMOVE
from claw_swebench.workspace import SWEBenchWorkspace

logger = logging.getLogger(__name__)


def collect_patch(workspace: SWEBenchWorkspace, base_commit: str) -> str:
    return workspace.get_git_diff(base_commit)


def _has_non_ascii(s: str) -> bool:
    return any(ord(c) > 127 for c in s)


def clean_patch(patch: str) -> str:
    if not patch or not patch.strip():
        return ""

    cleaned_hunks = []
    current_hunk = []
    skip_current = False

    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git"):
            if current_hunk and not skip_current:
                cleaned_hunks.extend(current_hunk)
            current_hunk = [line]
            skip_current = False

            for setup_file in SETUP_FILES_TO_REMOVE:
                if f"a/{setup_file}" in line or f"b/{setup_file}" in line:
                    skip_current = True
                    logger.debug("Stripping setup file from patch: %s", setup_file)
                    break

            if not skip_current and _has_non_ascii(line):
                skip_current = True
                logger.debug("Stripping non-ASCII filename diff from patch")

            if "Binary files" in line:
                skip_current = True
        else:
            current_hunk.append(line)

            if line.startswith("Binary files") or line.startswith("GIT binary patch"):
                skip_current = True

    if current_hunk and not skip_current:
        cleaned_hunks.extend(current_hunk)

    result = "".join(cleaned_hunks).rstrip()
    if result:
        result += "\n"
    return result


def is_empty_patch(patch: str) -> bool:
    if not patch or not patch.strip():
        return True

    has_changes = False
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            has_changes = True
            break
        if line.startswith("-") and not line.startswith("---"):
            has_changes = True
            break

    return not has_changes
