"""Unit tests for Store multi-agent coordination."""
import pathlib
import tempfile
import unittest

from fbbot.store import Store


class StoreMultiAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = pathlib.Path(self.tmp.name)
        self.tmp.close()
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        try:
            self.db_path.unlink()
        except Exception:
            pass

    def test_multi_agent_action_lifecycle(self):
        # 1. Add post
        post = {"post_id": "p100", "url": "https://fb.com/groups/g/posts/p100", "text": "Where to stay?"}
        self.store.add_post(post, "https://fb.com/groups/g", status="new")

        # 2. Add primary agent comment action
        act1 = self.store.add_action(
            post_id="p100",
            agent_id="agent_1",
            action_type="comment",
            content="Stay in Gulberg, it is central.",
            confidence="high",
            status="approved",
        )
        self.assertTrue(act1 > 0)
        self.assertTrue(self.store.has_agent_acted("p100", "agent_1"))
        self.assertFalse(self.store.has_agent_acted("p100", "agent_2"))

        # 3. Check primary comment retrieval
        primary = self.store.get_primary_comment("p100")
        self.assertIsNotNone(primary)
        self.assertEqual(primary["agent_id"], "agent_1")
        self.assertEqual(primary["content"], "Stay in Gulberg, it is central.")

        # 4. Add secondary agent collaborative reply action
        act2 = self.store.add_action(
            post_id="p100",
            agent_id="agent_2",
            action_type="reply",
            target_agent_id="agent_1",
            target_comment_text=primary["content"],
            content="Agree with Ali, MM Alam road has great food nearby.",
            confidence="high",
            status="pending",
        )
        self.assertTrue(self.store.has_agent_acted("p100", "agent_2"))

        # 5. Check pending actions
        pending = self.store.pending_actions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["action_id"], act2)

        # 6. Approve action
        self.store.approve_action(act2, by="review")
        self.assertEqual(len(self.store.pending_actions()), 0)

        # 7. Check due actions
        due = self.store.due_actions(agent_id="agent_2")
        self.assertEqual(len(due), 1)

        # 8. Mark executed
        self.store.mark_action_executed(act2)
        self.assertEqual(self.store.posted_today_by_agent("agent_2"), 1)


if __name__ == "__main__":
    unittest.main()

