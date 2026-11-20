import sqlite3

import pytest

from nai.store import Store, StoreError


def test_read_only_query_connection_can_only_enqueue_command(tmp_path):
    db = tmp_path / "nai.sqlite3"
    with Store(db) as writer:
        project = writer.create_project("web")
        node = writer.create_node(project, "web-root", "system")
    with Store(db, read_only=True) as web_store:
        assert web_store.get_project(project)["name"] == "web"
        command = web_store.enqueue_command(project, "retry_node", node_id=node, payload={"reason": "manual"})
        with pytest.raises(StoreError, match="read-only"):
            web_store.create_project("forbidden")
    with Store(db) as writer:
        claimed = writer.claim_commands()
        assert claimed[0]["id"] == command
        assert claimed[0]["payload"] == {"reason": "manual"}
        writer.complete_command(command)
        assert writer.list_commands(project)[0]["status"] == "succeeded"


def test_events_are_append_only(store, project):
    store.record_event(project, "test", "immutable")
    event_id = store.list_events(project)[0]["id"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE events SET message='changed' WHERE id=?", (event_id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM events WHERE id=?", (event_id,))
