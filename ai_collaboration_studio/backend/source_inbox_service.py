from __future__ import annotations

"""Persist and operate the domain-neutral source inbox.

The inbox is intentionally separated from ``manual_chatgpt`` conclusions.
Imported packets describe externally discovered source items, remain
``external_unverified``, and require a local user acknowledgement before they
can be attached to a room.  A round draft never creates or starts a round.
"""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from typing import TYPE_CHECKING, Any

from .source_inbox_contracts import (
    EXTERNAL_UNVERIFIED,
    SOURCE_IMPORT_STATUSES,
    SOURCE_STATUS_ATTACHED,
    SOURCE_STATUS_AWAITING_USER,
    SOURCE_STATUS_DUPLICATE,
    SOURCE_STATUS_RECEIVED,
    SOURCE_STATUS_ROUND_DRAFTED,
    SOURCE_STATUS_VALIDATED,
    SOURCE_ITEM_FINGERPRINT_VERSION,
    SourceInboxContractError,
    accept_source_import,
    build_source_import_receipt,
    canonical_sha256,
    project_source_item_fingerprint,
)

if TYPE_CHECKING:
    from .store import StudioStore


SOURCE_INBOX_IMPORT_RECORD_VERSION = "source_inbox_import_record_v1"
SOURCE_INBOX_ITEM_RECORD_VERSION = "source_inbox_item_record_v1"
SOURCE_INBOX_EVENT_VERSION = "source_inbox_state_event_v1"
SOURCE_INBOX_ATTACHMENT_VERSION = "source_inbox_attachment_v1"
SOURCE_INBOX_ROUND_DRAFT_VERSION = "source_inbox_round_draft_v1"
SOURCE_INBOX_IMPORT_RESULT_VERSION = "source_inbox_import_result_v1"
SOURCE_INBOX_LIST_VERSION = "source_inbox_list_v1"

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class SourceInboxError(ValueError):
    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def ensure_source_inbox_schema(connection: sqlite3.Connection) -> None:
    """Create the inbox schema inside Studio's controlled initialization.

    Formal databases are never initialized by this function at runtime: the
    host startup gate still requires an explicitly prepared migration first.
    """

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_inbox_imports (
            id TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_inbox_import_record_v1'),
            source_channel TEXT NOT NULL,
            source_key TEXT NOT NULL,
            external_run_id TEXT NOT NULL,
            import_key_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(import_key_sha256)=64),
            source_payload_bytes INTEGER NOT NULL CHECK(source_payload_bytes>=0),
            source_payload_sha256 TEXT NOT NULL CHECK(length(source_payload_sha256)=64),
            normalized_packet_sha256 TEXT NOT NULL CHECK(length(normalized_packet_sha256)=64),
            packet_json TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
            status TEXT NOT NULL,
            received_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(source_channel,source_key,external_run_id)
        );

        CREATE TABLE IF NOT EXISTS source_inbox_items (
            id TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_inbox_item_record_v1'),
            origin_import_id TEXT NOT NULL,
            server_fingerprint TEXT NOT NULL UNIQUE
                CHECK(length(server_fingerprint)=64),
            item_sha256 TEXT NOT NULL CHECK(length(item_sha256)=64),
            external_item_id TEXT NOT NULL DEFAULT '',
            item_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT '',
            headline TEXT NOT NULL,
            summary TEXT NOT NULL,
            item_json TEXT NOT NULL,
            state TEXT NOT NULL,
            state_version INTEGER NOT NULL DEFAULT 1 CHECK(state_version>0),
            acknowledged_by TEXT NOT NULL DEFAULT '',
            acknowledged_at INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(origin_import_id) REFERENCES source_inbox_imports(id)
        );

        CREATE TABLE IF NOT EXISTS source_inbox_import_items (
            import_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            position INTEGER NOT NULL CHECK(position>=0),
            disposition TEXT NOT NULL CHECK(disposition IN ('CREATED','DUPLICATE')),
            PRIMARY KEY(import_id,item_id),
            UNIQUE(import_id,position),
            FOREIGN KEY(import_id) REFERENCES source_inbox_imports(id) ON DELETE CASCADE,
            FOREIGN KEY(item_id) REFERENCES source_inbox_items(id)
        );

        CREATE TABLE IF NOT EXISTS source_inbox_state_events (
            id TEXT PRIMARY KEY,
            event_version TEXT NOT NULL
                CHECK(event_version='source_inbox_state_event_v1'),
            item_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL CHECK(sequence_no>0),
            event_type TEXT NOT NULL,
            from_state TEXT NOT NULL,
            to_state TEXT NOT NULL,
            actor TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
            previous_event_sha256 TEXT NOT NULL,
            event_sha256 TEXT NOT NULL UNIQUE CHECK(length(event_sha256)=64),
            created_at INTEGER NOT NULL,
            UNIQUE(item_id,sequence_no),
            FOREIGN KEY(item_id) REFERENCES source_inbox_items(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS source_inbox_attachments (
            id TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_inbox_attachment_v1'),
            item_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            material_id TEXT NOT NULL,
            material_version INTEGER NOT NULL CHECK(material_version>0),
            item_sha256 TEXT NOT NULL CHECK(length(item_sha256)=64),
            attachment_sha256 TEXT NOT NULL UNIQUE CHECK(length(attachment_sha256)=64),
            attached_by TEXT NOT NULL,
            attached_at INTEGER NOT NULL,
            UNIQUE(item_id,room_id),
            FOREIGN KEY(item_id) REFERENCES source_inbox_items(id) ON DELETE CASCADE,
            FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY(material_id) REFERENCES materials(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS source_inbox_round_drafts (
            id TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_inbox_round_draft_v1'),
            item_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            attachment_id TEXT NOT NULL,
            room_snapshot_sha256 TEXT NOT NULL CHECK(length(room_snapshot_sha256)=64),
            objective TEXT NOT NULL,
            draft_json TEXT NOT NULL,
            draft_sha256 TEXT NOT NULL UNIQUE CHECK(length(draft_sha256)=64),
            formal_round_created INTEGER NOT NULL DEFAULT 0
                CHECK(formal_round_created=0),
            provider_calls_performed INTEGER NOT NULL DEFAULT 0
                CHECK(provider_calls_performed=0),
            market_calls_performed INTEGER NOT NULL DEFAULT 0
                CHECK(market_calls_performed=0),
            created_by TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(item_id,room_id),
            FOREIGN KEY(item_id) REFERENCES source_inbox_items(id) ON DELETE CASCADE,
            FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE,
            FOREIGN KEY(attachment_id) REFERENCES source_inbox_attachments(id)
        );

        CREATE INDEX IF NOT EXISTS idx_source_inbox_items_state_time
            ON source_inbox_items(state,updated_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_source_inbox_import_time
            ON source_inbox_imports(received_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_source_inbox_events_item
            ON source_inbox_state_events(item_id,sequence_no);
        CREATE INDEX IF NOT EXISTS idx_source_inbox_attachments_room
            ON source_inbox_attachments(room_id,attached_at DESC,id DESC);
        CREATE INDEX IF NOT EXISTS idx_source_inbox_drafts_room
            ON source_inbox_round_drafts(room_id,created_at DESC,id DESC);
        """
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_object(
    value: Any,
    label: str,
    *,
    require_canonical: bool,
) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, item in pairs:
            if key in output:
                raise ValueError("duplicate JSON key")
            output[key] = item
        return output

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        if type(value) is not str:
            raise ValueError("stored JSON is not text")
        parsed = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if type(parsed) is not dict:
            raise ValueError("stored JSON is not an object")
        if require_canonical and _canonical_json(parsed) != value:
            raise ValueError("stored JSON is not canonical")
    except (OverflowError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceInboxError(
            f"{label} 已损坏。",
            code="SOURCE_INBOX_RECORD_CORRUPT",
            status=409,
        ) from exc
    return parsed


def _load_object(value: Any, label: str) -> dict[str, Any]:
    return _decode_object(value, label, require_canonical=True)


def _load_snapshot_object(value: Any, label: str) -> dict[str, Any]:
    return _decode_object(value, label, require_canonical=False)


def _stored_int(
    data: dict[str, Any],
    key: str,
    *,
    minimum: int = 0,
) -> int:
    value = data.get(key)
    if type(value) is not int or value < minimum:
        raise SourceInboxError(
            "来源收件箱整数镜像已损坏。",
            code="SOURCE_INBOX_RECORD_CORRUPT",
            status=409,
        )
    return value


def _require_record(condition: bool, message: str = "来源收件箱记录已损坏。") -> None:
    if not condition:
        raise SourceInboxError(
            message,
            code="SOURCE_INBOX_RECORD_CORRUPT",
            status=409,
        )


def _clean_identifier(value: Any, label: str, *, maximum: int = 160) -> str:
    if type(value) is not str:
        raise SourceInboxError(
            f"{label} 无效。",
            code="SOURCE_INBOX_REQUEST_INVALID",
        )
    clean = value.strip()
    if len(clean) > maximum or _IDENTIFIER_RE.fullmatch(clean) is None:
        raise SourceInboxError(
            f"{label} 无效。",
            code="SOURCE_INBOX_REQUEST_INVALID",
        )
    return clean


def _clean_actor(value: Any) -> str:
    return _clean_identifier(str(value or "local_user"), "actor", maximum=80)


def _clean_state_version(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise SourceInboxError(
            "expected_state_version 必须是正整数。",
            code="SOURCE_INBOX_REQUEST_INVALID",
        )
    return value


def _row_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


class SourceInboxService:
    def __init__(self, store: StudioStore, *, clock: Any = time.time) -> None:
        self.store = store
        self._clock = clock

    def _now_ms(self) -> int:
        value = self._clock()
        if type(value) not in {int, float} or isinstance(value, bool):
            raise SourceInboxError(
                "服务端时钟无效。",
                code="SOURCE_INBOX_CLOCK_INVALID",
                status=500,
            )
        timestamp = int(value * 1000)
        if timestamp < 0:
            raise SourceInboxError(
                "服务端时钟无效。",
                code="SOURCE_INBOX_CLOCK_INVALID",
                status=500,
            )
        return timestamp

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        item_id: str,
        event_type: str,
        from_state: str,
        to_state: str,
        actor: str,
        payload: dict[str, Any],
        created_at: int,
    ) -> dict[str, Any]:
        previous = connection.execute(
            """SELECT sequence_no,event_sha256 FROM source_inbox_state_events
               WHERE item_id=? ORDER BY sequence_no DESC LIMIT 1""",
            (item_id,),
        ).fetchone()
        sequence_no = int(previous["sequence_no"] if previous else 0) + 1
        previous_sha256 = str(previous["event_sha256"] if previous else "")
        payload_json = _canonical_json(payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        event = {
            "version": SOURCE_INBOX_EVENT_VERSION,
            "item_id": item_id,
            "sequence_no": sequence_no,
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "actor": actor,
            "payload_sha256": payload_sha256,
            "previous_event_sha256": previous_sha256,
            "created_at": created_at,
        }
        event_sha256 = canonical_sha256(event)
        event_id = _new_id("source_event")
        connection.execute(
            """INSERT INTO source_inbox_state_events(
                   id,event_version,item_id,sequence_no,event_type,from_state,to_state,
                   actor,payload_json,payload_sha256,previous_event_sha256,event_sha256,
                   created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                SOURCE_INBOX_EVENT_VERSION,
                item_id,
                sequence_no,
                event_type,
                from_state,
                to_state,
                actor,
                payload_json,
                payload_sha256,
                previous_sha256,
                event_sha256,
                created_at,
            ),
        )
        return {"id": event_id, **event, "event_sha256": event_sha256}

    def _verify_import_record(
        self,
        connection: sqlite3.Connection,
        import_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM source_inbox_imports WHERE id=?",
            (import_id,),
        ).fetchone()
        _require_record(row is not None, "来源导入记录缺失。")
        data = _row_dict(row)
        packet = _load_object(data.get("packet_json"), "来源导入包")
        receipt = _load_object(data.get("receipt_json"), "来源导入回执")
        received_at = _stored_int(data, "received_at")
        source_payload_bytes = _stored_int(data, "source_payload_bytes")
        created_at = _stored_int(data, "created_at")
        status = str(data.get("status") or "")
        _require_record(
            str(data.get("record_version") or "") == SOURCE_INBOX_IMPORT_RECORD_VERSION
            and status in SOURCE_IMPORT_STATUSES
            and created_at == received_at
        )
        try:
            expected_receipt = build_source_import_receipt(
                packet,
                received_at_ms=received_at,
                source_payload_bytes=source_payload_bytes,
                source_payload_sha256=str(data.get("source_payload_sha256") or ""),
                status=status,
            )
        except SourceInboxContractError as exc:
            raise SourceInboxError(
                "来源导入回执无法重建。",
                code="SOURCE_INBOX_RECORD_CORRUPT",
                status=409,
            ) from exc
        _require_record(
            receipt == expected_receipt
            and str(data.get("receipt_sha256") or "")
            == str(expected_receipt.get("receipt_sha256") or "")
            and str(data.get("normalized_packet_sha256") or "")
            == canonical_sha256(packet)
            == str(expected_receipt.get("normalized_packet_sha256") or "")
            and str(data.get("import_key_sha256") or "")
            == str(expected_receipt.get("import_key_sha256") or "")
            and str(data.get("source_payload_sha256") or "")
            == str(expected_receipt.get("source_payload_sha256") or "")
            and source_payload_bytes == expected_receipt.get("source_payload_bytes")
            and received_at == expected_receipt.get("received_at_ms")
            and str(data.get("source_channel") or "") == packet.get("source_channel")
            and str(data.get("source_key") or "") == packet.get("source_key")
            and str(data.get("external_run_id") or "") == packet.get("external_run_id")
        )

        packet_items = packet.get("items")
        _require_record(type(packet_items) is list)
        link_rows = connection.execute(
            """SELECT link.position,link.disposition,item.id AS item_id,
                      item.record_version AS item_record_version,
                      item.server_fingerprint,item.item_sha256,item.item_json
               FROM source_inbox_import_items link
               JOIN source_inbox_items item ON item.id=link.item_id
               WHERE link.import_id=? ORDER BY link.position""",
            (import_id,),
        ).fetchall()
        _require_record(len(link_rows) == len(packet_items))
        links: list[dict[str, Any]] = []
        for position, (link_row, packet_item) in enumerate(zip(link_rows, packet_items)):
            link = _row_dict(link_row)
            linked_item = _load_object(link.get("item_json"), "来源导入关联条目")
            disposition = str(link.get("disposition") or "")
            _require_record(
                _stored_int(link, "position") == position
                and disposition in {"CREATED", "DUPLICATE"}
                and str(link.get("item_record_version") or "")
                == SOURCE_INBOX_ITEM_RECORD_VERSION
                and linked_item == packet_item
                and str(link.get("item_sha256") or "") == canonical_sha256(linked_item)
                and str(link.get("server_fingerprint") or "")
                == str(linked_item.get("server_fingerprint") or "")
            )
            links.append({
                "position": position,
                "disposition": disposition,
                "item_id": str(link.get("item_id") or ""),
            })
        created_count = sum(link["disposition"] == "CREATED" for link in links)
        expected_status = (
            SOURCE_STATUS_VALIDATED
            if not links
            else SOURCE_STATUS_AWAITING_USER
            if created_count
            else SOURCE_STATUS_DUPLICATE
        )
        _require_record(status == expected_status)
        return {"row": data, "packet": packet, "receipt": receipt, "links": links}

    def _attachment_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        item_data: dict[str, Any],
        item: dict[str, Any],
    ) -> dict[str, Any]:
        data = _row_dict(row)
        material_version = _stored_int(data, "material_version", minimum=1)
        attached_at = _stored_int(data, "attached_at", minimum=1)
        projection = {
            "version": SOURCE_INBOX_ATTACHMENT_VERSION,
            "id": str(data.get("id") or ""),
            "item_id": str(data.get("item_id") or ""),
            "room_id": str(data.get("room_id") or ""),
            "material_id": str(data.get("material_id") or ""),
            "material_version": material_version,
            "item_sha256": str(data.get("item_sha256") or ""),
            "attachment_sha256": str(data.get("attachment_sha256") or ""),
            "attached_by": str(data.get("attached_by") or ""),
            "attached_at": attached_at,
        }
        attachment_basis = {
            "version": SOURCE_INBOX_ATTACHMENT_VERSION,
            "item_id": projection["item_id"],
            "room_id": projection["room_id"],
            "material_id": projection["material_id"],
            "material_version": material_version,
            "item_sha256": projection["item_sha256"],
            "attached_by": projection["attached_by"],
            "attached_at": attached_at,
        }
        _require_record(
            str(data.get("record_version") or "") == SOURCE_INBOX_ATTACHMENT_VERSION
            and projection["item_id"] == str(item_data.get("id") or "")
            and projection["item_sha256"] == str(item_data.get("item_sha256") or "")
            and _SHA256_RE.fullmatch(projection["attachment_sha256"]) is not None
            and projection["attachment_sha256"] == canonical_sha256(attachment_basis)
            and _IDENTIFIER_RE.fullmatch(projection["attached_by"]) is not None
        )
        version_rows = connection.execute(
            """SELECT * FROM material_versions
               WHERE material_id=? AND room_id=? AND version=?""",
            (
                projection["material_id"],
                projection["room_id"],
                material_version,
            ),
        ).fetchall()
        _require_record(len(version_rows) == 1, "来源附件材料快照缺失或不唯一。")
        material_snapshot = self.store._material_dict(
            _load_snapshot_object(version_rows[0]["snapshot_json"], "来源附件材料快照")
        )
        sources = item.get("sources") if type(item.get("sources")) is list else []
        primary_source = sources[0] if sources and type(sources[0]) is dict else {}
        content = self._material_content(item)
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        expected_material = self.store._material_dict({
            "id": projection["material_id"],
            "room_id": projection["room_id"],
            "title": f"[来源收件箱] {item.get('headline') or '未命名来源'}"[:120],
            "kind": "url" if primary_source.get("url") else "note",
            "source_url": str(primary_source.get("url") or "")[:2000],
            "content": content,
            "metadata_json": _canonical_json({
                "source_type": "other",
                "publisher": str(primary_source.get("publisher") or "")[:200],
                "published_at": str(item.get("published_at") or "")[:80],
                "original_url": str(primary_source.get("url") or "")[:2000],
                "final_url": str(primary_source.get("url") or "")[:2000],
                "source_sha256": projection["item_sha256"],
                "content_sha256": content_sha256,
                "prompt_injection_risk": self.store._material_prompt_injection_risk(content),
            }),
            "version": material_version,
            "active": 1,
            "official_supplement_pending": 0,
            "created_at": attached_at,
            "updated_at": attached_at,
        })
        _require_record(
            _stored_int(_row_dict(version_rows[0]), "changed_at") == attached_at
            and material_snapshot == expected_material,
            "来源附件材料快照与已封存条目不一致。",
        )
        return projection

    def _verified_room_version(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        stored_record: bool,
    ) -> dict[str, Any]:
        try:
            record = self.store._room_version_record(
                row,
                include_snapshot=True,
                connection=connection,
            )
        except Exception as exc:
            code = "SOURCE_INBOX_RECORD_CORRUPT" if stored_record else "SOURCE_INBOX_ROOM_SNAPSHOT_INVALID"
            raise SourceInboxError(
                "目标房间设置快照无法验证。",
                code=code,
                status=409,
            ) from exc
        if (
            record.get("integrity_ok") is not True
            or record.get("snapshot_storage_integrity_ok") is not True
            or _SHA256_RE.fullmatch(str(record.get("snapshot_sha256") or "")) is None
            or record.get("snapshot_sha256") != record.get("stored_snapshot_sha256")
        ):
            code = "SOURCE_INBOX_RECORD_CORRUPT" if stored_record else "SOURCE_INBOX_ROOM_SNAPSHOT_INVALID"
            raise SourceInboxError(
                "目标房间设置快照完整性校验失败。",
                code=code,
                status=409,
            )
        return record

    def _draft_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        item_data: dict[str, Any],
        attachments_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        data = _row_dict(row)
        draft = _load_object(data.get("draft_json"), "轮次草稿")
        expected_fields = {
            "version", "id", "item_id", "room_id", "attachment_id", "material_id",
            "material_version", "item_sha256", "room_settings_version",
            "room_snapshot_sha256", "objective", "request_state_version", "state",
            "formal_round_created", "provider_calls_performed", "market_calls_performed",
            "execution_capability", "user_confirmation_required_to_launch", "created_by",
            "created_at", "draft_sha256",
        }
        _require_record(set(draft) == expected_fields)
        draft_basis = {key: value for key, value in draft.items() if key != "draft_sha256"}
        stored_sha256 = str(data.get("draft_sha256") or "")
        attachment = attachments_by_id.get(str(draft.get("attachment_id") or ""))
        _require_record(
            str(data.get("record_version") or "") == SOURCE_INBOX_ROUND_DRAFT_VERSION
            and _SHA256_RE.fullmatch(stored_sha256) is not None
            and canonical_sha256(draft_basis) == stored_sha256
            and draft.get("draft_sha256") == stored_sha256
            and draft.get("version") == SOURCE_INBOX_ROUND_DRAFT_VERSION
            and draft.get("id") == str(data.get("id") or "")
            and draft.get("item_id") == str(data.get("item_id") or "")
            == str(item_data.get("id") or "")
            and draft.get("room_id") == str(data.get("room_id") or "")
            and draft.get("attachment_id") == str(data.get("attachment_id") or "")
            and draft.get("room_snapshot_sha256")
            == str(data.get("room_snapshot_sha256") or "")
            and draft.get("objective") == str(data.get("objective") or "")
            and draft.get("created_by") == str(data.get("created_by") or "")
            and draft.get("created_at") == _stored_int(data, "created_at", minimum=1)
            and draft.get("formal_round_created") is False
            and draft.get("provider_calls_performed") == 0
            and type(draft.get("provider_calls_performed")) is int
            and draft.get("market_calls_performed") == 0
            and type(draft.get("market_calls_performed")) is int
            and draft.get("execution_capability") == "none"
            and draft.get("user_confirmation_required_to_launch") is True
            and draft.get("state") == "DRAFT"
            and _stored_int(data, "formal_round_created") == 0
            and _stored_int(data, "provider_calls_performed") == 0
            and _stored_int(data, "market_calls_performed") == 0
            and type(draft.get("request_state_version")) is int
            and int(draft.get("request_state_version") or 0) >= 1
            and type(draft.get("room_settings_version")) is int
            and int(draft.get("room_settings_version") or 0) >= 1
            and type(draft.get("material_version")) is int
            and int(draft.get("material_version") or 0) >= 1
            and draft.get("item_sha256") == str(item_data.get("item_sha256") or "")
            and attachment is not None
            and attachment.get("item_id") == draft.get("item_id")
            and attachment.get("room_id") == draft.get("room_id")
            and attachment.get("material_id") == draft.get("material_id")
            and attachment.get("material_version") == draft.get("material_version")
            and attachment.get("item_sha256") == draft.get("item_sha256")
        )
        room_rows = connection.execute(
            "SELECT * FROM room_versions WHERE room_id=? AND version=?",
            (draft["room_id"], draft["room_settings_version"]),
        ).fetchall()
        _require_record(len(room_rows) == 1, "轮次草稿绑定的房间设置快照缺失或不唯一。")
        room_record = self._verified_room_version(
            connection,
            room_rows[0],
            stored_record=True,
        )
        _require_record(
            draft.get("room_snapshot_sha256") == room_record.get("snapshot_sha256")
        )
        return draft

    def _item_projection(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        include_events: bool,
    ) -> dict[str, Any]:
        data = _row_dict(row)
        item_id = str(data.get("id") or "")
        import_record = self._verify_import_record(
            connection,
            str(data.get("origin_import_id") or ""),
        )
        item = _load_object(data.get("item_json"), "来源条目")
        item_sha256 = str(data.get("item_sha256") or "")
        created_at = _stored_int(data, "created_at")
        updated_at = _stored_int(data, "updated_at")
        state_version = _stored_int(data, "state_version", minimum=1)
        acknowledged_at = _stored_int(data, "acknowledged_at")
        expires_at = _stored_int(data, "expires_at")
        origin_links = [
            link for link in import_record["links"]
            if link["item_id"] == item_id and link["disposition"] == "CREATED"
        ]
        mirror_fields = (
            "external_item_id", "item_type", "severity", "occurred_at", "published_at",
            "headline", "summary",
        )
        _require_record(
            str(data.get("record_version") or "") == SOURCE_INBOX_ITEM_RECORD_VERSION
            and len(origin_links) == 1
            and item_sha256 == canonical_sha256(item)
            and item.get("server_fingerprint_version") == SOURCE_ITEM_FINGERPRINT_VERSION
            and item.get("server_fingerprint") == project_source_item_fingerprint(item)
            == str(data.get("server_fingerprint") or "")
            and item.get("external_claims_verification") == EXTERNAL_UNVERIFIED
            and all(str(data.get(field) or "") == str(item.get(field) or "") for field in mirror_fields)
            and str(data.get("source_channel") or "")
            == str(import_record["row"].get("source_channel") or "")
            and str(data.get("source_key") or "")
            == str(import_record["row"].get("source_key") or "")
            and str(data.get("external_run_id") or "")
            == str(import_record["row"].get("external_run_id") or "")
            and _stored_int(data, "received_at")
            == _stored_int(import_record["row"], "received_at")
            and created_at == _stored_int(import_record["row"], "received_at")
            and updated_at >= created_at
            and str(data.get("state") or "") in {
                SOURCE_STATUS_AWAITING_USER,
                SOURCE_STATUS_ATTACHED,
                SOURCE_STATUS_ROUND_DRAFTED,
            }
            and (
                (acknowledged_at == 0 and str(data.get("acknowledged_by") or "") == "")
                or (
                    acknowledged_at >= created_at
                    and _IDENTIFIER_RE.fullmatch(str(data.get("acknowledged_by") or "")) is not None
                )
            )
            and (expires_at == 0 or expires_at >= created_at)
        )
        attachment_rows = connection.execute(
            """SELECT * FROM source_inbox_attachments
               WHERE item_id=? ORDER BY attached_at,id""",
            (item_id,),
        ).fetchall()
        attachments = [
            self._attachment_projection(
                connection,
                attachment,
                item_data=data,
                item=item,
            )
            for attachment in attachment_rows
        ]
        attachments_by_id = {attachment["id"]: attachment for attachment in attachments}
        _require_record(len(attachments_by_id) == len(attachments))
        draft_rows = connection.execute(
            """SELECT * FROM source_inbox_round_drafts
               WHERE item_id=? ORDER BY created_at,id""",
            (item_id,),
        ).fetchall()
        drafts = [
            self._draft_projection(
                connection,
                draft,
                item_data=data,
                attachments_by_id=attachments_by_id,
            )
            for draft in draft_rows
        ]
        drafts_by_id = {draft["id"]: draft for draft in drafts}
        _require_record(len(drafts_by_id) == len(drafts))

        events = connection.execute(
            """SELECT * FROM source_inbox_state_events
               WHERE item_id=? ORDER BY sequence_no""",
            (item_id,),
        ).fetchall()
        _require_record(len(events) >= 3, "来源事件审计链不完整。")
        previous_sha256 = ""
        previous_time = created_at
        current_state = SOURCE_STATUS_AWAITING_USER
        ack_event: dict[str, Any] | None = None
        attachment_event_ids: set[str] = set()
        draft_event_ids: set[str] = set()
        projected_events: list[dict[str, Any]] = []
        for sequence_no, event_row in enumerate(events, start=1):
            event_data = _row_dict(event_row)
            payload = _load_object(event_data.get("payload_json"), "来源事件")
            payload_sha256 = canonical_sha256(payload)
            created = _stored_int(event_data, "created_at")
            event_basis = {
                "version": SOURCE_INBOX_EVENT_VERSION,
                "item_id": str(event_data.get("item_id") or ""),
                "sequence_no": _stored_int(event_data, "sequence_no", minimum=1),
                "event_type": str(event_data.get("event_type") or ""),
                "from_state": str(event_data.get("from_state") or ""),
                "to_state": str(event_data.get("to_state") or ""),
                "actor": str(event_data.get("actor") or ""),
                "payload_sha256": payload_sha256,
                "previous_event_sha256": previous_sha256,
                "created_at": created,
            }
            event_sha256 = canonical_sha256(event_basis)
            _require_record(
                str(event_data.get("event_version") or "") == SOURCE_INBOX_EVENT_VERSION
                and event_basis["item_id"] == item_id
                and event_basis["sequence_no"] == sequence_no
                and created >= previous_time
                and _IDENTIFIER_RE.fullmatch(event_basis["actor"]) is not None
                and payload_sha256 == str(event_data.get("payload_sha256") or "")
                and previous_sha256 == str(event_data.get("previous_event_sha256") or "")
                and event_sha256 == str(event_data.get("event_sha256") or "")
            )
            if sequence_no == 1:
                _require_record(
                    event_basis["event_type"] == "IMPORTED"
                    and event_basis["from_state"] == ""
                    and event_basis["to_state"] == SOURCE_STATUS_RECEIVED
                    and created == created_at
                    and payload == {
                        "import_id": str(data.get("origin_import_id") or ""),
                        "receipt_sha256": import_record["receipt"]["receipt_sha256"],
                        "item_sha256": item_sha256,
                    }
                )
            elif sequence_no == 2:
                _require_record(
                    event_basis["event_type"] == "VALIDATED"
                    and event_basis["from_state"] == SOURCE_STATUS_RECEIVED
                    and event_basis["to_state"] == SOURCE_STATUS_VALIDATED
                    and event_basis["actor"] == "deterministic_kernel"
                    and created == created_at
                    and payload == {"server_fingerprint": item["server_fingerprint"]}
                )
            elif sequence_no == 3:
                _require_record(
                    event_basis["event_type"] == "QUEUED_FOR_USER"
                    and event_basis["from_state"] == SOURCE_STATUS_VALIDATED
                    and event_basis["to_state"] == SOURCE_STATUS_AWAITING_USER
                    and event_basis["actor"] == "deterministic_kernel"
                    and created == created_at
                    and payload == {"external_claims_verification": EXTERNAL_UNVERIFIED}
                )
            else:
                _require_record(event_basis["from_state"] == current_state)
                if event_basis["event_type"] == "ACKNOWLEDGED_AS_READ_ONLY":
                    _require_record(
                        ack_event is None
                        and event_basis["to_state"] == current_state
                        and set(payload) == {
                            "fact_confirmation", "approval", "execution_authorization"
                        }
                        and payload.get("fact_confirmation") is False
                        and payload.get("approval") is False
                        and payload.get("execution_authorization") is False
                    )
                    ack_event = {"actor": event_basis["actor"], "created_at": created}
                elif event_basis["event_type"] == "ATTACHED_TO_ROOM":
                    attachment_id = str(payload.get("attachment_id") or "")
                    attachment = attachments_by_id.get(attachment_id)
                    next_state = (
                        SOURCE_STATUS_ATTACHED
                        if current_state == SOURCE_STATUS_AWAITING_USER
                        else current_state
                    )
                    _require_record(
                        ack_event is not None
                        and attachment is not None
                        and attachment_id not in attachment_event_ids
                        and current_state in {
                            SOURCE_STATUS_AWAITING_USER,
                            SOURCE_STATUS_ATTACHED,
                            SOURCE_STATUS_ROUND_DRAFTED,
                        }
                        and event_basis["to_state"] == next_state
                        and set(payload) == {
                            "attachment_id", "attachment_sha256", "room_id",
                            "material_id", "material_version",
                        }
                        and payload.get("attachment_id") == attachment["id"]
                        and payload.get("attachment_sha256") == attachment["attachment_sha256"]
                        and payload.get("room_id") == attachment["room_id"]
                        and payload.get("material_id") == attachment["material_id"]
                        and type(payload.get("material_version")) is int
                        and payload.get("material_version") == attachment["material_version"]
                    )
                    attachment_event_ids.add(attachment_id)
                    current_state = next_state
                elif event_basis["event_type"] == "ROUND_DRAFT_CREATED":
                    draft_id = str(payload.get("draft_id") or "")
                    draft = drafts_by_id.get(draft_id)
                    _require_record(
                        ack_event is not None
                        and draft is not None
                        and draft_id not in draft_event_ids
                        and current_state in {SOURCE_STATUS_ATTACHED, SOURCE_STATUS_ROUND_DRAFTED}
                        and event_basis["to_state"] == SOURCE_STATUS_ROUND_DRAFTED
                        and set(payload) == {
                            "draft_id", "draft_sha256", "formal_round_created",
                            "provider_calls_performed", "market_calls_performed",
                        }
                        and payload.get("draft_id") == draft["id"]
                        and payload.get("draft_sha256") == draft["draft_sha256"]
                        and payload.get("formal_round_created") is False
                        and type(payload.get("provider_calls_performed")) is int
                        and payload.get("provider_calls_performed") == 0
                        and type(payload.get("market_calls_performed")) is int
                        and payload.get("market_calls_performed") == 0
                    )
                    draft_event_ids.add(draft_id)
                    current_state = SOURCE_STATUS_ROUND_DRAFTED
                else:
                    _require_record(False, "来源事件类型不受支持。")
            projected_events.append({**event_basis, "payload": payload, "event_sha256": event_sha256})
            previous_sha256 = event_sha256
            previous_time = created
        _require_record(
            state_version == 1 + len(events) - 3
            and str(data.get("state") or "") == current_state
            and updated_at == previous_time
            and attachment_event_ids == set(attachments_by_id)
            and draft_event_ids == set(drafts_by_id)
            and (
                (ack_event is None and acknowledged_at == 0 and str(data.get("acknowledged_by") or "") == "")
                or (
                    ack_event is not None
                    and acknowledged_at == ack_event["created_at"]
                    and str(data.get("acknowledged_by") or "") == ack_event["actor"]
                )
            )
        )
        projection = {
            "version": SOURCE_INBOX_ITEM_RECORD_VERSION,
            "id": item_id,
            "source_channel": str(import_record["row"].get("source_channel") or ""),
            "source_key": str(import_record["row"].get("source_key") or ""),
            "external_run_id": str(import_record["row"].get("external_run_id") or ""),
            "received_at": _stored_int(import_record["row"], "received_at"),
            "server_fingerprint": str(data.get("server_fingerprint") or ""),
            "item_sha256": item_sha256,
            "state": str(data.get("state") or ""),
            "state_version": state_version,
            "acknowledged": acknowledged_at > 0,
            "acknowledged_by": str(data.get("acknowledged_by") or ""),
            "acknowledged_at": acknowledged_at,
            "expires_at": expires_at,
            "created_at": created_at,
            "updated_at": updated_at,
            "external_claims_verification": EXTERNAL_UNVERIFIED,
            "item": item,
            "attachments": attachments,
            "round_drafts": drafts,
            "safety": {
                "acknowledgement_is_fact_confirmation": False,
                "formal_round_created": False,
                "provider_calls_performed": 0,
                "market_calls_performed": 0,
                "execution_capability": "none",
            },
        }
        if include_events:
            projection["events"] = projected_events
        return projection

    @staticmethod
    def _select_item(
        connection: sqlite3.Connection,
        item_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT item.*,imports.source_channel,imports.source_key,
                      imports.external_run_id,imports.received_at
               FROM source_inbox_items item
               JOIN source_inbox_imports imports ON imports.id=item.origin_import_id
               WHERE item.id=?""",
            (item_id,),
        ).fetchone()

    def import_packet(self, raw: Any, *, actor: str = "local_user") -> dict[str, Any]:
        received_at = self._now_ms()
        clean_actor = _clean_actor(actor)
        packet, provisional_receipt = accept_source_import(
            raw,
            received_at_ms=received_at,
        )
        import_key_sha256 = str(provisional_receipt["import_key_sha256"])
        normalized_sha256 = str(provisional_receipt["normalized_packet_sha256"])
        payload_sha256 = str(provisional_receipt["source_payload_sha256"])
        packet_json = _canonical_json(packet)

        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM source_inbox_imports WHERE import_key_sha256=?",
                (import_key_sha256,),
            ).fetchone()
            if existing:
                verified_import = self._verify_import_record(
                    connection,
                    str(existing["id"]),
                )
                if str(verified_import["row"]["normalized_packet_sha256"]) != normalized_sha256:
                    raise SourceInboxError(
                        "同一 source/run 标识已对应不同内容，拒绝语义漂移。",
                        code="SOURCE_IMPORT_KEY_CONFLICT",
                        status=409,
                    )
                item_rows = [
                    self._select_item(connection, link["item_id"])
                    for link in verified_import["links"]
                ]
                _require_record(all(item_row is not None for item_row in item_rows))
                return {
                    "version": SOURCE_INBOX_IMPORT_RESULT_VERSION,
                    "import_id": str(verified_import["row"]["id"]),
                    "status": str(verified_import["row"]["status"]),
                    "receipt": verified_import["receipt"],
                    "items": [
                        self._item_projection(connection, row, include_events=False)
                        for row in item_rows
                    ],
                    "idempotent_replay": True,
                    "created_item_count": 0,
                    "duplicate_item_count": len(item_rows),
                }

            decisions: list[dict[str, Any]] = []
            created_count = 0
            duplicate_count = 0
            for item in packet["items"]:
                item_json = _canonical_json(item)
                item_sha256 = canonical_sha256(item)
                fingerprint = str(item["server_fingerprint"])
                existing_item = connection.execute(
                    "SELECT id FROM source_inbox_items WHERE server_fingerprint=?",
                    (fingerprint,),
                ).fetchone()
                existing_item_id = ""
                if existing_item is None:
                    created_count += 1
                else:
                    existing_item_id = str(existing_item["id"])
                    existing_item_row = self._select_item(connection, existing_item_id)
                    _require_record(existing_item_row is not None)
                    existing_projection = self._item_projection(
                        connection,
                        existing_item_row,
                        include_events=False,
                    )
                    if str(existing_projection["item_sha256"]) != item_sha256:
                        raise SourceInboxError(
                            "同一 server fingerprint 已对应不同的完整来源条目。",
                            code="SOURCE_IMPORT_FINGERPRINT_CONFLICT",
                            status=409,
                        )
                    duplicate_count += 1
                decisions.append({
                    "item": item,
                    "item_json": item_json,
                    "item_sha256": item_sha256,
                    "fingerprint": fingerprint,
                    "existing_item_id": existing_item_id,
                })
            status = (
                SOURCE_STATUS_DUPLICATE
                if decisions and created_count == 0
                else SOURCE_STATUS_VALIDATED
                if not decisions
                else SOURCE_STATUS_AWAITING_USER
            )
            receipt = build_source_import_receipt(
                packet,
                received_at_ms=received_at,
                source_payload_bytes=int(provisional_receipt["source_payload_bytes"]),
                source_payload_sha256=payload_sha256,
                status=status,
            )
            import_id = _new_id("source_import")
            connection.execute(
                """INSERT INTO source_inbox_imports(
                       id,record_version,source_channel,source_key,external_run_id,
                       import_key_sha256,source_payload_bytes,source_payload_sha256,
                       normalized_packet_sha256,packet_json,receipt_json,receipt_sha256,
                       status,received_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    import_id,
                    SOURCE_INBOX_IMPORT_RECORD_VERSION,
                    packet["source_channel"],
                    packet["source_key"],
                    packet["external_run_id"],
                    import_key_sha256,
                    int(provisional_receipt["source_payload_bytes"]),
                    payload_sha256,
                    normalized_sha256,
                    packet_json,
                    _canonical_json(receipt),
                    str(receipt["receipt_sha256"]),
                    status,
                    received_at,
                    received_at,
                ),
            )
            item_ids: list[str] = []
            for position, decision in enumerate(decisions):
                item = decision["item"]
                fingerprint = decision["fingerprint"]
                item_sha256 = decision["item_sha256"]
                item_id = decision["existing_item_id"]
                disposition = "DUPLICATE" if item_id else "CREATED"
                if not item_id:
                    disposition = "CREATED"
                    item_id = _new_id("source_item")
                    connection.execute(
                        """INSERT INTO source_inbox_items(
                               id,record_version,origin_import_id,server_fingerprint,item_sha256,
                               external_item_id,item_type,severity,occurred_at,published_at,
                               headline,summary,item_json,state,state_version,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            item_id,
                            SOURCE_INBOX_ITEM_RECORD_VERSION,
                            import_id,
                            fingerprint,
                            item_sha256,
                            item["external_item_id"],
                            item["item_type"],
                            item["severity"],
                            item["occurred_at"],
                            item["published_at"],
                            item["headline"],
                            item["summary"],
                            decision["item_json"],
                            SOURCE_STATUS_AWAITING_USER,
                            1,
                            received_at,
                            received_at,
                        ),
                    )
                    event_payload = {
                        "import_id": import_id,
                        "receipt_sha256": str(receipt["receipt_sha256"]),
                        "item_sha256": item_sha256,
                    }
                    self._append_event(
                        connection,
                        item_id=item_id,
                        event_type="IMPORTED",
                        from_state="",
                        to_state=SOURCE_STATUS_RECEIVED,
                        actor=clean_actor,
                        payload=event_payload,
                        created_at=received_at,
                    )
                    self._append_event(
                        connection,
                        item_id=item_id,
                        event_type="VALIDATED",
                        from_state=SOURCE_STATUS_RECEIVED,
                        to_state=SOURCE_STATUS_VALIDATED,
                        actor="deterministic_kernel",
                        payload={"server_fingerprint": fingerprint},
                        created_at=received_at,
                    )
                    self._append_event(
                        connection,
                        item_id=item_id,
                        event_type="QUEUED_FOR_USER",
                        from_state=SOURCE_STATUS_VALIDATED,
                        to_state=SOURCE_STATUS_AWAITING_USER,
                        actor="deterministic_kernel",
                        payload={"external_claims_verification": EXTERNAL_UNVERIFIED},
                        created_at=received_at,
                    )
                connection.execute(
                    """INSERT INTO source_inbox_import_items(
                           import_id,item_id,position,disposition
                       ) VALUES(?,?,?,?)""",
                    (import_id, item_id, position, disposition),
                )
                item_ids.append(item_id)
            self._verify_import_record(connection, import_id)
            projected_items = []
            for item_id in item_ids:
                row = self._select_item(connection, item_id)
                if row is None:
                    raise SourceInboxError(
                        "来源条目写入后无法读取。",
                        code="SOURCE_INBOX_PERSISTENCE_FAILED",
                        status=500,
                    )
                projected_items.append(
                    self._item_projection(connection, row, include_events=False)
                )
            return {
                "version": SOURCE_INBOX_IMPORT_RESULT_VERSION,
                "import_id": import_id,
                "status": status,
                "receipt": receipt,
                "items": projected_items,
                "idempotent_replay": False,
                "created_item_count": created_count,
                "duplicate_item_count": duplicate_count,
            }

    def list_items(
        self,
        *,
        state: str = "",
        query: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        clean_state = str(state or "").strip().upper()
        if clean_state and clean_state not in SOURCE_IMPORT_STATUSES:
            raise SourceInboxError(
                "state 不在白名单中。",
                code="SOURCE_INBOX_REQUEST_INVALID",
            )
        if type(limit) is not int or not 1 <= limit <= 200:
            raise SourceInboxError(
                "limit 必须位于 1 到 200。",
                code="SOURCE_INBOX_REQUEST_INVALID",
            )
        clean_query = str(query or "").strip()[:200]
        clauses: list[str] = []
        parameters: list[Any] = []
        if clean_state:
            clauses.append("item.state=?")
            parameters.append(clean_state)
        if clean_query:
            clauses.append("(item.headline LIKE ? ESCAPE '\\' OR item.summary LIKE ? ESCAPE '\\')")
            escaped = clean_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend([f"%{escaped}%", f"%{escaped}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                f"""SELECT item.*,imports.source_channel,imports.source_key,
                           imports.external_run_id,imports.received_at
                    FROM source_inbox_items item
                    JOIN source_inbox_imports imports ON imports.id=item.origin_import_id
                    {where}
                    ORDER BY item.updated_at DESC,item.id DESC LIMIT ?""",
                parameters,
            ).fetchall()
            items = [
                self._item_projection(connection, row, include_events=False)
                for row in rows
            ]
            counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state,COUNT(*) AS count FROM source_inbox_items GROUP BY state"
                ).fetchall()
            }
        return {
            "version": SOURCE_INBOX_LIST_VERSION,
            "items": items,
            "counts": counts,
            "query": clean_query,
            "state": clean_state,
            "limit": limit,
        }

    def get_item(self, item_id: Any) -> dict[str, Any] | None:
        clean_item_id = _clean_identifier(item_id, "item_id")
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            row = self._select_item(connection, clean_item_id)
            return (
                self._item_projection(connection, row, include_events=True)
                if row is not None
                else None
            )

    def acknowledge(
        self,
        item_id: Any,
        *,
        expected_state_version: Any,
        acknowledgement: Any,
        actor: Any = "local_user",
    ) -> dict[str, Any]:
        clean_item_id = _clean_identifier(item_id, "item_id")
        expected_version = _clean_state_version(expected_state_version)
        if acknowledgement is not True:
            raise SourceInboxError(
                "必须明确确认“已阅，不代表事实确认”。",
                code="SOURCE_INBOX_ACKNOWLEDGEMENT_REQUIRED",
            )
        clean_actor = _clean_actor(actor)
        timestamp = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_item(connection, clean_item_id)
            if row is None:
                raise SourceInboxError("来源条目不存在。", code="SOURCE_INBOX_NOT_FOUND", status=404)
            verified_item = self._item_projection(connection, row, include_events=True)
            current_version = int(verified_item["state_version"])
            if current_version != expected_version:
                raise SourceInboxError(
                    "来源条目状态已变化，请刷新后重试。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            if verified_item["acknowledged"]:
                return verified_item
            state = str(verified_item["state"])
            if state not in {
                SOURCE_STATUS_AWAITING_USER,
                SOURCE_STATUS_ATTACHED,
                SOURCE_STATUS_ROUND_DRAFTED,
            }:
                raise SourceInboxError(
                    "当前状态不能确认已阅。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            cursor = connection.execute(
                """UPDATE source_inbox_items
                   SET acknowledged_by=?,acknowledged_at=?,state_version=state_version+1,
                       updated_at=?
                   WHERE id=? AND state_version=?""",
                (clean_actor, timestamp, timestamp, clean_item_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise SourceInboxError(
                    "来源条目状态已变化，请刷新后重试。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            self._append_event(
                connection,
                item_id=clean_item_id,
                event_type="ACKNOWLEDGED_AS_READ_ONLY",
                from_state=state,
                to_state=state,
                actor=clean_actor,
                payload={
                    "fact_confirmation": False,
                    "approval": False,
                    "execution_authorization": False,
                },
                created_at=timestamp,
            )
            updated = self._select_item(connection, clean_item_id)
            assert updated is not None
            return self._item_projection(connection, updated, include_events=True)

    @staticmethod
    def _material_content(item: dict[str, Any]) -> str:
        lines = [
            "[来源收件箱导入；外部声明尚未核验]",
            f"标题：{item.get('headline') or ''}",
            f"摘要：{item.get('summary') or ''}",
            "",
            "事实声明（external_unverified）：",
        ]
        facts = item.get("facts") if type(item.get("facts")) is list else []
        for fact in facts:
            if type(fact) is dict:
                lines.append(f"- {fact.get('claim') or ''}")
        lines.extend(["", "影响假设（待核验）："])
        hypotheses = (
            item.get("impact_hypotheses")
            if type(item.get("impact_hypotheses")) is list
            else []
        )
        for hypothesis in hypotheses:
            if type(hypothesis) is dict:
                lines.append(
                    f"- {hypothesis.get('statement') or ''} "
                    f"[范围={hypothesis.get('affected_area') or ''}; "
                    f"时间={hypothesis.get('time_horizon') or ''}]"
                )
        lines.extend(["", "未知项："])
        for unknown in item.get("unknowns") or []:
            lines.append(f"- {unknown}")
        lines.extend(["", "来源："])
        for source in item.get("sources") or []:
            if type(source) is dict:
                lines.append(
                    f"- {source.get('publisher') or '未声明发布者'}: "
                    f"{source.get('url') or ''}"
                )
        content = "\n".join(lines).strip()
        if len(content) <= 49_000:
            return content
        marker = "\n\n[材料视图因 50,000 字符上限截断；完整规范条目仍保留在来源收件箱。]"
        return content[: 49_000 - len(marker)].rstrip() + marker

    def attach_to_room(
        self,
        item_id: Any,
        *,
        room_id: Any,
        expected_state_version: Any,
        actor: Any = "local_user",
    ) -> dict[str, Any]:
        clean_item_id = _clean_identifier(item_id, "item_id")
        clean_room_id = _clean_identifier(room_id, "room_id")
        expected_version = _clean_state_version(expected_state_version)
        clean_actor = _clean_actor(actor)
        timestamp = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_item(connection, clean_item_id)
            if row is None:
                raise SourceInboxError("来源条目不存在。", code="SOURCE_INBOX_NOT_FOUND", status=404)
            verified_item = self._item_projection(connection, row, include_events=True)
            existing = next(
                (
                    attachment for attachment in verified_item["attachments"]
                    if attachment["room_id"] == clean_room_id
                ),
                None,
            )
            if existing is not None:
                return {
                    "attachment": existing,
                    "item": verified_item,
                    "idempotent_replay": True,
                }
            if int(verified_item["state_version"]) != expected_version:
                raise SourceInboxError(
                    "来源条目状态已变化，请刷新后重试。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            if not verified_item["acknowledged"]:
                raise SourceInboxError(
                    "附加前必须先确认已阅；确认不代表事实成立。",
                    code="SOURCE_INBOX_ACKNOWLEDGEMENT_REQUIRED",
                    status=409,
                )
            state = str(verified_item["state"])
            if state not in {
                SOURCE_STATUS_AWAITING_USER,
                SOURCE_STATUS_ATTACHED,
                SOURCE_STATUS_ROUND_DRAFTED,
            }:
                raise SourceInboxError(
                    "当前状态不能附加到房间。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            room = connection.execute("SELECT id FROM rooms WHERE id=?", (clean_room_id,)).fetchone()
            if room is None:
                raise SourceInboxError("目标房间不存在。", code="SOURCE_INBOX_ROOM_NOT_FOUND", status=404)

            item = verified_item["item"]
            item_sha256 = str(verified_item["item_sha256"])
            sources = item.get("sources") if type(item.get("sources")) is list else []
            primary_source = sources[0] if sources and type(sources[0]) is dict else {}
            content = self._material_content(item)
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            metadata = {
                "source_type": "other",
                "publisher": str(primary_source.get("publisher") or "")[:200],
                "published_at": str(item.get("published_at") or "")[:80],
                "original_url": str(primary_source.get("url") or "")[:2000],
                "final_url": str(primary_source.get("url") or "")[:2000],
                "source_sha256": item_sha256,
                "content_sha256": content_sha256,
                "prompt_injection_risk": self.store._material_prompt_injection_risk(content),
            }
            material_id = _new_id("mat")
            material = {
                "id": material_id,
                "room_id": clean_room_id,
                "title": f"[来源收件箱] {item.get('headline') or '未命名来源'}"[:120],
                "kind": "url" if primary_source.get("url") else "note",
                "source_url": str(primary_source.get("url") or "")[:2000],
                "content": content,
                "metadata_json": _canonical_json(metadata),
                "version": 1,
                "active": 1,
                "official_supplement_pending": 0,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            connection.execute(
                """INSERT INTO materials(
                       id,room_id,title,kind,source_url,content,metadata_json,version,
                       active,official_supplement_pending,created_at,updated_at
                   ) VALUES(
                       :id,:room_id,:title,:kind,:source_url,:content,:metadata_json,:version,
                       :active,:official_supplement_pending,:created_at,:updated_at
                   )""",
                material,
            )
            self.store._record_material_version(connection, material, timestamp)
            attachment_basis = {
                "version": SOURCE_INBOX_ATTACHMENT_VERSION,
                "item_id": clean_item_id,
                "room_id": clean_room_id,
                "material_id": material_id,
                "material_version": 1,
                "item_sha256": item_sha256,
                "attached_by": clean_actor,
                "attached_at": timestamp,
            }
            attachment_sha256 = canonical_sha256(attachment_basis)
            attachment_id = _new_id("source_attachment")
            connection.execute(
                """INSERT INTO source_inbox_attachments(
                       id,record_version,item_id,room_id,material_id,material_version,
                       item_sha256,attachment_sha256,attached_by,attached_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    attachment_id,
                    SOURCE_INBOX_ATTACHMENT_VERSION,
                    clean_item_id,
                    clean_room_id,
                    material_id,
                    1,
                    item_sha256,
                    attachment_sha256,
                    clean_actor,
                    timestamp,
                ),
            )
            next_state = SOURCE_STATUS_ATTACHED if state == SOURCE_STATUS_AWAITING_USER else state
            cursor = connection.execute(
                """UPDATE source_inbox_items
                   SET state=?,state_version=state_version+1,updated_at=?
                   WHERE id=? AND state_version=?""",
                (next_state, timestamp, clean_item_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise SourceInboxError(
                    "来源条目状态已变化，请刷新后重试。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            connection.execute("UPDATE rooms SET updated_at=? WHERE id=?", (timestamp, clean_room_id))
            self._append_event(
                connection,
                item_id=clean_item_id,
                event_type="ATTACHED_TO_ROOM",
                from_state=state,
                to_state=next_state,
                actor=clean_actor,
                payload={
                    "attachment_id": attachment_id,
                    "attachment_sha256": attachment_sha256,
                    "room_id": clean_room_id,
                    "material_id": material_id,
                    "material_version": 1,
                },
                created_at=timestamp,
            )
            updated = self._select_item(connection, clean_item_id)
            attachment = connection.execute(
                "SELECT * FROM source_inbox_attachments WHERE id=?",
                (attachment_id,),
            ).fetchone()
            assert updated is not None and attachment is not None
            final_item = self._item_projection(connection, updated, include_events=True)
            final_attachment = next(
                entry for entry in final_item["attachments"]
                if entry["id"] == attachment_id
            )
            return {
                "attachment": final_attachment,
                "item": final_item,
                "idempotent_replay": False,
            }

    def create_round_draft(
        self,
        item_id: Any,
        *,
        room_id: Any,
        expected_state_version: Any,
        objective: Any = "",
        actor: Any = "local_user",
    ) -> dict[str, Any]:
        clean_item_id = _clean_identifier(item_id, "item_id")
        clean_room_id = _clean_identifier(room_id, "room_id")
        expected_version = _clean_state_version(expected_state_version)
        if type(objective) is not str or len(objective.strip()) > 4_000:
            raise SourceInboxError(
                "objective 必须是最多 4000 字符的原生字符串。",
                code="SOURCE_INBOX_REQUEST_INVALID",
            )
        clean_actor = _clean_actor(actor)
        timestamp = self._now_ms()
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_item(connection, clean_item_id)
            if row is None:
                raise SourceInboxError("来源条目不存在。", code="SOURCE_INBOX_NOT_FOUND", status=404)
            verified_item = self._item_projection(connection, row, include_events=True)
            item = verified_item["item"]
            default_objective = (
                f"核对来源“{item.get('headline') or '未命名来源'}”的事实、反证、未知项与影响假设；"
                "仅形成研究草稿，不自动调用 Provider，不产生执行授权。"
            )
            try:
                clean_objective = self.store.clean_round_objective(
                    objective if objective.strip() else default_objective
                )
            except ValueError as exc:
                raise SourceInboxError(
                    str(exc),
                    code="SOURCE_INBOX_REQUEST_INVALID",
                ) from exc
            existing = next(
                (
                    draft for draft in verified_item["round_drafts"]
                    if draft["room_id"] == clean_room_id
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.get("objective") != clean_objective
                    or existing.get("request_state_version") != expected_version
                ):
                    raise SourceInboxError(
                        "已有轮次草稿与本次 objective 或原请求状态版本不一致。",
                        code="SOURCE_INBOX_DRAFT_CONFLICT",
                        status=409,
                    )
                return {
                    "round_draft": existing,
                    "item": verified_item,
                    "idempotent_replay": True,
                }
            if int(verified_item["state_version"]) != expected_version:
                raise SourceInboxError(
                    "来源条目状态已变化，请刷新后重试。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            if not verified_item["acknowledged"]:
                raise SourceInboxError(
                    "创建草稿前必须先确认已阅。",
                    code="SOURCE_INBOX_ACKNOWLEDGEMENT_REQUIRED",
                    status=409,
                )
            attachment = next(
                (
                    entry for entry in verified_item["attachments"]
                    if entry["room_id"] == clean_room_id
                ),
                None,
            )
            if attachment is None:
                raise SourceInboxError(
                    "创建草稿前必须先把来源附加到目标房间。",
                    code="SOURCE_INBOX_ATTACHMENT_REQUIRED",
                    status=409,
                )
            state = str(verified_item["state"])
            if state not in {SOURCE_STATUS_ATTACHED, SOURCE_STATUS_ROUND_DRAFTED}:
                raise SourceInboxError(
                    "当前状态不能创建轮次草稿。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            room_version = connection.execute(
                """SELECT * FROM room_versions
                   WHERE room_id=? ORDER BY version DESC LIMIT 1""",
                (clean_room_id,),
            ).fetchone()
            if room_version is None:
                raise SourceInboxError(
                    "目标房间缺少完整设置快照。",
                    code="SOURCE_INBOX_ROOM_SNAPSHOT_INVALID",
                    status=409,
                )
            room_record = self._verified_room_version(
                connection,
                room_version,
                stored_record=False,
            )
            draft_id = _new_id("source_round_draft")
            draft = {
                "version": SOURCE_INBOX_ROUND_DRAFT_VERSION,
                "id": draft_id,
                "item_id": clean_item_id,
                "room_id": clean_room_id,
                "attachment_id": str(attachment["id"]),
                "material_id": str(attachment["material_id"]),
                "material_version": int(attachment["material_version"]),
                "item_sha256": str(verified_item["item_sha256"]),
                "room_settings_version": int(room_record["version"]),
                "room_snapshot_sha256": str(room_record["snapshot_sha256"]),
                "objective": clean_objective,
                "request_state_version": expected_version,
                "state": "DRAFT",
                "formal_round_created": False,
                "provider_calls_performed": 0,
                "market_calls_performed": 0,
                "execution_capability": "none",
                "user_confirmation_required_to_launch": True,
                "created_by": clean_actor,
                "created_at": timestamp,
            }
            draft_sha256 = canonical_sha256(draft)
            draft["draft_sha256"] = draft_sha256
            connection.execute(
                """INSERT INTO source_inbox_round_drafts(
                       id,record_version,item_id,room_id,attachment_id,room_snapshot_sha256,
                       objective,draft_json,draft_sha256,formal_round_created,
                       provider_calls_performed,market_calls_performed,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    draft_id,
                    SOURCE_INBOX_ROUND_DRAFT_VERSION,
                    clean_item_id,
                    clean_room_id,
                    attachment["id"],
                    room_record["snapshot_sha256"],
                    clean_objective,
                    _canonical_json(draft),
                    draft_sha256,
                    0,
                    0,
                    0,
                    clean_actor,
                    timestamp,
                ),
            )
            cursor = connection.execute(
                """UPDATE source_inbox_items
                   SET state=?,state_version=state_version+1,updated_at=?
                   WHERE id=? AND state_version=?""",
                (SOURCE_STATUS_ROUND_DRAFTED, timestamp, clean_item_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise SourceInboxError(
                    "来源条目状态已变化，请刷新后重试。",
                    code="SOURCE_INBOX_STATE_CONFLICT",
                    status=409,
                )
            self._append_event(
                connection,
                item_id=clean_item_id,
                event_type="ROUND_DRAFT_CREATED",
                from_state=state,
                to_state=SOURCE_STATUS_ROUND_DRAFTED,
                actor=clean_actor,
                payload={
                    "draft_id": draft_id,
                    "draft_sha256": draft_sha256,
                    "formal_round_created": False,
                    "provider_calls_performed": 0,
                    "market_calls_performed": 0,
                },
                created_at=timestamp,
            )
            updated = self._select_item(connection, clean_item_id)
            stored_draft = connection.execute(
                "SELECT * FROM source_inbox_round_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            assert updated is not None and stored_draft is not None
            final_item = self._item_projection(connection, updated, include_events=True)
            final_draft = next(
                entry for entry in final_item["round_drafts"]
                if entry["id"] == draft_id
            )
            return {
                "round_draft": final_draft,
                "item": final_item,
                "idempotent_replay": False,
            }


__all__ = [
    "SOURCE_INBOX_ATTACHMENT_VERSION",
    "SOURCE_INBOX_EVENT_VERSION",
    "SOURCE_INBOX_IMPORT_RECORD_VERSION",
    "SOURCE_INBOX_IMPORT_RESULT_VERSION",
    "SOURCE_INBOX_ITEM_RECORD_VERSION",
    "SOURCE_INBOX_LIST_VERSION",
    "SOURCE_INBOX_ROUND_DRAFT_VERSION",
    "SourceInboxError",
    "SourceInboxService",
    "ensure_source_inbox_schema",
]
