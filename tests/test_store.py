from sendtox3.models import Item, new_item_id


def _item(**kw):
    base = dict(id=new_item_id(), created_at=1_700_000_000, kind="text", source="test", title="Hello")
    base.update(kw)
    return Item(**base)


def test_add_get_list_pending_and_deliver(store):
    a = store.add_item(_item(title="A", created_at=1), epub=b"E1", txt=b"T1")
    b = store.add_item(_item(title="B", created_at=2), epub=b"E2", txt=b"T2")
    assert store.get(a.id).title == "A"
    assert [i.title for i in store.list_items()] == ["B", "A"]
    assert [i.title for i in store.pending_items()] == ["A", "B"]
    store.mark_delivered(a.id, "auto", when=5)
    store.mark_delivered(a.id, "auto", when=9)  # first delivery time wins
    assert store.get(a.id).delivered == {"auto": 5}
    assert [i.id for i in store.pending_items()] == [b.id]
    assert store.file_bytes(b.id, "txt") == b"T2"
    store.requeue(a.id)
    assert len(store.pending_items()) == 2


def test_delivery_replay_and_secret(store):
    store.save_delivery("ev1", "100", ["x", "y"], b"EPUB")
    assert store.get_delivery("ev1", "100") == (["x", "y"], b"EPUB")
    assert store.get_delivery("ev1", "101") is None
    s1 = store.secret("device")
    assert s1 == store.secret("device") and len(s1) >= 24
    assert store.secret("device", configured="fixed") == "fixed"


def test_prune(store):
    old = store.add_item(_item(created_at=1000), epub=b"e", txt=b"t")
    new = store.add_item(_item(created_at=10_000_000), epub=b"e", txt=b"t")
    assert store.prune(30, now=10_000_000 + 10) == 1
    assert store.get(old.id) is None and store.get(new.id) is not None
    assert not store.item_dir(old.id).exists()


def test_textmodel_clean_text():
    from sendtox3.textmodel import clean_text
    assert clean_text("  a­b​  c™ \U0001F600 → d ") == "ab c(TM) -> d"
    assert clean_text("x\r\ny\n\n z", keep_newlines=True) == "x\ny\n\n z"
    assert clean_text("Café") == "Café"
