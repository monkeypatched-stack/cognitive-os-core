"""MongoAuditStore: append-only enforced by a unique entry_id index (the insert refuses a
duplicate), with the old check-before-insert kept when the index cannot be built."""

import pytest
from pymongo.errors import DuplicateKeyError

from src.monkey_brain.kernel.audit import AuditPersistenceError, MongoAuditStore


class FakeCollection:
    def __init__(self, *, index_ok: bool = True) -> None:
        self.docs: list[dict] = []
        self.index_ok = index_ok
        self.unique = False
        self.find_one_calls = 0

    def create_index(self, key, **kw):
        if not self.index_ok:
            raise RuntimeError("cannot build index")
        assert key == "entry_id" and kw["unique"] and kw["partialFilterExpression"] == {"entry_id": {"$exists": True}}
        self.unique = True

    def insert_one(self, doc):
        if self.unique and any(d.get("entry_id") == doc.get("entry_id") for d in self.docs):
            raise DuplicateKeyError("E11000 duplicate key")
        self.docs.append(doc)

    def find_one(self, q):
        self.find_one_calls += 1
        return next((d for d in self.docs if d.get("entry_id") == q["entry_id"]), None)


def test_with_the_index_the_insert_itself_refuses_a_duplicate_and_nothing_is_scanned():
    col = FakeCollection()
    store = MongoAuditStore(collection=col)
    store.append("t", "execute", {"entry_id": "a", "event_type": "execute"})
    with pytest.raises(AuditPersistenceError):
        store.append("t", "execute", {"entry_id": "a", "event_type": "execute"})
    assert len(col.docs) == 1 and col.find_one_calls == 0 and col.docs[0]["_immutable"] is True


def test_without_the_index_append_still_checks_before_inserting():
    col = FakeCollection(index_ok=False)
    store = MongoAuditStore(collection=col)
    store.append("t", "execute", {"entry_id": "a"})
    with pytest.raises(AuditPersistenceError):
        store.append("t", "execute", {"entry_id": "a"})
    assert len(col.docs) == 1 and col.find_one_calls == 2
