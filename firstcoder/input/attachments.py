"""Attachment discovery, staging, and provider-facing preparation."""

from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from firstcoder.input.clipboard import read_clipboard_image_bytes

AttachmentKind = Literal["image", "file"]

# Keep images within typical provider payload budgets.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Text-like files larger than this are attached as path references only.
MAX_INLINE_TEXT_BYTES = 200 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 16

COMMON_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

_FILE_URI_RE = re.compile(r"file://[^\s]+", re.IGNORECASE)
_PATH_CANDIDATE_RE = re.compile(r"(?:(?:[A-Za-z]:)?(?:/|\\)[^\s]+|(?:\./|\.\./)[^\s]+|~[^\s]+)")
_INLINE_CODE_RE = re.compile(r"`([^`\r\n]+)`")
_QUOTED_TEXT_RE = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"\r\n]+)(?P=quote)")
_BARE_IMAGE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_:/.-])(?:/|\./|\.\./)?[A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:png|jpe?g|gif|webp|bmp)\b",
    re.IGNORECASE,
)
_TEXT_MEDIA_PREFIXES = ("text/",)
_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/x-sh",
    "application/sql",
}


@dataclass(slots=True)
class UserAttachment:
    """An attachment staged in the composer before send."""

    kind: AttachmentKind
    path: Path
    filename: str
    media_type: str
    size_bytes: int
    source: str = "path"  # path | clipboard | paste


@dataclass(slots=True)
class PreparedAttachment:
    """Attachment copied into the session attachment store and ready for persistence."""

    kind: AttachmentKind
    filename: str
    media_type: str
    size_bytes: int
    relative_path: str
    sha256: str
    source: str = "path"
    inline_text: str | None = None


def guess_media_type(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(str(path))
    if media_type == "image/jpg":
        return "image/jpeg"
    return media_type or "application/octet-stream"


def is_image_media_type(media_type: str) -> bool:
    return media_type.startswith("image/")


def is_text_like_media_type(media_type: str, path: Path | None = None) -> bool:
    if media_type.startswith(_TEXT_MEDIA_PREFIXES) or media_type in _TEXT_MEDIA_TYPES:
        return True
    if path is not None:
        suffix = path.suffix.lower()
        if suffix in {
            ".md",
            ".txt",
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".csv",
            ".tsv",
            ".xml",
            ".html",
            ".css",
            ".scss",
            ".sh",
            ".bash",
            ".zsh",
            ".rs",
            ".go",
            ".java",
            ".kt",
            ".c",
            ".cc",
            ".cpp",
            ".h",
            ".hpp",
            ".sql",
            ".log",
            ".env",
            ".gitignore",
            ".dockerfile",
        }:
            return True
    return False


def attach_path(path: str | Path, *, source: str = "path") -> UserAttachment:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Attachment not found: {resolved}")
    if not resolved.is_file():
        raise IsADirectoryError(f"Attachment must be a file: {resolved}")
    size = resolved.stat().st_size
    media_type = guess_media_type(resolved)
    kind: AttachmentKind = "image" if is_image_media_type(media_type) else "file"
    if kind == "image" and size > MAX_IMAGE_BYTES:
        raise ValueError(f"Image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)}MB limit: {resolved.name}")
    return UserAttachment(
        kind=kind,
        path=resolved,
        filename=resolved.name,
        media_type=media_type,
        size_bytes=size,
        source=source,
    )


def extract_explicit_image_references(text: str) -> list[str]:
    """提取文本中明确写出的本地图片路径，并保持首次出现顺序。

    这里只识别路径字面量，不扫描目录，也不把远程 URL 当作本地附件。调用方还必须
    使用 :func:`resolve_explicit_image_references` 做工作区、存在性和大小校验。
    """

    if not text:
        return []

    candidates: list[tuple[int, str]] = []
    for match in _INLINE_CODE_RE.finditer(text):
        candidates.append((match.start(), match.group(1)))
    for match in _QUOTED_TEXT_RE.finditer(text):
        candidates.append((match.start(), match.group("value")))
    candidates.extend((match.start(), match.group(0)) for match in _BARE_IMAGE_PATH_RE.finditer(text))

    references: list[str] = []
    seen: set[str] = set()
    for _, candidate in sorted(candidates, key=lambda item: item[0]):
        value = candidate.strip().lstrip("([{<").rstrip(".,;:!?)]}>")
        if not value or "://" in value or value.lower().startswith("data:"):
            continue
        if Path(value).suffix.lower() not in COMMON_IMAGE_SUFFIXES:
            continue
        if value in seen:
            continue
        seen.add(value)
        references.append(value)
    return references


def resolve_explicit_image_references(text: str, *, workspace_root: str | Path) -> list[Path]:
    """解析工作区内已存在的明确图片引用。

    缺失路径和工作区外路径会被忽略，避免把命令示例、预期输出图片或敏感的沙箱外
    文件误当成输入附件。已存在但超过 provider 图片上限的文件会保留明确错误语义。
    """

    root = Path(workspace_root).expanduser().resolve()
    resolved_paths: list[Path] = []
    seen: set[Path] = set()
    for reference in extract_explicit_image_references(text):
        raw_path = Path(reference).expanduser()
        candidate = raw_path if raw_path.is_absolute() else root / raw_path
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved in seen or not resolved.is_file():
            continue
        attachment = attach_path(resolved, source="path")
        if attachment.kind != "image":
            continue
        seen.add(resolved)
        resolved_paths.append(resolved)
        if len(resolved_paths) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise ValueError(f"Too many attachments (max {MAX_ATTACHMENTS_PER_MESSAGE})")
    return resolved_paths


def parse_path_candidates(text: str) -> list[str]:
    """Extract likely absolute/relative file paths from paste text."""

    if not text or not text.strip():
        return []
    candidates: list[str] = []
    for match in _FILE_URI_RE.findall(text):
        candidates.append(match)
    # Prefer whole-line paths (common when Finder pastes one path per line).
    for line in text.splitlines():
        stripped = line.strip().strip('"').strip("'")
        if not stripped:
            continue
        if stripped.startswith("file://") or stripped.startswith(("/", "~", "./", "../")) or re.match(r"^[A-Za-z]:[\\/]", stripped):
            candidates.append(stripped)
            continue
        # Single-token relative path without spaces.
        if " " not in stripped and ("/" in stripped or "\\" in stripped) and Path(stripped).suffix:
            candidates.append(stripped)
    for match in _PATH_CANDIDATE_RE.findall(text):
        candidates.append(match.strip().strip('"').strip("'"))

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _path_from_candidate(candidate: str) -> Path | None:
    text = candidate.strip().strip('"').strip("'")
    if not text:
        return None
    if text.startswith("file://"):
        # Handle file:///Users/... and file://localhost/Users/...
        without_scheme = text[7:]
        if without_scheme.startswith("localhost"):
            without_scheme = without_scheme[len("localhost") :]
        text = without_scheme
    from urllib.parse import unquote

    text = unquote(text)
    path = Path(text).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if resolved.is_file():
        return resolved
    return None


def resolve_paste_attachments(
    paste_text: str | None,
    *,
    include_clipboard_image: bool = True,
) -> list[UserAttachment]:
    """Resolve attachments from paste text and/or the OS clipboard image."""

    attachments: list[UserAttachment] = []
    seen_paths: set[Path] = set()

    if paste_text:
        for candidate in parse_path_candidates(paste_text):
            path = _path_from_candidate(candidate)
            if path is None or path in seen_paths:
                continue
            try:
                attachment = attach_path(path, source="paste")
            except (OSError, ValueError):
                continue
            attachments.append(attachment)
            seen_paths.add(path)

    # If the paste is only path(s), we still may also have an image on the clipboard.
    # Prefer path attachments when both exist to avoid duplicate noise.
    if include_clipboard_image and not attachments:
        image_bytes = read_clipboard_image_bytes()
        if image_bytes:
            media_type = _sniff_image_media_type(image_bytes)
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(media_type, ".png")
            temp_dir = Path.home() / ".firstcoder" / "tmp" / "clipboard"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"clipboard-{uuid4().hex}{suffix}"
            temp_path.write_bytes(image_bytes)
            try:
                attachment = attach_path(temp_path, source="clipboard")
                attachment.filename = f"clipboard{suffix}"
                attachments.append(attachment)
            except (OSError, ValueError):
                temp_path.unlink(missing_ok=True)

    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValueError(f"Too many attachments (max {MAX_ATTACHMENTS_PER_MESSAGE})")
    return attachments


def prepare_attachments_for_session(
    attachments: list[UserAttachment],
    *,
    store_root: Path,
    session_id: str,
) -> list[PreparedAttachment]:
    """Copy attachments under the session attachment directory."""

    if not attachments:
        return []
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ValueError(f"Too many attachments (max {MAX_ATTACHMENTS_PER_MESSAGE})")

    target_dir = store_root / "attachments" / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[PreparedAttachment] = []

    for item in attachments:
        raw = item.path.read_bytes()
        digest = sha256(raw).hexdigest()
        suffix = item.path.suffix or _suffix_for_media_type(item.media_type)
        safe_name = _safe_filename(item.filename or f"attachment{suffix}")
        dest_name = f"{digest[:16]}-{safe_name}"
        dest_path = target_dir / dest_name
        if not dest_path.exists():
            dest_path.write_bytes(raw)
        relative = dest_path.relative_to(store_root).as_posix()
        inline_text = _inline_attachment_text(item, raw)
        prepared.append(
            PreparedAttachment(
                kind=item.kind,
                filename=item.filename,
                media_type=item.media_type if item.media_type != "image/jpg" else "image/jpeg",
                size_bytes=item.size_bytes,
                relative_path=relative,
                sha256=digest,
                source=item.source,
                inline_text=inline_text,
            )
        )
    return prepared


def load_image_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _inline_attachment_text(item: UserAttachment, raw: bytes) -> str | None:
    if item.kind != "file" or item.size_bytes > MAX_INLINE_TEXT_BYTES or not is_text_like_media_type(item.media_type, item.path):
        return None
    return raw.decode("utf-8", errors="replace")


def format_attachment_chip(attachment: UserAttachment | PreparedAttachment) -> str:
    size = _human_size(attachment.size_bytes)
    icon = "🖼" if attachment.kind == "image" else "📎"
    return f"{icon} {attachment.filename} ({size})"


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name.strip()) or "attachment"
    return cleaned[:120]


def _suffix_for_media_type(media_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "text/plain": ".txt",
        "application/json": ".json",
    }.get(media_type, "")


def _sniff_image_media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"
