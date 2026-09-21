"""只追加事件存储与哈希链。

存储为 JSON 行文件（每行一个事件），进程内使用内存列表。
每条事件包含前一条事件的哈希，形成哈希链；账本头部暴露
``root_hash``，供各方核对“看到的是同一本账”。

``event_id`` 是提交方提供的幂等键：重复核证回执、网络重试等
重复提交只会取回首次落账的事件，不会产生第二条流水。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any

from .events import EVENT_AUTHORIZED_ROLES, Event

_GENESIS_HASH = "0" * 64


def _canonical_json(data: Any) -> str:
    """以稳定顺序序列化，保证哈希可跨进程复现。"""

    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_event(
    *,
    event_id: str,
    type_: str,
    payload: dict[str, Any],
    occurred_at: str,
    actor: str,
    prev_hash: str,
) -> str:
    body = _canonical_json(
        {
            "event_id": event_id,
            "type": type_,
            "payload": payload,
            "occurred_at": occurred_at,
            "actor": actor,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class AuthorizationError(PermissionError):
    """提交角色无权写入该类事件。"""


class EventStore:
    """线程安全的只追加事件存储。"""

    def __init__(self, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._index: dict[str, Event] = {}
        self._path = path
        if path and os.path.exists(path):
            self._load(path)

    # ------------------------------------------------------------------ 读取

    @property
    def root_hash(self) -> str:
        """账本当前哈希根（末条事件哈希；空账为创世哈希）。"""

        with self._lock:
            if not self._events:
                return _GENESIS_HASH
            return self._events[-1].event_hash

    def events(self, *, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.seq > after_seq]

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            return self._index.get(event_id)

    def snapshot_all(self) -> list[dict[str, Any]]:
        """导出全部事件（管理/审计用途）。"""

        with self._lock:
            return [event_to_dict(e) for e in self._events]

    # ------------------------------------------------------------------ 写入

    def append(
        self,
        *,
        event_id: str,
        type_: str,
        payload: dict[str, Any],
        occurred_at: str,
        actor: str,
    ) -> tuple[Event, bool]:
        """追加事件。

        返回 ``(事件, 是否新建)``。同一 ``event_id`` 重复提交时返回
        既有事件与 ``False``——重复核证回执等场景不会重复入账。
        角色不符抛 :class:`AuthorizationError`。
        """

        allowed = EVENT_AUTHORIZED_ROLES.get(type_)
        if allowed is None:
            raise ValueError(f"未知事件类型: {type_}")
        if actor not in allowed:
            raise AuthorizationError(f"角色 {actor} 无权提交 {type_}")

        with self._lock:
            existing = self._index.get(event_id)
            if existing is not None:
                # 同编号必须是同内容、同提交角色的重放；否则是编号撞车，拒绝。
                # occurred_at 不参与比较（重放时间由服务端生成）。
                if (
                    existing.type != type_
                    or existing.actor != actor
                    or existing.payload != dict(payload)
                ):
                    raise ValueError(f"event_id 已用于不同事件: {event_id}")
                return existing, False

            prev_hash = self._events[-1].event_hash if self._events else _GENESIS_HASH
            event_hash = digest_event(
                event_id=event_id,
                type_=type_,
                payload=payload,
                occurred_at=occurred_at,
                actor=actor,
                prev_hash=prev_hash,
            )
            event = Event(
                seq=len(self._events) + 1,
                event_id=event_id,
                type=type_,  # type: ignore[arg-type]
                payload=dict(payload),
                occurred_at=occurred_at,
                actor=actor,
                prev_hash=prev_hash,
                event_hash=event_hash,
            )
            self._events.append(event)
            self._index[event_id] = event
            if self._path:
                self._append_line(event)
            return event, True

    # ------------------------------------------------------------------ 持久化

    def _append_line(self, event: Event) -> None:
        assert self._path is not None
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")

    def _load(self, path: str) -> None:
        prev = _GENESIS_HASH
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                event = Event(
                    seq=raw["seq"],
                    event_id=raw["event_id"],
                    type=raw["type"],
                    payload=raw["payload"],
                    occurred_at=raw["occurred_at"],
                    actor=raw["actor"],
                    prev_hash=raw["prev_hash"],
                    event_hash=raw["event_hash"],
                )
                if event.prev_hash != prev:
                    raise ValueError("事件文件哈希链断裂")
                expected = digest_event(
                    event_id=event.event_id,
                    type_=event.type,
                    payload=event.payload,
                    occurred_at=event.occurred_at,
                    actor=event.actor,
                    prev_hash=event.prev_hash,
                )
                if expected != event.event_hash:
                    raise ValueError(f"事件 {event.event_id} 哈希校验失败")
                self._events.append(event)
                self._index[event.event_id] = event
                prev = event.event_hash


def event_to_dict(event: Event) -> dict[str, Any]:
    return {
        "seq": event.seq,
        "event_id": event.event_id,
        "type": event.type,
        "payload": event.payload,
        "occurred_at": event.occurred_at,
        "actor": event.actor,
        "prev_hash": event.prev_hash,
        "event_hash": event.event_hash,
    }
