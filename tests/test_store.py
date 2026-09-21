"""事件存储：哈希链、幂等、持久化重载、角色写权限。"""

from __future__ import annotations

import os
import tempfile
import unittest

from service.ledger.engine import LedgerEngine
from service.ledger.store import AuthorizationError, EventStore
from tests._fixtures import build_confirmed_engine


class StoreTest(unittest.TestCase):
    def test_hash_chain_changes_with_each_append(self) -> None:
        store = EventStore()
        roots = [store.root_hash]
        eng = LedgerEngine(store)
        eng.append("method.registered", "m1", {"version": "M", "doc_hash": "h"}, actor="methodology_body")
        roots.append(store.root_hash)
        eng.append("baseline.registered", "b1", {"version": "B", "doc_hash": "h"}, actor="baseline_body")
        roots.append(store.root_hash)
        self.assertEqual(len(set(roots)), 3)
        # 链：后一条的 prev_hash 指向前一条。
        events = store.events()
        self.assertEqual(events[1].prev_hash, events[0].event_hash)

    def test_idempotent_event_id(self) -> None:
        store = EventStore()
        eng = LedgerEngine(store)
        e1, c1 = eng.append("method.registered", "m1", {"version": "M", "doc_hash": "h"}, actor="office")
        e2, c2 = eng.append("method.registered", "m1", {"version": "M", "doc_hash": "h"}, actor="office")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(e1.event_hash, e2.event_hash)
        self.assertEqual(len(store.events()), 1)

    def test_same_event_id_different_payload_rejected(self) -> None:
        from service.ledger.engine import LedgerError

        eng = LedgerEngine(EventStore())
        eng.append("method.registered", "m1", {"version": "M", "doc_hash": "h"}, actor="office")
        # 编号撞车（不同载荷）：拒绝，不新增事件。
        with self.assertRaises((LedgerError, ValueError)):
            eng.append("method.registered", "m1", {"version": "M2", "doc_hash": "h2"}, actor="office")
        self.assertEqual(len(eng.store.events()), 1)

    def test_wrong_role_rejected_before_persist(self) -> None:
        store = EventStore()
        eng = LedgerEngine(store)
        with self.assertRaises(AuthorizationError):
            eng.append("monitoring.submitted", "mon", {"x": 1}, actor="office")
        self.assertEqual(store.events(), [])

    def test_persistence_roundtrip_verifies_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.jsonl")
            eng = LedgerEngine(EventStore(path))
            a = lambda t, eid, p, actor="office": eng.append(t, eid, p, actor=actor)  # noqa: E731
            a("method.registered", "m1", {"version": "M-1", "doc_hash": "hm"})
            a("baseline.registered", "b1", {"version": "BL-1", "doc_hash": "hb"})
            a("boundary.changed", "bd1", {"version": "BND-1", "area_mu": 12, "doc_hash": "hd"})
            root = eng.store.root_hash

            reloaded = LedgerEngine(EventStore(path))
            self.assertEqual(reloaded.store.root_hash, root)
            self.assertEqual(reloaded.methods["M-1"]["doc_hash"], "hm")

            # 篡改文件应在加载时暴露。
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            evil = lines[0].replace("hm", "TAMPERED")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(evil + "".join(lines[1:]))
            with self.assertRaises(ValueError):
                EventStore(path)

    def test_rebuilt_engine_matches(self) -> None:
        eng = build_confirmed_engine()
        rebuilt = LedgerEngine(eng.store)
        self.assertEqual(rebuilt.participant_account("FA"), eng.participant_account("FA"))
        self.assertEqual(rebuilt.store.root_hash, eng.store.root_hash)


if __name__ == "__main__":
    unittest.main()
