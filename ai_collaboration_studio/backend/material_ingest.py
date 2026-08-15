from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import re
import socket
import ssl
import time
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from .market.earnings_materials import curated_official_material_candidate
from .market.ir_releases import IR_FEEDS
from .store import OFFICIAL_MATERIAL_ELIGIBLE_ERROR_CODES, STORE, StudioStore


MAX_FILE_BYTES = 2_000_000
MAX_FETCH_BYTES = 1_500_000
MAX_EXTRACTED_CHARS = 50_000
MAX_DOCX_UNCOMPRESSED_BYTES = 12_000_000
MAX_PDF_PAGES = 80
MAX_REDIRECTS = 5
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
ALLOWED_WEB_MIME_TYPES = {
    "text/html",
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/tab-separated-values",
    "application/json",
    "application/xml",
    "text/xml",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".html", ".htm", ".xml"}


def current_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class FetchedResource:
    raw: bytes
    content_type: str
    final_url: str
    declared_length: int = 0


@dataclass(frozen=True, slots=True)
class PinnedAddress:
    family: int
    ip: str
    sockaddr: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ValidatedHttpTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    request_target: str
    host_header: str
    addresses: tuple[PinnedAddress, ...]


@dataclass(slots=True)
class ExtractedMaterial:
    text: str
    title: str
    metadata: dict[str, Any]


class VisibleHTMLParser(HTMLParser):
    ignored_tags = {"script", "style", "noscript", "template", "svg"}
    block_tags = {
        "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
        "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "nav",
        "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        clean_tag = tag.lower()
        if clean_tag in self.ignored_tags:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if clean_tag == "title":
            self._in_title = True
        if clean_tag in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        clean_tag = tag.lower()
        if clean_tag in self.ignored_tags and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if clean_tag == "title":
            self._in_title = False
        if clean_tag in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.parts.append(data)


def normalize_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = "".join(char for char in text if char in {"\n", "\t"} or ord(char) >= 32)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    output: list[str] = []
    blank = False
    for line in lines:
        if line:
            output.append(line)
            blank = False
        elif output and not blank:
            output.append("")
            blank = True
    return "\n".join(output).strip()


def decode_text(raw: bytes, charset: str = "") -> str:
    candidates = [charset.strip().lower(), "utf-8-sig", "utf-16", "gb18030"]
    for encoding in dict.fromkeys(candidate for candidate in candidates if candidate):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise ValueError("文件文本编码无法识别；请转换为 UTF-8、UTF-16 或 GB18030")


def parse_content_type(value: str) -> tuple[str, str]:
    parts = [part.strip() for part in str(value or "").split(";")]
    mime_type = parts[0].lower()
    charset = ""
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip('"')
    return mime_type, charset


def extract_html(raw: bytes, charset: str = "") -> tuple[str, str]:
    parser = VisibleHTMLParser()
    parser.feed(decode_text(raw, charset))
    parser.close()
    return normalize_text("".join(parser.parts)), normalize_text(" ".join(parser.title_parts))[:120]


def extract_docx(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            total_uncompressed = sum(item.file_size for item in archive.infolist())
            if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("DOCX 解压后内容过大")
            try:
                document_xml = archive.read("word/document.xml")
            except KeyError as exc:
                raise ValueError("DOCX 缺少正文结构") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 文件结构无效") from exc
    root = ElementTree.fromstring(document_xml)
    paragraphs: list[str] = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        parts = [node.text or "" for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return normalize_text("\n".join(paragraphs))


def extract_pdf(raw: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF 解析需要先安装 requirements.txt 中的 pypdf") from exc
    try:
        reader = PdfReader(BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise ValueError("暂不支持加密 PDF")
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise ValueError(f"PDF 页数超过上限 {MAX_PDF_PAGES}")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"PDF 文本解析失败：{str(exc)[:160]}") from exc
    text = normalize_text("\n\n".join(pages))
    if not text:
        raise ValueError("PDF 没有可提取文本，可能是扫描件；当前版本不进行 OCR")
    return text, page_count


def extract_material(raw: bytes, filename: str, content_type: str = "") -> ExtractedMaterial:
    if not raw:
        raise ValueError("文件内容为空")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"文件超过 {MAX_FILE_BYTES // 1_000_000} MB 上限")
    safe_name = Path(filename or "material.txt").name[:180]
    extension = Path(safe_name).suffix.lower()
    mime_type, charset = parse_content_type(content_type)
    if not mime_type:
        mime_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    title = Path(safe_name).stem[:120]
    extra: dict[str, Any] = {}

    if extension in {".html", ".htm"} or mime_type == "text/html":
        text, html_title = extract_html(raw, charset)
        title = html_title or title
        method = "html_visible_text"
    elif extension == ".docx" or mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        text = extract_docx(raw)
        method = "docx_xml"
    elif extension == ".pdf" or mime_type == "application/pdf":
        text, page_count = extract_pdf(raw)
        extra["page_count"] = page_count
        method = "pypdf_text"
    elif extension == ".json" or mime_type == "application/json":
        decoded = decode_text(raw, charset)
        try:
            text = json.dumps(json.loads(decoded), ensure_ascii=False, indent=2)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 文件格式无效") from exc
        method = "json_pretty_text"
    elif extension in TEXT_EXTENSIONS or mime_type.startswith("text/") or mime_type in {"application/xml"}:
        text = normalize_text(decode_text(raw, charset))
        method = "decoded_text"
    else:
        raise ValueError("暂不支持此文件类型；当前支持 TXT、MD、CSV、TSV、JSON、HTML、XML、DOCX、PDF")

    if not text.strip():
        raise ValueError("没有提取到可供 AI 使用的文本")
    truncated = len(text) > MAX_EXTRACTED_CHARS
    text = text[:MAX_EXTRACTED_CHARS].rstrip()
    metadata = {
        "original_name": safe_name,
        "content_type": mime_type,
        "source_bytes": len(raw),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "extraction_method": method,
        "extracted_at": current_ms(),
        "truncated": truncated,
        **extra,
    }
    return ExtractedMaterial(text=text, title=title or "未命名资料", metadata=metadata)


def _host_header(hostname: str, port: int, scheme: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        rendered_host = hostname
    else:
        rendered_host = f"[{address}]" if address.version == 6 else str(address)
    default_port = 443 if scheme == "https" else 80
    return rendered_host if port == default_port else f"{rendered_host}:{port}"


def _resolve_public_http_target(
    value: str,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> ValidatedHttpTarget:
    raw_url = str(value or "").strip()
    if len(raw_url) > 2000:
        raise ValueError("网页链接过长")
    parsed = urlsplit(raw_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("只允许抓取 http:// 或 https:// 网页")
    if not parsed.hostname:
        raise ValueError("网页链接缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("网页链接不能包含用户名或密码")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("不能抓取本机或内网地址")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("网页端口格式无效") from exc
    if port not in {80, 443}:
        raise ValueError("网页抓取只允许标准 80/443 端口")

    resolved_addresses: list[tuple[int, ipaddress.IPv4Address | ipaddress.IPv6Address]] = []
    try:
        literal_ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            hostname = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("网页域名格式无效") from exc
        try:
            resolved = resolver(hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("网页域名无法解析") from exc
        for item in resolved:
            try:
                family = int(item[0])
                if family not in {socket.AF_INET, socket.AF_INET6}:
                    continue
                address = ipaddress.ip_address(str(item[4][0]).split("%", 1)[0])
            except (IndexError, TypeError, ValueError):
                continue
            if (family == socket.AF_INET and address.version != 4) or (
                family == socket.AF_INET6 and address.version != 6
            ):
                continue
            resolved_addresses.append((family, address))
    else:
        if "%" in host:
            raise ValueError("不能抓取带区域标识的地址")
        hostname = str(literal_ip)
        family = socket.AF_INET if literal_ip.version == 4 else socket.AF_INET6
        resolved_addresses.append((family, literal_ip))

    if not resolved_addresses or any(not address.is_global for _family, address in resolved_addresses):
        raise ValueError("不能抓取本机、私网、链路本地或保留地址")

    pinned_addresses: list[PinnedAddress] = []
    seen: set[tuple[int, str]] = set()
    for family, address in resolved_addresses:
        identity = (family, str(address))
        if identity in seen:
            continue
        seen.add(identity)
        sockaddr: tuple[Any, ...] = (
            (str(address), port)
            if family == socket.AF_INET
            else (str(address), port, 0, 0)
        )
        pinned_addresses.append(PinnedAddress(family=family, ip=str(address), sockaddr=sockaddr))

    normalized_url = urlunsplit((scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return ValidatedHttpTarget(
        url=normalized_url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        request_target=request_target,
        host_header=_host_header(hostname, port, scheme),
        addresses=tuple(pinned_addresses),
    )


def validate_public_http_url(
    value: str,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    return _resolve_public_http_target(value, resolver).url


class _OwnedHTTPResponse:
    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self._connection = connection
        self._response = response

    @property
    def status(self) -> int:
        return int(self._response.status)

    @property
    def headers(self) -> Any:
        return self._response.headers

    def read(self, amount: int) -> bytes:
        return self._response.read(amount)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


def _open_pinned_response(
    target: ValidatedHttpTarget,
    address: PinnedAddress,
    headers: dict[str, str],
    timeout: float,
) -> _OwnedHTTPResponse:
    connected_socket = socket.socket(address.family, socket.SOCK_STREAM)
    connection: http.client.HTTPConnection | None = None
    try:
        connected_socket.settimeout(timeout)
        connected_socket.connect(address.sockaddr)
        if target.scheme == "https":
            context = ssl.create_default_context()
            connected_socket = context.wrap_socket(
                connected_socket,
                server_hostname=target.hostname,
            )
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=timeout)
        connection.sock = connected_socket
        connection.request("GET", target.request_target, headers=headers)
        return _OwnedHTTPResponse(connection, connection.getresponse())
    except BaseException:
        if connection is not None:
            connection.close()
        else:
            connected_socket.close()
        raise


def _close_response(response: Any) -> None:
    try:
        response.close()
    except OSError:
        pass


def _response_header(response: Any, name: str) -> str:
    return str(response.headers.get(name) or "")


def fetch_public_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    transport: Callable[[ValidatedHttpTarget, PinnedAddress, dict[str, str], float], Any] | None = None,
) -> FetchedResource:
    open_response = transport or _open_pinned_response
    current_url = str(url or "")
    base_headers = {
        "Accept": "text/html,text/plain,application/json,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document;q=0.8",
        "Connection": "close",
        "User-Agent": "AICollaborationStudio-MaterialFetcher/0.3",
    }

    for redirect_count in range(MAX_REDIRECTS + 1):
        target = _resolve_public_http_target(current_url, resolver)
        headers = {**base_headers, "Host": target.host_header}
        response: Any | None = None
        last_error: BaseException | None = None
        for address in target.addresses:
            try:
                response = open_response(target, address, headers, 20.0)
                break
            except (OSError, http.client.HTTPException) as exc:
                last_error = exc
        if response is None:
            detail = str(last_error or "连接失败")[:160]
            raise ValueError(f"网页连接失败：{detail}") from last_error

        next_url = ""
        try:
            status = int(response.status)
            if status in REDIRECT_STATUS_CODES:
                location = _response_header(response, "Location").strip()
                if not location:
                    raise ValueError(f"网页抓取失败：HTTP {status} 重定向缺少 Location")
                if redirect_count >= MAX_REDIRECTS:
                    raise ValueError("网页重定向次数超过上限")
                next_url = urljoin(target.url, location)
            elif status < 200 or status >= 300:
                raise ValueError(f"网页抓取失败：HTTP {status}")
            else:
                content_type = _response_header(response, "Content-Type")
                mime_type, _charset = parse_content_type(content_type)
                if mime_type not in ALLOWED_WEB_MIME_TYPES and not mime_type.startswith("text/"):
                    raise ValueError(f"网页内容类型不受支持：{mime_type or '未知'}")
                try:
                    declared_length = int(_response_header(response, "Content-Length") or 0)
                except (TypeError, ValueError):
                    declared_length = 0
                if declared_length > MAX_FETCH_BYTES:
                    raise ValueError("网页响应超过 1.5 MB 上限")
                raw = response.read(MAX_FETCH_BYTES + 1)
        finally:
            _close_response(response)

        if next_url:
            current_url = next_url
            continue
        if len(raw) > MAX_FETCH_BYTES:
            raise ValueError("网页响应超过 1.5 MB 上限")
        return FetchedResource(
            raw=raw,
            content_type=content_type,
            final_url=target.url,
            declared_length=max(0, declared_length),
        )

    raise ValueError("网页重定向次数超过上限")


class MaterialIngestService:
    def __init__(
        self,
        store: StudioStore = STORE,
        fetcher: Callable[[str], FetchedResource] = fetch_public_url,
    ) -> None:
        self.store = store
        self.fetcher = fetcher

    def _validate_update_precondition(
        self,
        room_id: str,
        material_id: str,
        payload: dict[str, Any],
    ) -> int | None:
        if not material_id:
            return None
        if "expected_version" not in payload:
            raise ValueError("替换资料必须提供 expected_version")
        try:
            expected_version = int(payload.get("expected_version"))
        except (TypeError, ValueError) as exc:
            raise ValueError("资料版本必须是整数") from exc
        current = self.store.get_material(room_id, material_id)
        if not current:
            raise ValueError("房间或资料不存在")
        if expected_version != int(current.get("version") or 1):
            raise ValueError("资料版本已变化，请刷新后再替换")
        return expected_version

    def import_file(self, room_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        material_id = str(payload.get("material_id") or "").strip()
        expected_version = self._validate_update_precondition(room_id, material_id, payload)
        supplement = payload.get("official_supplement")
        candidate: dict[str, Any] | None = None
        if supplement is not None:
            if not isinstance(supplement, dict) or supplement.get("version") != "official_supplement_v1":
                raise ValueError("官方材料补充声明版本无效")
            if supplement.get("user_confirmed") is not True:
                raise ValueError("必须由用户显式确认本次官方材料补充预览")
            candidate = curated_official_material_candidate(
                str(supplement.get("symbol") or ""),
                str(supplement.get("official_url") or ""),
            )
            if not candidate:
                raise ValueError("官方材料补充只接受内置目录中的精确 HTTPS URL")
            for supplied_key in ("fiscal_period", "material_kind"):
                supplied_value = str(supplement.get(supplied_key) or "").strip()
                if supplied_value and supplied_value != str(candidate.get(supplied_key) or ""):
                    raise ValueError(f"官方材料补充的 {supplied_key} 与内置精确候选不一致")
            raw_error_codes = supplement.get("original_error_codes")
            if not isinstance(raw_error_codes, list) or not raw_error_codes:
                raise ValueError("官方材料补充必须绑定 ACCESS_TIMEOUT 或 ACCESS_ERROR")
            normalized_error_codes = [
                str(code or "").strip().upper()
                for code in raw_error_codes
                if str(code or "").strip()
            ]
            if (
                not normalized_error_codes
                or any(code not in OFFICIAL_MATERIAL_ELIGIBLE_ERROR_CODES for code in normalized_error_codes)
            ):
                raise ValueError("官方材料补充不能覆盖 SEC、IR、Futu、FRED 或其他来源错误")
        encoded = str(payload.get("content_base64") or "")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("文件内容不是有效 Base64") from exc
        filename = Path(str(payload.get("filename") or "material.txt")).name
        extracted = extract_material(raw, filename, str(payload.get("content_type") or ""))
        title = str(payload.get("title") or extracted.title).strip()[:120]
        event_metadata = self._event_metadata(payload.get("metadata"))
        source_url = ""
        if supplement is not None and candidate is not None:
            symbol = str(candidate.get("symbol") or "").strip().upper()
            title = str(candidate.get("title") or "Official earnings material").strip()[:120]
            source_url = str(candidate.get("official_url") or "")
            event_metadata = {
                "source_type": "company_ir",
                "event_type": "earnings",
                "publisher": str((IR_FEEDS.get(symbol) or {}).get("publisher") or ""),
                "symbols": [symbol],
                "fiscal_period": str(candidate.get("fiscal_period") or ""),
                "claim_status": "user_attested_official_copy",
            }
        material_payload = {
            "title": title,
            "kind": "file_excerpt",
            "source_url": source_url,
            "content": extracted.text,
            "metadata": {**extracted.metadata, **event_metadata},
        }
        if supplement is not None:
            if bool(material_payload["metadata"].get("truncated")):
                raise ValueError("被截断的上传文件不能补充官方材料门禁")
            if self.store._material_prompt_injection_flags(material_payload):
                raise ValueError("命中提示注入隔离的文件不能补充官方材料门禁")
        material = (
            self.store.update_material(
                room_id,
                material_id,
                {**material_payload, "expected_version": expected_version},
                official_supplement_pending=supplement is not None,
            )
            if material_id
            else self.store.add_material(
                room_id,
                material_payload,
                official_supplement_pending=supplement is not None,
            )
        )
        if not material:
            raise ValueError("房间或资料不存在")
        if supplement is not None:
            material["_official_attestation"] = self.store.stage_material_official_attestation(
                room_id,
                str(material.get("id") or ""),
                supplement,
                expected_material_version=int(material.get("version") or 0),
                expected_material_snapshot_sha256=self.store._material_snapshot_sha256(material),
            )
        return material

    def fetch_url(self, room_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        material_id = str(payload.get("material_id") or "").strip()
        expected_version = self._validate_update_precondition(room_id, material_id, payload)
        source_url = str(payload.get("url") or payload.get("source_url") or "").strip()
        resource = self.fetcher(source_url)
        final_path = unquote(PurePosixPath(urlsplit(resource.final_url).path).name)
        mime_type, _charset = parse_content_type(resource.content_type)
        if not final_path or "." not in final_path:
            final_path = "page.html" if mime_type == "text/html" else "material.txt"
        extracted = extract_material(resource.raw, final_path, resource.content_type)
        metadata = {
            **extracted.metadata,
            "original_url": source_url,
            "final_url": resource.final_url,
            "fetched_at": current_ms(),
            **self._event_metadata(payload.get("metadata")),
        }
        metadata.setdefault("publisher", urlsplit(resource.final_url).hostname or "")
        title = str(payload.get("title") or extracted.title or urlsplit(resource.final_url).hostname or "网页资料").strip()[:120]
        material_payload = {
            "title": title,
            "kind": "url",
            "source_url": source_url,
            "content": extracted.text,
            "metadata": metadata,
        }
        material = (
            self.store.update_material(
                room_id,
                material_id,
                {**material_payload, "expected_version": expected_version},
            )
            if material_id
            else self.store.add_material(room_id, material_payload)
        )
        if not material:
            raise ValueError("房间或资料不存在")
        return material

    @staticmethod
    def _event_metadata(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        clean: dict[str, Any] = {}
        for key in ("source_type", "event_type", "publisher", "published_at"):
            if key in value:
                clean[key] = value.get(key)
        if isinstance(value.get("symbols"), list):
            clean["symbols"] = value.get("symbols")
        return clean


MATERIAL_INGEST = MaterialIngestService()
