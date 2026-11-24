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

    def test_multi_agent_action_lifecycle_and_parent_sync(self):
        # 1. Add post
        post = {"post_id": "p100", "url": "https://fb.com/groups/g/posts/p100", "text": "Where to stay?"}
        self.store.add_post(post, "https://fb.com/groups/g", status="new")

        # 2. Add primary agent comment action (approved, not posted yet)
        act1 = self.store.add_action(
            post_id="p100",
            agent_id="agent_1",
            action_type="comment",
            content="Stay in Gulberg, it is central.",
            confidence="high",
            status="approved",
            approved_by="auto",
        )
        self.assertTrue(act1 > 0)
        self.assertTrue(self.store.has_agent_acted("p100", "agent_1"))
        self.assertFalse(self.store.has_agent_acted("p100", "agent_2"))

        # Legacy approved must NOT duplicate managed actions
        self.assertEqual(len(self.store.legacy_approved()), 0)

        # 3. Check primary comment retrieval
        primary = self.store.get_primary_comment("p100")
        self.assertIsNotNone(primary)
        self.assertEqual(primary["action_id"], act1)
        self.assertEqual(primary["agent_id"], "agent_1")

        # 4. Add secondary agent collaborative like_and_reply action linked to act1
        act2 = self.store.add_action(
            post_id="p100",
            agent_id="agent_2",
            action_type="like_and_reply",
            target_action_id=act1,
            target_agent_id="agent_1",
            target_comment_text=primary["content"],
            content="Agree with Ali, MM Alam road has great food nearby.",
            confidence="high",
            status="pending",
            delay_minutes=0,
        )
        self.assertTrue(self.store.has_agent_acted("p100", "agent_2"))

        # 5. Approve and edit primary comment; child must see live edited text
        self.store.approve_action(act1, content="Stay near Liberty Market, very convenient.", by="edited")

        # 6. Approve secondary action
        self.store.approve_action(act2, by="review")
        self.assertEqual(len(self.store.pending_actions()), 0)

        # Secondary action MUST NOT be due while primary comment is still unposted!
        self.assertEqual(len(self.store.due_actions(agent_id="agent_2")), 0)
        self.assertEqual(self.store.waiting_delay_count("agent_2"), 1)

        # 7. Execute primary comment -> child action becomes due and carries updated text!
        self.store.mark_action_executed(act1)
        due2 = self.store.due_actions(agent_id="agent_2")
        self.assertEqual(len(due2), 1)
        self.assertEqual(due2[0]["live_target_comment"], "Stay near Liberty Market, very convenient.")

        # 8. Execute secondary action and check history
        self.store.mark_action_executed(act2)
        self.assertEqual(self.store.posted_today_by_agent("agent_1"), 1)
        self.assertEqual(self.store.posted_today_by_agent("agent_2"), 1)
        hist = self.store.history()
        self.assertEqual(len(hist), 2)

    def test_skipping_parent_cancels_dependent_actions(self):
        post = {"post_id": "p200", "url": "https://fb.com/groups/g/posts/p200", "text": "Taxi fare?"}
        self.store.add_post(post, "https://fb.com/groups/g", status="new")
        act1 = self.store.add_action(
            post_id="p200", agent_id="agent_1", action_type="comment",
            content="Use Indrive.", status="approved",
        )
        act2 = self.store.add_action(
            post_id="p200", agent_id="agent_2", action_type="like",
            target_action_id=act1, target_agent_id="agent_1",
            target_comment_text="Use Indrive.", status="pending",
        )
        self.store.set_action_status(act1, "skipped")
        self.assertEqual(len(self.store.pending_actions()), 0)
        actions = {r["action_id"]: r for r in self.store.get_post_actions("p200")}
        self.assertEqual(actions[act2]["status"], "skipped")

    def test_like_on_existing_comment_never_enters_legacy_queue(self):
        post = {"post_id": "p300", "url": "https://www.facebook.com/groups/g/posts/300", "text": "q"}
        self.store.add_post(post, "g", "new")
        # what scan stores for an auto-approved like+reply on another member's comment
        self.store.save_analysis("p300", {"action": "like_and_reply", "reply": "tip"}, "engaged", primary_agent_id="agent_1")
        act = self.store.add_action(
            post_id="p300", agent_id="agent_1", action_type="like_and_reply", content="tip",
            target_comment_text="existing answer", status="approved", approved_by="auto",
        )
        self.assertEqual(self.store.legacy_approved(), [])
        self.assertIsNone(self.store.get_primary_comment("p300"))
        self.assertEqual(len(self.store.due_actions(agent_id="agent_1")), 1)
        self.store.mark_action_executed(act)
        self.assertEqual(self.store.get_post("p300")["status"], "engaged")
        self.assertEqual(self.store.by_status("pending"), [])

    def test_approved_post_without_comment_text_is_not_legacy(self):
        post = {"post_id": "p301", "url": "https://www.facebook.com/groups/g/posts/301", "text": "q"}
        self.store.add_post(post, "g", "approved")
        self.assertEqual(self.store.legacy_approved(), [])

    def test_likes_count_toward_daily_cap(self):
        post = {"post_id": "p302", "url": "https://www.facebook.com/groups/g/posts/302", "text": "q"}
        self.store.add_post(post, "g", "engaged")
        act = self.store.add_action(post_id="p302", agent_id="agent_1", action_type="like", status="approved")
        self.store.mark_action_executed(act)
        self.assertEqual(self.store.posted_today_by_agent("agent_1"), 1)

    def test_discard_unfinished_retries_interrupted_posts(self):
        post = {"post_id": "p303", "url": "https://www.facebook.com/groups/g/posts/303", "text": "q", "author_id": "u1"}
        self.store.add_post(post, "g", "new")
        self.assertEqual(self.store.discard_unfinished(), 1)
        self.assertFalse(self.store.has_post("p303"))


if __name__ == "__main__":
    unittest.main()
