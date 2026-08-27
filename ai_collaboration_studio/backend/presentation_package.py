from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import re
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from xml.etree import ElementTree


PPTX_INGEST_PACKAGE_VERSION = "pptx_ingest_package_v1"
ARTIFACT_RENDER_PACKAGE_VERSION = "artifact_render_package_v1"
ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION = (
    "artifact_render_verification_receipt_v1"
)
PPTX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)

MAX_PPTX_SOURCE_BYTES = 2_000_000
MAX_PPTX_UNCOMPRESSED_BYTES = 20_000_000
MAX_PPTX_ARCHIVE_MEMBERS = 2_048
MAX_PPTX_SLIDES = 120
MAX_PPTX_XML_PART_BYTES = 2_000_000
MAX_PPTX_EXTRACTED_CHARS = 100_000

_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_FORBIDDEN_MEMBER_PREFIXES = (
    "ppt/activex/",
    "ppt/ctrlprops/",
    "ppt/embeddings/",
)
_FORBIDDEN_MEMBER_SUFFIXES = (
    ".bin",
    ".com",
    ".dll",
    ".exe",
    ".js",
    ".msi",
    ".ps1",
    ".vbs",
)
_FORBIDDEN_CONTENT_TYPE_TOKENS = (
    "activex",
    "embeddedpackage",
    "macroenabled",
    "oleobject",
    "vbaproject",
)
_FORBIDDEN_RELATIONSHIP_TYPE_TOKENS = (
    "/activex",
    "/control",
    "/oleobject",
    "/package",
    "/vbaproject",
)
_AUTOMATIC_RENDER_CHECKS = (
    "all_slides_rendered",
    "no_unintended_overflow",
    "no_unintended_overlap",
    "render_succeeded",
    "slide_count_matches",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class PresentationPackageError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


def _error(message: str, code: str) -> PresentationPackageError:
    return PresentationPackageError(message, code=code)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _error(
            "Presentation package contains a non-canonical JSON value.",
            "PRESENTATION_PACKAGE_INVALID",
        ) from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _require_closed_mapping(
    value: Any,
    expected_fields: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise _error(f"{label} must be an object.", "PRESENTATION_PACKAGE_INVALID")
    if set(value) != expected_fields:
        raise _error(
            f"{label} fields are not closed.",
            "PRESENTATION_PACKAGE_INVALID",
        )
    return value


def _require_text(value: Any, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise _error(f"{label} must be text.", "PRESENTATION_PACKAGE_INVALID")
    if len(value) > maximum or (not allow_empty and not value):
        raise _error(f"{label} is invalid.", "PRESENTATION_PACKAGE_INVALID")
    return value


def _require_identifier(value: Any, *, label: str) -> str:
    text = _require_text(value, label=label, maximum=160)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise _error(f"{label} is invalid.", "PRESENTATION_PACKAGE_INVALID")
    return text


def _require_sha256(value: Any, *, label: str) -> str:
    text = _require_text(value, label=label, maximum=64)
    if not _SHA256_RE.fullmatch(text):
        raise _error(f"{label} must be a lowercase SHA-256 digest.", "PRESENTATION_PACKAGE_INVALID")
    return text


def _require_positive_int(value: Any, *, label: str, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise _error(f"{label} must be an integer in range.", "PRESENTATION_PACKAGE_INVALID")
    return value


def _safe_filename(value: Any, *, extension: str = ".pptx") -> str:
    text = _require_text(value, label="filename", maximum=180)
    safe = Path(text).name
    if safe != text or not safe.lower().endswith(extension):
        raise _error("The presentation filename is invalid.", "PPTX_FILENAME_INVALID")
    return safe


def _safe_xml_root(raw: bytes, *, part_name: str) -> ElementTree.Element:
    if len(raw) > MAX_PPTX_XML_PART_BYTES:
        raise _error(
            f"PPTX XML part is too large: {part_name}",
            "PPTX_XML_PART_TOO_LARGE",
        )
    lowered = raw.lower().replace(b"\x00", b"")
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise _error(
            f"PPTX XML declarations are not allowed: {part_name}",
            "PPTX_XML_ACTIVE_CONTENT_REJECTED",
        )
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise _error(
            f"PPTX XML is invalid: {part_name}",
            "PPTX_XML_INVALID",
        ) from exc


def _normalized_archive_name(value: str) -> str:
    if not value or "\\" in value or value.startswith("/"):
        raise _error("PPTX archive member path is invalid.", "PPTX_ARCHIVE_PATH_INVALID")
    path = PurePosixPath(value.rstrip("/"))
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise _error("PPTX archive member path is invalid.", "PPTX_ARCHIVE_PATH_INVALID")
    normalized = path.as_posix()
    if normalized != value.rstrip("/"):
        raise _error("PPTX archive member path is ambiguous.", "PPTX_ARCHIVE_PATH_INVALID")
    return normalized


def _inspect_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_PPTX_ARCHIVE_MEMBERS:
        raise _error("PPTX contains too many archive members.", "PPTX_MEMBER_LIMIT_EXCEEDED")
    members: dict[str, zipfile.ZipInfo] = {}
    casefold_names: set[str] = set()
    total_uncompressed = 0
    for info in infos:
        name = _normalized_archive_name(info.filename)
        folded = name.casefold()
        if folded in casefold_names:
            raise _error("PPTX contains duplicate member identities.", "PPTX_ARCHIVE_AMBIGUOUS")
        casefold_names.add(folded)
        if info.flag_bits & 0x1:
            raise _error("Encrypted PPTX members are not supported.", "PPTX_ENCRYPTED_REJECTED")
        unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
        if unix_mode and (unix_mode & 0o170000) == 0o120000:
            raise _error("PPTX symbolic-link members are not allowed.", "PPTX_ARCHIVE_PATH_INVALID")
        if info.is_dir():
            continue
        total_uncompressed += int(info.file_size)
        if total_uncompressed > MAX_PPTX_UNCOMPRESSED_BYTES:
            raise _error(
                "PPTX uncompressed content exceeds the safety limit.",
                "PPTX_UNCOMPRESSED_LIMIT_EXCEEDED",
            )
        lowered = name.lower()
        if lowered.startswith(_FORBIDDEN_MEMBER_PREFIXES) or lowered.endswith(
            _FORBIDDEN_MEMBER_SUFFIXES
        ):
            raise _error(
                "PPTX macros, OLE, ActiveX, and embedded objects are not allowed.",
                "PPTX_ACTIVE_CONTENT_REJECTED",
            )
        members[name] = info
    return members


def _read_part(
    archive: zipfile.ZipFile,
    members: Mapping[str, zipfile.ZipInfo],
    name: str,
    *,
    required: bool = True,
) -> bytes:
    info = members.get(name)
    if info is None:
        if required:
            raise _error(f"PPTX part is missing: {name}", "PPTX_STRUCTURE_INVALID")
        return b""
    if int(info.file_size) > MAX_PPTX_XML_PART_BYTES and name.lower().endswith(
        (".xml", ".rels")
    ):
        raise _error(f"PPTX XML part is too large: {name}", "PPTX_XML_PART_TOO_LARGE")
    try:
        return archive.read(info)
    except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise _error(f"PPTX part cannot be read: {name}", "PPTX_ARCHIVE_INVALID") from exc


def _relationship_rows(root: ElementTree.Element) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for node in root.findall(f"{{{_PACKAGE_RELATIONSHIP_NS}}}Relationship"):
        relationship_id = str(node.attrib.get("Id") or "")
        relationship_type = str(node.attrib.get("Type") or "")
        target = str(node.attrib.get("Target") or "")
        target_mode = str(node.attrib.get("TargetMode") or "")
        if not relationship_id or relationship_id in seen_ids or not relationship_type or not target:
            raise _error("PPTX relationship entry is invalid.", "PPTX_RELATIONSHIP_INVALID")
        seen_ids.add(relationship_id)
        lowered_type = relationship_type.lower()
        if any(token in lowered_type for token in _FORBIDDEN_RELATIONSHIP_TYPE_TOKENS):
            raise _error(
                "PPTX active or embedded relationship is not allowed.",
                "PPTX_ACTIVE_CONTENT_REJECTED",
            )
        rows.append(
            {
                "id": relationship_id,
                "type": relationship_type,
                "target": target,
                "target_mode": target_mode,
            }
        )
    return rows


def _resolve_internal_target(source_part: str, target: str) -> str:
    if not target or "\\" in target or target.startswith("/"):
        raise _error("PPTX relationship target is invalid.", "PPTX_RELATIONSHIP_INVALID")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise _error("PPTX relationship target escapes the package.", "PPTX_RELATIONSHIP_INVALID")
    return _normalized_archive_name(resolved)


def _relationship_part_name(source_part: str) -> str:
    directory = posixpath.dirname(source_part)
    filename = posixpath.basename(source_part)
    return posixpath.join(directory, "_rels", f"{filename}.rels")


def _extract_xml_text(root: ElementTree.Element) -> str:
    paragraphs: list[str] = []
    paragraph_tag = f"{{{_DRAWING_NS}}}p"
    text_tag = f"{{{_DRAWING_NS}}}t"
    for paragraph in root.iter(paragraph_tag):
        parts = [node.text or "" for node in paragraph.iter(text_tag)]
        text = "".join(parts).replace("\x00", "").strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs).strip()


def _content_types_are_safe(root: ElementTree.Element) -> None:
    presentation_declared = False
    for node in root:
        if node.tag not in {
            f"{{{_CONTENT_TYPES_NS}}}Default",
            f"{{{_CONTENT_TYPES_NS}}}Override",
        }:
            continue
        content_type = str(node.attrib.get("ContentType") or "").lower()
        if any(token in content_type for token in _FORBIDDEN_CONTENT_TYPE_TOKENS):
            raise _error(
                "PPTX declares macros, OLE, ActiveX, or embedded content.",
                "PPTX_ACTIVE_CONTENT_REJECTED",
            )
        if (
            str(node.attrib.get("PartName") or "") == "/ppt/presentation.xml"
            and content_type == PPTX_CONTENT_TYPE
        ):
            presentation_declared = True
    if not presentation_declared:
        raise _error(
            "PPTX content types do not declare a standard presentation.",
            "PPTX_STRUCTURE_INVALID",
        )


def _external_relationship_manifest(
    archive: zipfile.ZipFile,
    members: Mapping[str, zipfile.ZipInfo],
) -> tuple[int, str]:
    targets: list[str] = []
    for name in sorted(members):
        if not name.lower().endswith(".rels"):
            continue
        root = _safe_xml_root(_read_part(archive, members, name), part_name=name)
        for row in _relationship_rows(root):
            if row["target_mode"].lower() == "external":
                targets.append(row["target"])
    targets.sort()
    return len(targets), _sha256_value(targets)


def _ordered_slide_parts(
    archive: zipfile.ZipFile,
    members: Mapping[str, zipfile.ZipInfo],
) -> list[str]:
    presentation_name = "ppt/presentation.xml"
    relationships_name = "ppt/_rels/presentation.xml.rels"
    presentation_root = _safe_xml_root(
        _read_part(archive, members, presentation_name),
        part_name=presentation_name,
    )
    relationships_root = _safe_xml_root(
        _read_part(archive, members, relationships_name),
        part_name=relationships_name,
    )
    relationships = {row["id"]: row for row in _relationship_rows(relationships_root)}
    slide_parts: list[str] = []
    seen_parts: set[str] = set()
    slide_id_tag = f"{{{_PRESENTATION_NS}}}sldId"
    relationship_id_key = f"{{{_RELATIONSHIP_NS}}}id"
    for slide_node in presentation_root.iter(slide_id_tag):
        relationship_id = str(slide_node.attrib.get(relationship_id_key) or "")
        relationship = relationships.get(relationship_id)
        if (
            relationship is None
            or relationship["target_mode"].lower() == "external"
            or not relationship["type"].lower().endswith("/slide")
        ):
            raise _error("PPTX slide relationship is invalid.", "PPTX_STRUCTURE_INVALID")
        part_name = _resolve_internal_target(presentation_name, relationship["target"])
        if (
            not part_name.lower().startswith("ppt/slides/")
            or not part_name.lower().endswith(".xml")
            or part_name not in members
            or part_name in seen_parts
        ):
            raise _error("PPTX slide identity is invalid.", "PPTX_STRUCTURE_INVALID")
        seen_parts.add(part_name)
        slide_parts.append(part_name)
    if not slide_parts:
        raise _error("PPTX contains no slides.", "PPTX_STRUCTURE_INVALID")
    if len(slide_parts) > MAX_PPTX_SLIDES:
        raise _error("PPTX slide count exceeds the safety limit.", "PPTX_SLIDE_LIMIT_EXCEEDED")
    return slide_parts


def _notes_text_for_slide(
    archive: zipfile.ZipFile,
    members: Mapping[str, zipfile.ZipInfo],
    slide_part: str,
) -> str:
    relationships_name = _relationship_part_name(slide_part)
    if relationships_name not in members:
        return ""
    relationships_root = _safe_xml_root(
        _read_part(archive, members, relationships_name),
        part_name=relationships_name,
    )
    notes_targets = [
        row
        for row in _relationship_rows(relationships_root)
        if row["type"].lower().endswith("/notesslide")
    ]
    if len(notes_targets) > 1:
        raise _error("PPTX slide has ambiguous notes.", "PPTX_STRUCTURE_INVALID")
    if not notes_targets:
        return ""
    relationship = notes_targets[0]
    if relationship["target_mode"].lower() == "external":
        raise _error("External speaker notes are not allowed.", "PPTX_STRUCTURE_INVALID")
    notes_part = _resolve_internal_target(slide_part, relationship["target"])
    if (
        not notes_part.lower().startswith("ppt/notesslides/")
        or not notes_part.lower().endswith(".xml")
    ):
        raise _error("PPTX notes relationship is invalid.", "PPTX_STRUCTURE_INVALID")
    notes_root = _safe_xml_root(
        _read_part(archive, members, notes_part),
        part_name=notes_part,
    )
    return _extract_xml_text(notes_root)


def build_pptx_ingest_package(
    source_bytes: bytes,
    *,
    filename: str,
) -> dict[str, Any]:
    if type(source_bytes) is not bytes or not source_bytes:
        raise _error("PPTX source must be non-empty bytes.", "PPTX_SOURCE_INVALID")
    if len(source_bytes) > MAX_PPTX_SOURCE_BYTES:
        raise _error("PPTX source exceeds the safety limit.", "PPTX_SOURCE_LIMIT_EXCEEDED")
    safe_filename = _safe_filename(filename)
    try:
        archive = zipfile.ZipFile(BytesIO(source_bytes))
    except zipfile.BadZipFile as exc:
        raise _error("PPTX ZIP structure is invalid.", "PPTX_ARCHIVE_INVALID") from exc
    with archive:
        members = _inspect_archive(archive)
        content_types_name = "[Content_Types].xml"
        content_types_root = _safe_xml_root(
            _read_part(archive, members, content_types_name),
            part_name=content_types_name,
        )
        _content_types_are_safe(content_types_root)
        _read_part(archive, members, "_rels/.rels")
        slide_parts = _ordered_slide_parts(archive, members)
        external_count, external_sha256 = _external_relationship_manifest(
            archive,
            members,
        )
        slides: list[dict[str, Any]] = []
        combined_parts: list[str] = []
        for slide_number, slide_part in enumerate(slide_parts, start=1):
            slide_root = _safe_xml_root(
                _read_part(archive, members, slide_part),
                part_name=slide_part,
            )
            text = _extract_xml_text(slide_root)
            notes_text = _notes_text_for_slide(archive, members, slide_part)
            slide = {
                "slide_number": slide_number,
                "part_name": slide_part,
                "text": text,
                "text_sha256": _sha256_bytes(text.encode("utf-8")),
                "notes_text": notes_text,
                "notes_text_sha256": _sha256_bytes(notes_text.encode("utf-8")),
            }
            slides.append(slide)
            section = f"# Slide {slide_number}\n{text}".rstrip()
            if notes_text:
                section += f"\n\nSpeaker notes:\n{notes_text}"
            combined_parts.append(section)
        full_text = "\n\n".join(combined_parts).strip()
        text_truncated = len(full_text) > MAX_PPTX_EXTRACTED_CHARS
        extracted_text = full_text[:MAX_PPTX_EXTRACTED_CHARS].rstrip()

    package: dict[str, Any] = {
        "schema_version": PPTX_INGEST_PACKAGE_VERSION,
        "source": {
            "filename": safe_filename,
            "content_type": PPTX_CONTENT_TYPE,
            "source_bytes": len(source_bytes),
            "source_sha256": _sha256_bytes(source_bytes),
        },
        "limits": {
            "max_source_bytes": MAX_PPTX_SOURCE_BYTES,
            "max_uncompressed_bytes": MAX_PPTX_UNCOMPRESSED_BYTES,
            "max_archive_members": MAX_PPTX_ARCHIVE_MEMBERS,
            "max_slides": MAX_PPTX_SLIDES,
            "max_xml_part_bytes": MAX_PPTX_XML_PART_BYTES,
            "max_extracted_chars": MAX_PPTX_EXTRACTED_CHARS,
        },
        "presentation": {
            "slide_count": len(slides),
            "slides": slides,
            "external_relationship_count": external_count,
            "external_relationship_targets_sha256": external_sha256,
        },
        "extracted_text": extracted_text,
        "extracted_text_sha256": _sha256_bytes(extracted_text.encode("utf-8")),
        "text_truncated": text_truncated,
        "safety": {
            "macros_allowed": False,
            "ole_objects_allowed": False,
            "embedded_objects_allowed": False,
            "external_relationships_fetched": False,
            "xml_external_entities_allowed": False,
        },
    }
    package["package_sha256"] = _sha256_value(package)
    return verify_pptx_ingest_package(package)


def verify_pptx_ingest_package(
    value: Any,
    *,
    source_bytes: bytes | None = None,
) -> dict[str, Any]:
    package = _require_closed_mapping(
        value,
        {
            "schema_version",
            "source",
            "limits",
            "presentation",
            "extracted_text",
            "extracted_text_sha256",
            "text_truncated",
            "safety",
            "package_sha256",
        },
        label="PPTX ingest package",
    )
    if package["schema_version"] != PPTX_INGEST_PACKAGE_VERSION:
        raise _error("PPTX ingest package version is unsupported.", "PPTX_PACKAGE_UNSUPPORTED")
    source = _require_closed_mapping(
        package["source"],
        {"filename", "content_type", "source_bytes", "source_sha256"},
        label="PPTX source",
    )
    filename = _safe_filename(source["filename"])
    if source["content_type"] != PPTX_CONTENT_TYPE:
        raise _error("PPTX source content type is invalid.", "PRESENTATION_PACKAGE_INVALID")
    source_size = _require_positive_int(source["source_bytes"], label="source_bytes")
    _require_sha256(source["source_sha256"], label="source_sha256")
    limits = _require_closed_mapping(
        package["limits"],
        {
            "max_source_bytes",
            "max_uncompressed_bytes",
            "max_archive_members",
            "max_slides",
            "max_xml_part_bytes",
            "max_extracted_chars",
        },
        label="PPTX limits",
    )
    expected_limits = {
        "max_source_bytes": MAX_PPTX_SOURCE_BYTES,
        "max_uncompressed_bytes": MAX_PPTX_UNCOMPRESSED_BYTES,
        "max_archive_members": MAX_PPTX_ARCHIVE_MEMBERS,
        "max_slides": MAX_PPTX_SLIDES,
        "max_xml_part_bytes": MAX_PPTX_XML_PART_BYTES,
        "max_extracted_chars": MAX_PPTX_EXTRACTED_CHARS,
    }
    if limits != expected_limits or source_size > MAX_PPTX_SOURCE_BYTES:
        raise _error("PPTX safety limits do not match v1.", "PRESENTATION_PACKAGE_INVALID")
    presentation = _require_closed_mapping(
        package["presentation"],
        {
            "slide_count",
            "slides",
            "external_relationship_count",
            "external_relationship_targets_sha256",
        },
        label="PPTX presentation",
    )
    slide_count = _require_positive_int(presentation["slide_count"], label="slide_count")
    if slide_count > MAX_PPTX_SLIDES or type(presentation["slides"]) is not list:
        raise _error("PPTX slide manifest is invalid.", "PRESENTATION_PACKAGE_INVALID")
    if len(presentation["slides"]) != slide_count:
        raise _error("PPTX slide count does not match its manifest.", "PRESENTATION_PACKAGE_INVALID")
    combined_parts: list[str] = []
    for index, raw_slide in enumerate(presentation["slides"], start=1):
        slide = _require_closed_mapping(
            raw_slide,
            {
                "slide_number",
                "part_name",
                "text",
                "text_sha256",
                "notes_text",
                "notes_text_sha256",
            },
            label="PPTX slide",
        )
        if slide["slide_number"] != index:
            raise _error("PPTX slide order is invalid.", "PRESENTATION_PACKAGE_INVALID")
        part_name = _require_text(slide["part_name"], label="slide part", maximum=240)
        if not part_name.startswith("ppt/slides/") or not part_name.endswith(".xml"):
            raise _error("PPTX slide part is invalid.", "PRESENTATION_PACKAGE_INVALID")
        text = _require_text(slide["text"], label="slide text", maximum=MAX_PPTX_EXTRACTED_CHARS, allow_empty=True)
        notes = _require_text(slide["notes_text"], label="slide notes", maximum=MAX_PPTX_EXTRACTED_CHARS, allow_empty=True)
        if _sha256_bytes(text.encode("utf-8")) != _require_sha256(
            slide["text_sha256"], label="slide text digest"
        ):
            raise _error("PPTX slide text digest is invalid.", "PPTX_PACKAGE_INTEGRITY_FAILED")
        if _sha256_bytes(notes.encode("utf-8")) != _require_sha256(
            slide["notes_text_sha256"], label="slide notes digest"
        ):
            raise _error("PPTX slide notes digest is invalid.", "PPTX_PACKAGE_INTEGRITY_FAILED")
        section = f"# Slide {index}\n{text}".rstrip()
        if notes:
            section += f"\n\nSpeaker notes:\n{notes}"
        combined_parts.append(section)
    _require_positive_int(
        presentation["external_relationship_count"],
        label="external relationship count",
        allow_zero=True,
    )
    _require_sha256(
        presentation["external_relationship_targets_sha256"],
        label="external relationship digest",
    )
    extracted_text = _require_text(
        package["extracted_text"],
        label="PPTX extracted text",
        maximum=MAX_PPTX_EXTRACTED_CHARS,
        allow_empty=True,
    )
    if _sha256_bytes(extracted_text.encode("utf-8")) != _require_sha256(
        package["extracted_text_sha256"], label="extracted text digest"
    ):
        raise _error("PPTX extracted text digest is invalid.", "PPTX_PACKAGE_INTEGRITY_FAILED")
    if type(package["text_truncated"]) is not bool:
        raise _error("PPTX truncation flag is invalid.", "PRESENTATION_PACKAGE_INVALID")
    full_text = "\n\n".join(combined_parts).strip()
    expected_truncated = len(full_text) > MAX_PPTX_EXTRACTED_CHARS
    expected_extracted_text = full_text[:MAX_PPTX_EXTRACTED_CHARS].rstrip()
    if (
        extracted_text != expected_extracted_text
        or package["text_truncated"] is not expected_truncated
    ):
        raise _error(
            "PPTX extracted text does not match the slide manifest.",
            "PPTX_PACKAGE_INTEGRITY_FAILED",
        )
    safety = _require_closed_mapping(
        package["safety"],
        {
            "macros_allowed",
            "ole_objects_allowed",
            "embedded_objects_allowed",
            "external_relationships_fetched",
            "xml_external_entities_allowed",
        },
        label="PPTX safety",
    )
    if any(type(item) is not bool for item in safety.values()) or any(safety.values()):
        raise _error("PPTX safety boundary is invalid.", "PRESENTATION_PACKAGE_INVALID")
    supplied_package_sha256 = _require_sha256(package["package_sha256"], label="package digest")
    unsigned = {key: copy.deepcopy(item) for key, item in package.items() if key != "package_sha256"}
    if _sha256_value(unsigned) != supplied_package_sha256:
        raise _error("PPTX package digest is invalid.", "PPTX_PACKAGE_INTEGRITY_FAILED")
    if source_bytes is not None:
        if type(source_bytes) is not bytes:
            raise _error("PPTX source verification requires bytes.", "PPTX_SOURCE_INVALID")
        if len(source_bytes) != source_size or _sha256_bytes(source_bytes) != source["source_sha256"]:
            raise _error("PPTX package is bound to different source bytes.", "PPTX_SOURCE_BINDING_MISMATCH")
        rebuilt = build_pptx_ingest_package(source_bytes, filename=filename)
        if rebuilt != package:
            raise _error("PPTX package does not match the source bytes.", "PPTX_SOURCE_BINDING_MISMATCH")
    return copy.deepcopy(package)


def build_artifact_render_package(
    *,
    artifact_id: str,
    artifact_version: int,
    artifact_snapshot_sha256: str,
    output_filename: str,
    output_bytes: bytes,
    renderer_id: str,
    renderer_version: str,
    rendered_slide_sha256s: list[str],
) -> dict[str, Any]:
    clean_artifact_id = _require_identifier(artifact_id, label="artifact_id")
    clean_artifact_version = _require_positive_int(artifact_version, label="artifact_version")
    clean_artifact_sha256 = _require_sha256(
        artifact_snapshot_sha256,
        label="artifact_snapshot_sha256",
    )
    clean_filename = _safe_filename(output_filename)
    clean_renderer_id = _require_identifier(renderer_id, label="renderer_id")
    clean_renderer_version = _require_identifier(renderer_version, label="renderer_version")
    if type(rendered_slide_sha256s) is not list:
        raise _error("Rendered slide digests must be a list.", "ARTIFACT_RENDER_PACKAGE_INVALID")
    pptx_package = build_pptx_ingest_package(output_bytes, filename=clean_filename)
    slide_count = int(pptx_package["presentation"]["slide_count"])
    if len(rendered_slide_sha256s) != slide_count:
        raise _error(
            "Rendered slide count does not match the PPTX.",
            "ARTIFACT_RENDER_SLIDE_COUNT_MISMATCH",
        )
    rendered_slides = []
    for index, digest in enumerate(rendered_slide_sha256s, start=1):
        rendered_slides.append(
            {
                "slide_number": index,
                "image_sha256": _require_sha256(
                    digest,
                    label="rendered slide digest",
                ),
            }
        )
    render_manifest_sha256 = _sha256_value(rendered_slides)
    package: dict[str, Any] = {
        "schema_version": ARTIFACT_RENDER_PACKAGE_VERSION,
        "artifact_binding": {
            "artifact_id": clean_artifact_id,
            "artifact_version": clean_artifact_version,
            "artifact_snapshot_sha256": clean_artifact_sha256,
        },
        "renderer": {
            "renderer_id": clean_renderer_id,
            "renderer_version": clean_renderer_version,
        },
        "output": {
            "filename": clean_filename,
            "content_type": PPTX_CONTENT_TYPE,
            "output_bytes": len(output_bytes),
            "output_sha256": _sha256_bytes(output_bytes),
            "pptx_ingest_package_sha256": pptx_package["package_sha256"],
            "slide_count": slide_count,
        },
        "rendered_slides": rendered_slides,
        "render_manifest_sha256": render_manifest_sha256,
        "safety": {
            "execution_capability": "none",
            "output_path_must_be_user_selected": True,
            "automatic_checks_are_user_acceptance": False,
        },
    }
    package["package_sha256"] = _sha256_value(package)
    return verify_artifact_render_package(package)


def verify_artifact_render_package(
    value: Any,
    *,
    output_bytes: bytes | None = None,
) -> dict[str, Any]:
    package = _require_closed_mapping(
        value,
        {
            "schema_version",
            "artifact_binding",
            "renderer",
            "output",
            "rendered_slides",
            "render_manifest_sha256",
            "safety",
            "package_sha256",
        },
        label="artifact render package",
    )
    if package["schema_version"] != ARTIFACT_RENDER_PACKAGE_VERSION:
        raise _error("Artifact render package version is unsupported.", "ARTIFACT_RENDER_PACKAGE_UNSUPPORTED")
    artifact = _require_closed_mapping(
        package["artifact_binding"],
        {"artifact_id", "artifact_version", "artifact_snapshot_sha256"},
        label="artifact binding",
    )
    _require_identifier(artifact["artifact_id"], label="artifact_id")
    _require_positive_int(artifact["artifact_version"], label="artifact_version")
    _require_sha256(artifact["artifact_snapshot_sha256"], label="artifact snapshot digest")
    renderer = _require_closed_mapping(
        package["renderer"],
        {"renderer_id", "renderer_version"},
        label="renderer",
    )
    _require_identifier(renderer["renderer_id"], label="renderer_id")
    _require_identifier(renderer["renderer_version"], label="renderer_version")
    output = _require_closed_mapping(
        package["output"],
        {
            "filename",
            "content_type",
            "output_bytes",
            "output_sha256",
            "pptx_ingest_package_sha256",
            "slide_count",
        },
        label="render output",
    )
    filename = _safe_filename(output["filename"])
    if output["content_type"] != PPTX_CONTENT_TYPE:
        raise _error("Render output content type is invalid.", "ARTIFACT_RENDER_PACKAGE_INVALID")
    output_size = _require_positive_int(output["output_bytes"], label="output_bytes")
    output_sha256 = _require_sha256(output["output_sha256"], label="output digest")
    _require_sha256(output["pptx_ingest_package_sha256"], label="PPTX package digest")
    slide_count = _require_positive_int(output["slide_count"], label="slide_count")
    if type(package["rendered_slides"]) is not list or len(package["rendered_slides"]) != slide_count:
        raise _error("Rendered slide manifest is invalid.", "ARTIFACT_RENDER_PACKAGE_INVALID")
    for index, raw_slide in enumerate(package["rendered_slides"], start=1):
        slide = _require_closed_mapping(
            raw_slide,
            {"slide_number", "image_sha256"},
            label="rendered slide",
        )
        if slide["slide_number"] != index:
            raise _error("Rendered slide order is invalid.", "ARTIFACT_RENDER_PACKAGE_INVALID")
        _require_sha256(slide["image_sha256"], label="rendered image digest")
    if _sha256_value(package["rendered_slides"]) != _require_sha256(
        package["render_manifest_sha256"], label="render manifest digest"
    ):
        raise _error("Render manifest digest is invalid.", "ARTIFACT_RENDER_PACKAGE_INTEGRITY_FAILED")
    safety = _require_closed_mapping(
        package["safety"],
        {
            "execution_capability",
            "output_path_must_be_user_selected",
            "automatic_checks_are_user_acceptance",
        },
        label="render safety",
    )
    if safety != {
        "execution_capability": "none",
        "output_path_must_be_user_selected": True,
        "automatic_checks_are_user_acceptance": False,
    }:
        raise _error("Render safety boundary is invalid.", "ARTIFACT_RENDER_PACKAGE_INVALID")
    supplied_sha256 = _require_sha256(package["package_sha256"], label="render package digest")
    unsigned = {key: copy.deepcopy(item) for key, item in package.items() if key != "package_sha256"}
    if _sha256_value(unsigned) != supplied_sha256:
        raise _error("Render package digest is invalid.", "ARTIFACT_RENDER_PACKAGE_INTEGRITY_FAILED")
    if output_bytes is not None:
        if type(output_bytes) is not bytes:
            raise _error("Render output verification requires bytes.", "ARTIFACT_RENDER_PACKAGE_INVALID")
        if len(output_bytes) != output_size or _sha256_bytes(output_bytes) != output_sha256:
            raise _error("Render package is bound to different output bytes.", "ARTIFACT_RENDER_OUTPUT_MISMATCH")
        pptx = build_pptx_ingest_package(output_bytes, filename=filename)
        if (
            pptx["package_sha256"] != output["pptx_ingest_package_sha256"]
            or pptx["presentation"]["slide_count"] != slide_count
        ):
            raise _error("Render package PPTX binding is invalid.", "ARTIFACT_RENDER_OUTPUT_MISMATCH")
    return copy.deepcopy(package)


def _normalize_automatic_checks(value: Any) -> dict[str, bool]:
    checks = _require_closed_mapping(
        value,
        set(_AUTOMATIC_RENDER_CHECKS),
        label="automatic render checks",
    )
    if any(type(checks[name]) is not bool for name in _AUTOMATIC_RENDER_CHECKS):
        raise _error("Automatic render check values must be booleans.", "ARTIFACT_RENDER_RECEIPT_INVALID")
    return {name: checks[name] for name in _AUTOMATIC_RENDER_CHECKS}


def _normalize_user_review(value: Any) -> dict[str, Any]:
    review = _require_closed_mapping(
        value,
        {"status", "reviewed_by", "reviewed_at", "notes"},
        label="user review",
    )
    status = _require_text(review["status"], label="user review status", maximum=16)
    if status not in {"pending", "accepted", "rejected"}:
        raise _error("User review status is invalid.", "ARTIFACT_RENDER_RECEIPT_INVALID")
    reviewed_by = _require_text(
        review["reviewed_by"],
        label="reviewed_by",
        maximum=32,
        allow_empty=True,
    )
    reviewed_at = _require_positive_int(
        review["reviewed_at"],
        label="reviewed_at",
        allow_zero=True,
    )
    notes = _require_text(review["notes"], label="review notes", maximum=2000, allow_empty=True)
    if status == "pending":
        if reviewed_by or reviewed_at != 0:
            raise _error("Pending review cannot claim user acceptance.", "ARTIFACT_RENDER_RECEIPT_INVALID")
    elif reviewed_by != "user" or reviewed_at <= 0:
        raise _error("Completed review requires an explicit user and timestamp.", "ARTIFACT_RENDER_RECEIPT_INVALID")
    return {
        "status": status,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "notes": notes,
    }


def _receipt_outcome(
    checks: Mapping[str, bool],
    user_review: Mapping[str, Any],
) -> tuple[str, str, list[str]]:
    failed_checks = [name for name in _AUTOMATIC_RENDER_CHECKS if not checks[name]]
    automatic_status = "passed" if not failed_checks else "failed"
    issues = [f"AUTOMATIC_CHECK_FAILED:{name}" for name in failed_checks]
    if failed_checks:
        return "failed", automatic_status, issues
    if user_review["status"] == "accepted":
        return "verified", automatic_status, []
    if user_review["status"] == "rejected":
        return "failed", automatic_status, ["USER_REVIEW_REJECTED"]
    return "needs_user_review", automatic_status, ["USER_REVIEW_REQUIRED"]


def build_artifact_render_verification_receipt(
    render_package: Any,
    *,
    automatic_checks: dict[str, bool],
    user_review: dict[str, Any],
) -> dict[str, Any]:
    verified_package = verify_artifact_render_package(render_package)
    checks = _normalize_automatic_checks(automatic_checks)
    review = _normalize_user_review(user_review)
    status, automatic_status, issues = _receipt_outcome(checks, review)
    receipt: dict[str, Any] = {
        "schema_version": ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION,
        "render_package_sha256": verified_package["package_sha256"],
        "artifact_snapshot_sha256": verified_package["artifact_binding"][
            "artifact_snapshot_sha256"
        ],
        "output_sha256": verified_package["output"]["output_sha256"],
        "automatic_checks": checks,
        "automatic_status": automatic_status,
        "user_review": review,
        "status": status,
        "issues": issues,
    }
    receipt["receipt_sha256"] = _sha256_value(receipt)
    return verify_artifact_render_verification_receipt(
        receipt,
        render_package=verified_package,
    )


def verify_artifact_render_verification_receipt(
    value: Any,
    *,
    render_package: Any | None = None,
) -> dict[str, Any]:
    receipt = _require_closed_mapping(
        value,
        {
            "schema_version",
            "render_package_sha256",
            "artifact_snapshot_sha256",
            "output_sha256",
            "automatic_checks",
            "automatic_status",
            "user_review",
            "status",
            "issues",
            "receipt_sha256",
        },
        label="artifact render verification receipt",
    )
    if receipt["schema_version"] != ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION:
        raise _error(
            "Artifact render verification receipt version is unsupported.",
            "ARTIFACT_RENDER_RECEIPT_UNSUPPORTED",
        )
    _require_sha256(receipt["render_package_sha256"], label="render package digest")
    _require_sha256(receipt["artifact_snapshot_sha256"], label="artifact snapshot digest")
    _require_sha256(receipt["output_sha256"], label="render output digest")
    checks = _normalize_automatic_checks(receipt["automatic_checks"])
    review = _normalize_user_review(receipt["user_review"])
    expected_status, expected_automatic_status, expected_issues = _receipt_outcome(
        checks,
        review,
    )
    if receipt["automatic_status"] != expected_automatic_status:
        raise _error("Automatic render status is invalid.", "ARTIFACT_RENDER_RECEIPT_INTEGRITY_FAILED")
    if receipt["status"] != expected_status:
        raise _error("Render verification status is invalid.", "ARTIFACT_RENDER_RECEIPT_INTEGRITY_FAILED")
    if type(receipt["issues"]) is not list or receipt["issues"] != expected_issues:
        raise _error("Render verification issues are invalid.", "ARTIFACT_RENDER_RECEIPT_INTEGRITY_FAILED")
    supplied_sha256 = _require_sha256(receipt["receipt_sha256"], label="receipt digest")
    unsigned = {key: copy.deepcopy(item) for key, item in receipt.items() if key != "receipt_sha256"}
    if _sha256_value(unsigned) != supplied_sha256:
        raise _error("Render verification receipt digest is invalid.", "ARTIFACT_RENDER_RECEIPT_INTEGRITY_FAILED")
    if render_package is not None:
        package = verify_artifact_render_package(render_package)
        if (
            receipt["render_package_sha256"] != package["package_sha256"]
            or receipt["artifact_snapshot_sha256"]
            != package["artifact_binding"]["artifact_snapshot_sha256"]
            or receipt["output_sha256"] != package["output"]["output_sha256"]
        ):
            raise _error(
                "Render verification receipt is bound to another package.",
                "ARTIFACT_RENDER_RECEIPT_BINDING_MISMATCH",
            )
    return copy.deepcopy(receipt)


__all__ = [
    "ARTIFACT_RENDER_PACKAGE_VERSION",
    "ARTIFACT_RENDER_VERIFICATION_RECEIPT_VERSION",
    "MAX_PPTX_ARCHIVE_MEMBERS",
    "MAX_PPTX_EXTRACTED_CHARS",
    "MAX_PPTX_SLIDES",
    "MAX_PPTX_SOURCE_BYTES",
    "MAX_PPTX_UNCOMPRESSED_BYTES",
    "PPTX_CONTENT_TYPE",
    "PPTX_INGEST_PACKAGE_VERSION",
    "PresentationPackageError",
    "build_artifact_render_package",
    "build_artifact_render_verification_receipt",
    "build_pptx_ingest_package",
    "verify_artifact_render_package",
    "verify_artifact_render_verification_receipt",
    "verify_pptx_ingest_package",
]
