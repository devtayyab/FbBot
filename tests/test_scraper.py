"""Offline tests for the Facebook JSON parser. Run: .venv/bin/python -m unittest"""
import json
import unittest

from fbbot.scraper import extract_posts, parse_blob

STORY = {
    "__typename": "Story",
    "post_id": "111",
    "url": "https://www.facebook.com/groups/travelgroup/posts/111/?__cft__=x",
    "comet_sections": {
        "content": {
            "story": {
                "__typename": "Story",
                "post_id": "111",
                "url": "https://www.facebook.com/groups/travelgroup/posts/111/",
                "actors": [{"__typename": "User", "id": "42", "name": "Sara K"}],
                "message": {"text": "Coming to Lahore next week, where should I stay?"},
                "attached_story": {
                    "__typename": "Story",
                    "message": {"text": "a shared post deeper down"},
                },
            }
        },
        "context_layout": {"story": {"comet_sections": {"metadata": [{"story": {"creation_time": 1762862400}}]}}},
    },
}
COMMENT = {"__typename": "Comment", "url": "https://www.facebook.com/groups/travelgroup/posts/111/?comment_id=9"}


class ExtractTest(unittest.TestCase):
    def test_extracts_post_fields(self):
        posts = extract_posts({"data": {"node": {"group_feed": {"edges": [{"node": STORY}, {"node": COMMENT}]}}}})
        self.assertEqual(list(posts), ["111"])
        post = posts["111"]
        self.assertEqual(post["url"], "https://www.facebook.com/groups/travelgroup/posts/111/")
        self.assertEqual(post["text"], "Coming to Lahore next week, where should I stay?")
        self.assertEqual(post["author_name"], "Sara K")
        self.assertEqual(post["author_id"], "42")
        self.assertEqual(post["created_at"], 1762862400)

    def test_permalink_urls_and_missing_post_id(self):
        story = {"__typename": "Story", "url": "https://www.facebook.com/groups/123/permalink/555/"}
        self.assertIn("555", extract_posts(story))

    def test_parse_blob_handles_prefix_and_multiple_docs(self):
        blob = "for (;;);" + json.dumps({"a": 1}) + "\n" + json.dumps({"b": 2}) + "\nnot json"
        self.assertEqual(parse_blob(blob), [{"a": 1}, {"b": 2}])


if __name__ == "__main__":
    unittest.main()
