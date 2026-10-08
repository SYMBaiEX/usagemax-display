"""Small, dependency-free reader for official OTLP JSON and JSON Lines files."""

from __future__ import annotations

import glob
import gzip
import json
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OtelRecord:
    timestamp: float
    service: str
    name: str
    duration_ms: int | None = None
    status: str = "OK"
    kind: str = "SPAN"
    trace_id: str = ""
    span_id: str = ""
    model: str = ""
    host: str = ""
    session_id: str = ""

    @property
    def identity(self) -> tuple:
        return (self.host, self.service, self.session_id, self.trace_id, self.span_id, self.name, self.timestamp, self.kind)


def _value(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    for key in (
        "stringValue", "intValue", "doubleValue", "boolValue", "bytesValue",
    ):
        if key in payload:
            return payload[key]
    return payload


def _attributes(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("key", "")): _value(item.get("value"))
        for item in items
        if isinstance(item, dict) and item.get("key")
    }


def _seconds(value: Any) -> float:
    try:
        return int(str(value or "0")) / 1_000_000_000
    except (TypeError, ValueError):
        return 0.0


def _status(value: Any, severity: str = "") -> str:
    code = value.get("code", value) if isinstance(value, dict) else value
    normalized = str(code).upper()
    severity = severity.upper()
    if normalized in {"2", "STATUS_CODE_ERROR", "ERROR"} or "ERROR" in severity or "FATAL" in severity:
        return "ERR"
    if "WARN" in severity:
        return "WRN"
    return "OK"


def _service(resource: dict[str, Any], attributes: dict[str, Any], fallback: str) -> str:
    return str(
        attributes.get("agent.name")
        or attributes.get("gen_ai.agent.name")
        or resource.get("service.name")
        or resource.get("host.name")
        or fallback
        or "OTEL"
    )


def parse_otlp_json(payload: dict[str, Any], fallback_service: str = "OTEL") -> list[OtelRecord]:
    """Normalize OTLP/JSON TracesData and LogsData into compact HUD records."""
    records: list[OtelRecord] = []
    for resource_spans in payload.get("resourceSpans", []):
        if not isinstance(resource_spans, dict):
            continue
        resource = _attributes(resource_spans.get("resource", {}).get("attributes", []))
        for scope_spans in resource_spans.get("scopeSpans", []):
            if not isinstance(scope_spans, dict):
                continue
            scope = scope_spans.get("scope", {})
            scope_name = str(scope.get("name", fallback_service)) if isinstance(scope, dict) else fallback_service
            for span in scope_spans.get("spans", []):
                if not isinstance(span, dict):
                    continue
                attributes = _attributes(span.get("attributes", []))
                started = _seconds(span.get("startTimeUnixNano"))
                ended = _seconds(span.get("endTimeUnixNano"))
                timestamp = ended or started or time.time()
                duration = max(0, int(round((ended - started) * 1000))) if ended and started and ended >= started else None
                records.append(OtelRecord(
                    timestamp=timestamp,
                    service=_service(resource, attributes, scope_name),
                    name=str(span.get("name", "otel.span"))[:96],
                    duration_ms=duration,
                    status=_status(span.get("status", {})),
                    kind="SPAN",
                    trace_id=str(span.get("traceId", "")),
                    span_id=str(span.get("spanId", "")),
                    model=str(attributes.get("gen_ai.request.model", "")),
                    host=str(resource.get("host.name") or attributes.get("hud.source") or fallback_service),
                    session_id=str(attributes.get("gen_ai.conversation.id") or attributes.get("session.id") or ""),
                ))

    for resource_logs in payload.get("resourceLogs", []):
        if not isinstance(resource_logs, dict):
            continue
        resource = _attributes(resource_logs.get("resource", {}).get("attributes", []))
        for scope_logs in resource_logs.get("scopeLogs", []):
            if not isinstance(scope_logs, dict):
                continue
            scope = scope_logs.get("scope", {})
            scope_name = str(scope.get("name", fallback_service)) if isinstance(scope, dict) else fallback_service
            for record in scope_logs.get("logRecords", []):
                if not isinstance(record, dict):
                    continue
                attributes = _attributes(record.get("attributes", []))
                body = _value(record.get("body", {}))
                name = str(attributes.get("event.name") or body or "otel.log")
                records.append(OtelRecord(
                    timestamp=_seconds(record.get("timeUnixNano") or record.get("observedTimeUnixNano")) or time.time(),
                    service=_service(resource, attributes, scope_name),
                    name=name[:96],
                    status=_status({}, str(record.get("severityText", ""))),
                    kind="LOG",
                    trace_id=str(record.get("traceId", "")),
                    span_id=str(record.get("spanId", "")),
                    model=str(attributes.get("gen_ai.request.model", "")),
                    host=str(resource.get("host.name") or attributes.get("hud.source") or fallback_service),
                    session_id=str(attributes.get("gen_ai.conversation.id") or attributes.get("session.id") or ""),
                ))
    return records


class OtelFileIngestor:
    """Read rotating or snapshot-style OTLP JSONL files without replaying records."""

    def __init__(self, patterns: Iterable[str | Path], max_records: int = 4096) -> None:
        self.patterns = tuple(str(pattern) for pattern in patterns)
        self.records: deque[OtelRecord] = deque(maxlen=max_records)
        self.seen_order: deque[tuple[str, str, str, float, str]] = deque(maxlen=max_records * 8)
        self.seen: set[tuple[str, str, str, float, str]] = set()
        self.file_state: dict[Path, tuple[int, int]] = {}

    def _paths(self) -> list[Path]:
        found: set[Path] = set()
        for pattern in self.patterns:
            matches = glob.glob(pattern)
            if not matches and Path(pattern).is_file():
                matches = [pattern]
            found.update(Path(match) for match in matches)
        return sorted(found)

    def scan(self, now: float | None = None) -> tuple[OtelRecord, ...]:
        now = now or time.time()
        paths = self._paths()
        live_paths = set(paths)
        for stale in set(self.file_state) - live_paths:
            self.file_state.pop(stale, None)
        for path in paths:
            try:
                stat = path.stat()
                state = (stat.st_size, stat.st_mtime_ns)
                if self.file_state.get(path) == state:
                    continue
                with path.open("rb") as handle:
                    handle.seek(0, 2)
                    handle.seek(max(0, handle.tell() - 2_000_000))
                    lines = handle.read().decode("utf-8", "replace").splitlines()
            except (OSError, PermissionError):
                continue
            self.file_state[path] = state
            for line in lines:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                for record in parse_otlp_json(payload, path.stem):
                    if record.identity in self.seen:
                        continue
                    if len(self.seen_order) == self.seen_order.maxlen:
                        self.seen.discard(self.seen_order[0])
                    self.seen_order.append(record.identity)
                    self.seen.add(record.identity)
                    self.records.append(record)
        cutoff = now - 3600
        return tuple(record for record in self.records if record.timestamp >= cutoff)


class OtlpHttpReceiver:
    """Tiny loopback OTLP/HTTP JSON receiver for local agent runtimes."""

    def __init__(
        self, output: Path, host: str = "127.0.0.1", port: int = 4318,
        max_bytes: int = 2_000_000,
    ) -> None:
        self.output = output
        self.host = host
        self.port = port
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.records: deque[OtelRecord] = deque(maxlen=4096)
        self.version = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.status = "OFFLINE"
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _reply(self, status: int, body: bytes = b"{}") -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path not in {"/v1/traces", "/v1/logs"}:
                    self._reply(404)
                    return
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type != "application/json":
                    self._reply(415)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._reply(400)
                    return
                if length <= 0 or length > receiver.max_bytes:
                    self._reply(413)
                    return
                raw = self.rfile.read(length)
                if self.headers.get("Content-Encoding", "").lower() == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except (OSError, EOFError):
                        self._reply(400)
                        return
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._reply(400)
                    return
                expected = "resourceSpans" if self.path.endswith("traces") else "resourceLogs"
                if not isinstance(payload, dict) or expected not in payload:
                    self._reply(400)
                    return
                receiver._append(payload)
                self._reply(200)

        try:
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.server = ThreadingHTTPServer((self.host, self.port), Handler)
            self.port = int(self.server.server_address[1])
            self.server.daemon_threads = True
            self.thread = threading.Thread(
                target=self.server.serve_forever, name="otlp-http-receiver", daemon=True,
            )
            self.thread.start()
            self.status = "LIVE"
        except OSError:
            self.server = None
            self.status = "BUSY"

    def _append(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")) + "\n"
        with self.lock:
            received = parse_otlp_json(payload, "OTLP-HTTP")
            if received:
                self.records.extend(received)
                self.version += 1
            try:
                if self.output.exists() and self.output.stat().st_size > 8_000_000:
                    backup = self.output.with_suffix(self.output.suffix + ".1")
                    backup.unlink(missing_ok=True)
                    self.output.replace(backup)
                with self.output.open("a", encoding="utf-8") as handle:
                    handle.write(encoded)
            except OSError:
                self.status = "ERROR"

    def snapshot(self) -> tuple[int, tuple[OtelRecord, ...]]:
        """Expose newly received records without waiting for a file rescan."""
        with self.lock:
            return self.version, tuple(self.records)

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.server = None
        self.status = "OFFLINE"
