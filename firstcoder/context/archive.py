"""Durable, content-addressed storage for compacted tool results.

The archive intentionally owns only bytes-on-disk and the two placeholder
formats.  It does not decide *when* a result should be compacted; the context
pipeline remains responsible for that policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firstcoder.context.models import MessagePart, utc_now_iso
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.context.versions import ARCHIVE_SCHEMA_VERSION, COMPACTION_STRATEGY_VERSION


_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,128}")


class ArchiveIntegrityError(RuntimeError):
    """Raised when an archive's immutable content and digest disagree."""


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    """The public, path-free identity and size information for one archive."""

    archive_id: str
    session_id: str
    content_sha256: str
    original_chars: int
    original_tokens: int
    created_at: str
    schema_version: str = ARCHIVE_SCHEMA_VERSION


@dataclass(slots=True)
class ToolResultArchive:
    """Persist full tool output and construct compact context projections."""

    root: str | Path
    preview_chars: int = 500

    def store_original(
        self,
        session_id: str,
        part: MessagePart,
        original_content: str | None = None,
    ) -> ArchiveRecord:
        """Store immutable full text under a content-addressed archive id.

        Re-storing identical content is a no-op.  A pre-existing file with a
        different digest is corruption, never something this method repairs or
        overwrites.
        """

        self._validate_part(part)
        raw = part.content if original_content is None else original_content
        digest = _sha256(raw)
        return self._store(
            session_id=session_id,
            archive_id=f"ar_{digest[:32]}",
            raw=raw,
            content_sha256=digest,
        )

    def read(self, session_id: str, archive_id: str) -> tuple[ArchiveRecord, str]:
        """Return verified archive metadata and its full original content.

        Callers receive no filesystem paths; all integrity validation remains
        owned by this archive boundary before bytes leave it.
        """

        text_path, metadata_path = self._archive_paths(session_id, archive_id)
        raw = text_path.read_text(encoding="utf-8")
        metadata = _read_metadata(metadata_path)
        actual_digest = _sha256(raw)

        if metadata.get("schema_version") == ARCHIVE_SCHEMA_VERSION:
            expected_id = metadata.get("archive_id")
            expected_digest = metadata.get("content_sha256")
            if expected_id != archive_id or not isinstance(expected_digest, str):
                raise ArchiveIntegrityError(f"{ARCHIVE_SCHEMA_VERSION} archive metadata is invalid")
            if expected_digest != actual_digest:
                raise ArchiveIntegrityError("archive content does not match its SHA-256")
            if archive_id != _content_addressed_id(actual_digest):
                raise ArchiveIntegrityError(
                    f"{ARCHIVE_SCHEMA_VERSION} archive id does not match its content SHA-256"
                )
            if metadata.get("original_chars") != len(raw):
                raise ArchiveIntegrityError("archive character count does not match content")
            return (
                ArchiveRecord(
                    archive_id=archive_id,
                    session_id=session_id,
                    content_sha256=actual_digest,
                    original_chars=len(raw),
                    original_tokens=_metadata_tokens(metadata, raw),
                    created_at=_metadata_created_at(metadata),
                    schema_version=ARCHIVE_SCHEMA_VERSION,
                ),
                raw,
            )

        # v1 archives used random IDs plus a non-authoritative fingerprint.
        # They remain readable, but their true digest is always recomputed.
        if "content_fingerprint" not in metadata:
            raise ArchiveIntegrityError("archive metadata has no recognized schema")
        return (
            ArchiveRecord(
                archive_id=archive_id,
                session_id=session_id,
                content_sha256=actual_digest,
                original_chars=len(raw),
                original_tokens=_metadata_tokens(metadata, raw),
                created_at=_metadata_created_at(metadata),
                schema_version="v1",
            ),
            raw,
        )

    def make_placeholder(
        self,
        part: MessagePart,
        record: ArchiveRecord,
        lifecycle: str = "derived",
        summary: str | None = None,
        key_errors: tuple[str, ...] = (),
    ) -> MessagePart:
        """Create the v2 projection, which never embeds source-output bytes."""

        self._validate_part(part)
        tool_name = _short(part.metadata.get("tool_name") or "tool", 64)
        status = _short(_tool_status(part), 32)
        safe_lifecycle = _short(lifecycle, 32)
        resolved_summary = _short(
            summary or _default_summary(part, original_tokens=record.original_tokens), 240
        )
        errors = tuple(_short(error, 72) for error in key_errors[:3] if str(error).strip())
        lines = [
            "[Tool result archived]",
            f"archive_id={record.archive_id}",
            f"tool={tool_name}",
            f"status={status}",
            f"lifecycle={safe_lifecycle}",
            f"original_tokens={record.original_tokens}",
            f"summary={resolved_summary}",
        ]
        lines.extend(f"key_errors={error}" for error in errors)
        lines.append("Use retrieve_archive(archive_id, ...) to inspect the original.")
        content = _fit_placeholder(lines, maximum=480)

        metadata: dict[str, Any] = dict(part.metadata)
        # A caller can hand us an old projection; do not accidentally retain
        # its preview in the v2 projection.
        metadata.pop("preview", None)
        metadata.pop("preview_tokens", None)
        metadata.update(
            {
                "archive_id": record.archive_id,
                "original_content_sha256": record.content_sha256,
                "original_tokens": record.original_tokens,
                "compaction_state": "archived",
                "compacted_by": "l3_archive",
            }
        )
        return MessagePart(
            id=part.id,
            message_id=part.message_id,
            kind=part.kind,
            content=content,
            metadata=metadata,
        )

    def archive_part(
        self,
        *,
        session_id: str,
        part: MessagePart,
        summary: str | None = None,
        archive_id: str | None = None,
    ) -> MessagePart:
        """Compatibility API retaining the legacy preview-bearing projection."""

        if part.metadata.get("compaction_state") == "archived" and part.metadata.get("archive_id"):
            return part
        self._validate_part(part)

        # New archives are always content-addressed.  The compatibility
        # parameter remains only so callers that already compute this id can
        # assert it; arbitrary legacy/random IDs are never written as v2.
        resolved_id = archive_id or part.metadata.get("archive_id")
        expected_id = _content_addressed_id(_sha256(part.content))
        if resolved_id is not None and resolved_id != expected_id:
            raise ValueError("new archive_id must equal the content-addressed ID")
        record = self.store_original(session_id, part)

        text_path, metadata_path = self._archive_paths(session_id, record.archive_id)
        preview = part.content[: self.preview_chars]
        preview_tokens = estimate_text_tokens(preview)
        resolved_summary = summary or _default_summary(part, original_tokens=record.original_tokens)
        metadata: dict[str, Any] = dict(part.metadata)
        metadata.update(
            {
                "archive_id": record.archive_id,
                "archive_path": str(text_path),
                "archive_metadata_path": str(metadata_path),
                "summary": resolved_summary,
                "preview": preview,
                "original_tokens": record.original_tokens,
                "preview_tokens": preview_tokens,
                "content_fingerprint": record.content_sha256,
                "compaction_state": "archived",
                "compacted_by": "archive",
                "compacted_at": record.created_at,
                "compaction_strategy_version": COMPACTION_STRATEGY_VERSION,
            }
        )
        return MessagePart(
            id=part.id,
            message_id=part.message_id,
            kind=part.kind,
            content=_legacy_placeholder_text(
                archive_id=record.archive_id,
                summary=resolved_summary,
                preview=preview,
                original_tokens=record.original_tokens,
                preview_tokens=preview_tokens,
            ),
            metadata=metadata,
        )

    def _store(
        self,
        *,
        session_id: str,
        archive_id: str,
        raw: str,
        content_sha256: str,
    ) -> ArchiveRecord:
        text_path, metadata_path = self._archive_paths(session_id, archive_id)
        text_path.parent.mkdir(parents=True, exist_ok=True)

        if text_path.exists():
            if _sha256(text_path.read_text(encoding="utf-8")) != content_sha256:
                raise ArchiveIntegrityError("existing archive text has a different SHA-256")
        else:
            _atomic_write(text_path, raw)

        record = ArchiveRecord(
            archive_id=archive_id,
            session_id=session_id,
            content_sha256=content_sha256,
            original_chars=len(raw),
            original_tokens=estimate_text_tokens(raw),
            created_at=utc_now_iso(),
        )
        expected_metadata = {
            "archive_id": record.archive_id,
            "content_sha256": record.content_sha256,
            "original_chars": record.original_chars,
            "original_tokens": record.original_tokens,
            "created_at": record.created_at,
            "schema_version": record.schema_version,
        }
        if metadata_path.exists():
            existing = _read_metadata(metadata_path)
            if existing.get("schema_version") == ARCHIVE_SCHEMA_VERSION:
                if (
                    existing.get("archive_id") != archive_id
                    or existing.get("content_sha256") != content_sha256
                    or existing.get("original_chars") != len(raw)
                ):
                    raise ArchiveIntegrityError("existing archive metadata disagrees with content")
                record = ArchiveRecord(
                    archive_id=archive_id,
                    session_id=session_id,
                    content_sha256=content_sha256,
                    original_chars=len(raw),
                    original_tokens=_metadata_tokens(existing, raw),
                    created_at=_metadata_created_at(existing),
                )
            else:
                raise ArchiveIntegrityError("existing archive metadata uses an incompatible schema")
        else:
            _atomic_write(metadata_path, json.dumps(expected_metadata, ensure_ascii=False, sort_keys=True))
        return record

    def _archive_paths(self, session_id: str, archive_id: str) -> tuple[Path, Path]:
        _validate_component(session_id, "session_id")
        _validate_component(archive_id, "archive_id")
        directory = Path(self.root) / "archives" / session_id
        return directory / f"{archive_id}.txt", directory / f"{archive_id}.json"

    @staticmethod
    def _validate_part(part: MessagePart) -> None:
        if part.kind != "tool_result":
            raise ValueError("ToolResultArchive only accepts tool_result parts")


def _validate_component(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_COMPONENT.fullmatch(value) is None:
        raise ValueError(f"{name} must contain only letters, digits, underscores, or hyphens")


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _content_addressed_id(content_sha256: str) -> str:
    return f"ar_{content_sha256[:32]}"


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace *path* with UTF-8 text in the same directory."""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveIntegrityError("archive metadata cannot be read") from exc
    if not isinstance(value, dict):
        raise ArchiveIntegrityError("archive metadata must be an object")
    return value


def _metadata_tokens(metadata: dict[str, Any], raw: str) -> int:
    tokens = metadata.get("original_tokens")
    return tokens if isinstance(tokens, int) and tokens >= 0 else estimate_text_tokens(raw)


def _metadata_created_at(metadata: dict[str, Any]) -> str:
    created_at = metadata.get("created_at")
    return created_at if isinstance(created_at, str) else ""


def _short(value: object, maximum: int) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:maximum]


def _tool_status(part: MessagePart) -> str:
    if part.metadata.get("ok") is False:
        return "failed"
    status = str(part.metadata.get("status") or "").strip().lower()
    if status in {"failed", "failure", "error", "errored"} or part.metadata.get("is_error"):
        return "failed"
    return "success"


def _fit_placeholder(lines: list[str], *, maximum: int) -> str:
    content = "\n".join(lines)
    if len(content) <= maximum:
        return content
    # The summary is the only intentionally variable, user-facing field.  Trim
    # it first, then drop optional error lines if unusual metadata still fills
    # the envelope.  The retrieval instruction always remains present.
    required = [line for line in lines if not line.startswith(("summary=", "key_errors="))]
    optional = [line for line in lines if line.startswith("summary=")]
    candidate = "\n".join(required[:6] + optional + required[6:])
    if len(candidate) > maximum:
        excess = len(candidate) - maximum
        summary = optional[0][8:]
        optional = [f"summary={summary[: max(0, len(summary) - excess)]}"]
        candidate = "\n".join(required[:6] + optional + required[6:])
    return candidate[:maximum]


def _default_summary(part: MessagePart, *, original_tokens: int) -> str:
    tool_name = str(part.metadata.get("tool_name") or "tool")
    return f"{tool_name} 输出过大，已归档。原始估算 {original_tokens} tokens。"


def _legacy_placeholder_text(
    *,
    archive_id: str,
    summary: str,
    preview: str,
    original_tokens: int,
    preview_tokens: int,
) -> str:
    return "\n".join(
        [
            "[Tool result archived]",
            f"archive_id={archive_id}",
            f"summary={summary}",
            f"original_tokens={original_tokens}",
            f"preview_tokens={preview_tokens}",
            f"preview={preview}",
        ]
    )
