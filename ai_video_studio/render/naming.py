"""Output file names: templates, Windows-safe names and conflict handling (no Qt)."""

from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai_video_studio.core.errors import StudioError

TEMPLATE_FIELDS = {
    "original_name": "file name of the source video without extension",
    "preset": "preset name",
    "resolution": "output size, e.g. 1080x1920",
    "width": "output width",
    "height": "output height",
    "codec": "h264 or h265",
    "date": "today's date, YYYY-MM-DD",
    "index": "position in the batch (001, 002, …)",
}
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
MAX_NAME = 150  # characters before the extension; keeps full paths well below Windows limits
CONFLICT_POLICIES = {"rename": "Add a number (keep both)", "skip": "Skip the video", "replace": "Replace the file"}


class NamingError(StudioError):
    pass


def template_fields(template: str) -> list[str]:
    try:
        return [name for _text, name, _spec, _conv in string.Formatter().parse(template) if name is not None]
    except ValueError as exc:
        raise NamingError(f"The file name template is not valid: {exc}") from exc


def validate_template(template: str) -> None:
    if not template.strip():
        raise NamingError("The file name template is empty.")
    unknown = [f for f in template_fields(template) if f not in TEMPLATE_FIELDS]
    if unknown:
        raise NamingError(f"Unknown field(s) in the file name template: {', '.join('{' + f + '}' for f in unknown)}. "
                          f"Available: {', '.join('{' + f + '}' for f in TEMPLATE_FIELDS)}")
    if "{" not in template:
        raise NamingError("The template must contain at least one field such as {original_name}, otherwise every "
                          "video would get the same name.")


def sanitize_filename(name: str) -> str:
    """A file name Windows accepts: no reserved characters, names or trailing dots/spaces."""
    name = INVALID_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    name = name[:MAX_NAME].rstrip(". ")
    if not name:
        name = "output"
    if name.split(".")[0].upper() in RESERVED:
        name = "_" + name
    return name


def render_name(template: str, fields: dict) -> str:
    validate_template(template)
    values = {k: str(v) for k, v in fields.items()}
    values.setdefault("date", datetime.now().strftime("%Y-%m-%d"))
    try:
        return sanitize_filename(template.format(**values))
    except (KeyError, IndexError, ValueError) as exc:
        raise NamingError(f"The file name template could not be filled in: {exc}") from exc


def path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass
class OutputDecision:
    item_id: str
    path: Path | None   # None = skipped
    action: str         # write | rename | replace | skip
    reason: str = ""


def numbered(path: Path, n: int) -> Path:
    return path.with_name(f"{path.stem} ({n}){path.suffix}")


def plan_outputs(planned: list[tuple[str, Path]], policy: str, sources: set[str], busy: set[str],
                 exists=lambda p: p.exists()) -> list[OutputDecision]:
    """Decide the final output path for each ``(item_id, wanted_path)``.

    * A source video is never an output: a numbered name is used instead.
    * Two videos of the batch never get the same name: later ones are numbered.
    * A file another queued or running job will write (``busy``) is never used.
    * An existing file is replaced only with policy "replace"; "skip" skips the video and
      "rename" adds a number.
    """
    if policy not in CONFLICT_POLICIES:
        raise NamingError(f"Unknown conflict policy '{policy}'")
    taken = set(busy)
    decisions = []
    for item_id, wanted in planned:
        path, action, reason = Path(wanted), "write", ""
        key = path_key(path)

        def free(candidate: Path) -> bool:
            k = path_key(candidate)
            return k not in taken and k not in sources and not exists(candidate)

        if key in sources:
            action, reason = "rename", "the name would overwrite a source video"
        elif key in taken:
            action, reason = "rename", "another video in the queue already uses this name"
        elif exists(path):
            if policy == "skip":
                decisions.append(OutputDecision(item_id, None, "skip", f"{path.name} already exists"))
                continue
            action, reason = ("replace", f"{path.name} already exists and will be replaced") if policy == "replace" \
                else ("rename", f"{path.name} already exists")
        if action == "rename":
            n = 2
            while not free(numbered(path, n)):
                n += 1
            path = numbered(path, n)
        taken.add(path_key(path))
        decisions.append(OutputDecision(item_id, path, action, reason))
    return decisions
