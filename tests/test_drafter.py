"""Offline tests for parsing model output. Run: .venv/bin/python -m unittest"""
import unittest

from fbbot.drafter import Analysis, CollaborationAnalysis, _clean, _extract_json


class ExtractJsonTest(unittest.TestCase):
    def test_strips_think_block_and_code_fence(self):
        raw = '<think>let me see {not json}</think>\n```json\n{"relevant": true, "reply": "Hi"}\n```'
        self.assertEqual(_extract_json(raw), '{"relevant": true, "reply": "Hi"}')

    def test_rejects_output_without_json(self):
        with self.assertRaises(ValueError):
            _extract_json("Sorry, I can't help with that.")

    def test_lenient_fields(self):
        a = Analysis.model_validate_json('{"relevant": true, "reply": "Go early", "confidence": "High", "travel_dates": null}')
        self.assertEqual(a.confidence, "high")
        self.assertEqual(a.travel_dates, "")
        self.assertEqual(a.questions, [])

    def test_collaboration_analysis(self):
        c = CollaborationAnalysis.model_validate_json(
            '{"action": "Reply", "reply": "Agree with Ali!", "confidence": "HIGH", "reason": "Adds tip"}'
        )
        self.assertEqual(c.action, "reply")
        self.assertEqual(c.reply, "Agree with Ali!")
        self.assertEqual(c.confidence, "high")

    def test_collaboration_like_action(self):
        c = CollaborationAnalysis.model_validate_json(
            '{"action": "like", "reply": null, "confidence": "medium", "reason": "Already answered"}'
        )
        self.assertEqual(c.action, "like")
        self.assertEqual(c.reply, "")

    def test_collaboration_like_and_reply_and_better_comment(self):
        c1 = CollaborationAnalysis.model_validate_json(
            '{"action": "like_and_reply", "reply": "Agreed, also go before 8pm!", "confidence": "high", "reason": "Extra tip"}'
        )
        self.assertEqual(c1.action, "like_and_reply")
        self.assertEqual(c1.reply, "Agreed, also go before 8pm!")

        c2 = CollaborationAnalysis.model_validate_json(
            '{"action": "comment", "reply": "Better standalone advice here.", "confidence": "high", "reason": "Missed main question"}'
        )
        self.assertEqual(c2.action, "comment")
        self.assertEqual(c2.reply, "Better standalone advice here.")

    def test_clean_removes_newlines_and_quotes(self):
        self.assertEqual(_clean('"Take the metro.\nIt is fast."'), "Take the metro. It is fast.")


class PeerCollaborationTest(unittest.TestCase):
    """Between teammates the only outcomes are comment, reply or skip, whatever the model says."""

    def _run(self, answer: dict):
        import json as _json
        from fbbot.drafter import Drafter

        class FakeBackend:
            def complete(self, user, system_override=None, schema_override=None):
                self.system, self.schema = system_override, schema_override
                return _json.dumps(answer)

        d = Drafter.__new__(Drafter)
        d.provider, d.destination, d.persona, d.max_words, d.knowledge = "claude", "Lahore", "a local", 40, "notes"
        d._backend = FakeBackend()
        return d, d.analyze_collaboration("post", "existing", "Ali", peer=True)

    def test_like_becomes_skip(self):
        _, out = self._run({"action": "like", "reply": "", "confidence": "high", "reason": "complete"})
        self.assertEqual(out.action, "skip")

    def test_like_and_reply_loses_the_like(self):
        _, out = self._run(
            {"action": "like_and_reply", "reply": "It opens at 6pm, not 4pm.", "confidence": "high", "reason": "hours"}
        )
        self.assertEqual(out.action, "reply")

    def test_empty_text_is_skip(self):
        _, out = self._run({"action": "comment", "reply": "", "confidence": "high", "reason": ""})
        self.assertEqual(out.action, "skip")

    def test_peer_prompt_and_schema_are_used(self):
        from fbbot.drafter import PEER_SCHEMA
        d, out = self._run(
            {"action": "comment", "reply": "Fare is 500, agree it first.", "confidence": "high", "reason": "fare"}
        )
        self.assertEqual(out.action, "comment")
        self.assertIs(d._backend.schema, PEER_SCHEMA)
        self.assertIn("MORE ACCURATE", d._backend.system)


if __name__ == "__main__":
    unittest.main()
