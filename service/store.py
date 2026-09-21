"""只追加事件存储：链式哈希、幂等键、JSONL 快照持久化。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone

from .models import Event


def utc_now() -> str:
    """返回 UTC ISO-8601 时间戳。"""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(data: object) -> str:
    """对结构稳定的 JSON 序列化，用于哈希。"""

    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(payload: object) -> str:
    """计算内容的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class IdempotencyError(ValueError):
    """幂等键重复但参数不一致。"""


class EventStore:
    """内存事件日志，可选 JSONL 持久化。

    事件按 seq 单调递增并以 prev_hash 串链，任何篡改都会破坏链哈希。
    同一 idem_key 的重复提交返回原事件；参数不一致则报错。
    """

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._idem: dict[str, Event] = {}
        self._path = path
        if path and os.path.exists(path):
            self._load(path)

    # ------------------------------------------------------------------
    # 追加 / 查询
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        idem_key: str | None = None,
        event_time: str | None = None,
        event_id: str | None = None,
    ) -> Event:
        with self._lock:
            if idem_key is not None and idem_key in self._idem:
                existing = self._idem[idem_key]
                if existing.event_type != event_type or existing.payload != payload:
                    raise IdempotencyError(
                        f"幂等键 {idem_key} 已用于不同内容的事件 {existing.event_id}"
                    )
                return existing

            seq = len(self._events) + 1
            prev_hash = self._events[-1].event_hash if self._events else "GENESIS"
            eid = event_id or f"evt_{uuid.uuid4().hex[:16]}"
            etime = event_time or utc_now()
            eh = digest(
                {
                    "seq": seq,
                    "event_id": eid,
                    "event_type": event_type,
                    "event_time": etime,
                    "payload": payload,
                    "prev_hash": prev_hash,
                    "idem_key": idem_key,
                }
            )
            event = Event(
                seq=seq,
                event_id=eid,
                event_type=event_type,
                event_time=etime,
                payload=payload,
                prev_hash=prev_hash,
                event_hash=eh,
                idem_key=idem_key,
            )
            self._events.append(event)
            if idem_key is not None:
                self._idem[idem_key] = event
            if self._path:
                self._append_line(event)
            return event

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def head_hash(self) -> str:
        with self._lock:
            return self._events[-1].event_hash if self._events else "GENESIS"

    # ------------------------------------------------------------------
    # 完整性校验与持久化
    # ------------------------------------------------------------------

    def verify_chain(self) -> list[str]:
        """重算链哈希，返回发现的问题列表（空列表表示完好）。"""

        problems: list[str] = []
        prev = "GENESIS"
        seen_idem: dict[str, str] = {}
        with self._lock:
            for event in self._events:
                if event.prev_hash != prev:
                    problems.append(f"seq={event.seq} 前序哈希不匹配")
                expected = digest(
                    {
                        "seq": event.seq,
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "event_time": event.event_time,
                        "payload": event.payload,
                        "prev_hash": event.prev_hash,
                        "idem_key": event.idem_key,
                    }
                )
                if expected != event.event_hash:
                    problems.append(f"seq={event.seq} 事件哈希不匹配")
                if event.idem_key:
                    if event.idem_key in seen_idem:
                        problems.append(
                            f"seq={event.seq} 幂等键 {event.idem_key} 重复"
                        )
                    seen_idem[event.idem_key] = event.event_id
                prev = event.event_hash
        return problems

    def _append_line(self, event: Event) -> None:
        assert self._path is not None
        line = canonical_json(
            {
                "seq": event.seq,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "event_time": event.event_time,
                "payload": event.payload,
                "prev_hash": event.prev_hash,
                "event_hash": event.event_hash,
                "idem_key": event.idem_key,
            }
        )
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _load(self, path: str) -> None:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                event = Event(**raw)
                self._events.append(event)
                if event.idem_key:
                    self._idem[event.idem_key] = event
