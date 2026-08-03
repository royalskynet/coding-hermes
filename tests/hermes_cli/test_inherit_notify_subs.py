"""Public create_task regression tests for notification inheritance."""

import json

from hermes_cli.kanban_db import add_notify_sub, connect, create_task


def test_inherit_notify_subs_includes_chat_type_and_delivery_metadata(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    parent_id = create_task(conn, title="Parent Task")
    add_notify_sub(
        conn, task_id=parent_id, platform="telegram",
        chat_id="12345", chat_type="topic", thread_id="67890",
        user_id="user-1", notifier_profile="test-profile",
        delivery_metadata={"topic_id": 67890, "reply_to": 555},
    )
    child_id = create_task(conn, title="Child Task", parents=[parent_id])
    row = conn.execute(
        "SELECT chat_type, delivery_metadata, thread_id, notifier_profile "
        "FROM kanban_notify_subs "
        "WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?",
        (child_id, "telegram", "12345", "67890"),
    ).fetchone()
    assert row is not None
    assert row["chat_type"] == "topic"
    # JSON keys may serialize in any order — parse and check
    meta = json.loads(row["delivery_metadata"] or "{}")
    assert meta.get("topic_id") == 67890
    assert meta.get("reply_to") == 555
    assert row["thread_id"] == "67890"
    assert row["notifier_profile"] == "test-profile"


def test_inherit_notify_subs_multiple_parents(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    parent_ids = []
    for title, topic_id in [("Parent A", 111), ("Parent B", 222)]:
        parent_id = create_task(conn, title=title)
        parent_ids.append(parent_id)
        add_notify_sub(
            conn, task_id=parent_id, platform="telegram", chat_id="12345",
            chat_type="topic", thread_id=str(topic_id), user_id="user-1",
            notifier_profile="test-profile",
            delivery_metadata={"topic_id": topic_id},
        )
    child_id = create_task(conn, title="Child", parents=parent_ids)
    rows = conn.execute(
        "SELECT chat_type, delivery_metadata, thread_id FROM kanban_notify_subs "
        "WHERE task_id=? AND platform=? AND chat_id=? ORDER BY thread_id",
        (child_id, "telegram", "12345"),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["chat_type"] == "topic"
    assert rows[1]["chat_type"] == "topic"


def test_create_task_inheritance_is_not_overwritten_by_later_duplicate(tmp_path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    parent_id = create_task(conn, title="Parent")
    add_notify_sub(
        conn, task_id=parent_id, platform="telegram",
        chat_id="12345", chat_type="topic", thread_id="67890",
        user_id="user-1",
    )
    child_id = create_task(conn, title="Child", parents=[parent_id])
    add_notify_sub(
        conn, task_id=child_id, platform="telegram",
        chat_id="12345", chat_type="group", thread_id="67890",
        user_id="user-1", notifier_profile="direct",
    )
    count = conn.execute(
        "SELECT COUNT(*) c FROM kanban_notify_subs "
        "WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?",
        (child_id, "telegram", "12345", "67890"),
    ).fetchone()["c"]
    assert count == 1
    row = conn.execute(
        "SELECT chat_type, notifier_profile FROM kanban_notify_subs "
        "WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?",
        (child_id, "telegram", "12345", "67890"),
    ).fetchone()
    assert row["chat_type"] == "topic"
    assert row["notifier_profile"] == "direct"
