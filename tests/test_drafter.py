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

    def test_clean_removes_newlines_and_quotes(self):
        self.assertEqual(_clean('"Take the metro.\nIt is fast."'), "Take the metro. It is fast.")


if __name__ == "__main__":
    unittest.main()
