"""事件存储测试：链式哈希、幂等、持久化与篡改检测。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from service.store import EventStore, IdempotencyError, digest


class EventStoreTest(unittest.TestCase):
    def test_chain_hashes_link(self) -> None:
        store = EventStore()
        e1 = store.append("a", {"x": 1})
        e2 = store.append("b", {"y": 2})
        self.assertEqual(e1.prev_hash, "GENESIS")
        self.assertEqual(e2.prev_hash, e1.event_hash)
        self.assertEqual(store.verify_chain(), [])
        self.assertEqual(store.head_hash(), e2.event_hash)

    def test_idempotency_returns_same_event(self) -> None:
        store = EventStore()
        e1 = store.append("a", {"x": 1}, idem_key="k1")
        e2 = store.append("a", {"x": 1}, idem_key="k1")
        self.assertIs(e1, e2)
        self.assertEqual(len(store.all()), 1)

    def test_idempotency_conflict_rejected(self) -> None:
        store = EventStore()
        store.append("a", {"x": 1}, idem_key="k1")
        with self.assertRaises(IdempotencyError):
            store.append("a", {"x": 2}, idem_key="k1")
        with self.assertRaises(IdempotencyError):
            store.append("b", {"x": 1}, idem_key="k1")

    def test_persistence_roundtrip(self) -> None:
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            store = EventStore(path)
            store.append("a", {"x": 1})
            store.append("b", {"y": 2}, idem_key="k")
            store.append("b", {"y": 2}, idem_key="k")
            head = store.head_hash()

            reloaded = EventStore(path)
            self.assertEqual(reloaded.head_hash(), head)
            self.assertEqual(reloaded.verify_chain(), [])
            self.assertEqual(len(reloaded.all()), 2)
            self.assertIsNotNone(reloaded.append("b", {"y": 2}, idem_key="k"))
        finally:
            os.remove(path)

    def test_tamper_is_detected(self) -> None:
        path = tempfile.mktemp(suffix=".jsonl")
        try:
            store = EventStore(path)
            store.append("a", {"x": 1})
            store.append("b", {"y": 2})
            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            obj = json.loads(lines[0])
            obj["payload"]["x"] = 999
            lines[0] = json.dumps(obj) + "\n"
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(lines)
            problems = EventStore(path).verify_chain()
            self.assertTrue(problems)
        finally:
            os.remove(path)

    def test_digest_is_stable_and_order_independent(self) -> None:
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))
        self.assertNotEqual(digest({"a": 1}), digest({"a": 2}))


if __name__ == "__main__":
    unittest.main()
