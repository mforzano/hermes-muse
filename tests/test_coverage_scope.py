"""Prompt contract checks; these do not run or assess a language model."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CoverageScopeTests(unittest.TestCase):
    def test_one_policy_referenced_by_sources_and_jobs(self):
        policy = (ROOT/'skills/companion/references/proactivity.md').read_text(encoding="utf-8")
        self.assertEqual(policy.count('## Coverage scope'), 2)  # heading plus quoted preferences heading
        cases = json.loads((ROOT/'tests/fixtures/coverage_scope.json').read_text(encoding="utf-8"))['cases']
        self.assertEqual(len(cases), 8)
        for case in cases:
            self.assertIn(case['rule'], policy, case['id'])
        for name in ('proactive-watch', 'memory-upkeep', 'nightly-review', 'feed-pulse', 'weekly-governance-review', 'monthly-system-audit'):
            prompt = (ROOT/'prompts'/f'{name}.md').read_text(encoding="utf-8")
            self.assertIn('Coverage scope', prompt)
            self.assertNotIn('including the full rolling 30-day calendar', prompt)
        calendar = (ROOT/'skills/companion/references/calendar.md').read_text(encoding="utf-8")
        self.assertNotIn('every proactive patrol', calendar)
        self.assertNotIn('On every existing patrol', calendar)
        self.assertIn('every patrol where calendars are in scope', calendar)
        self.assertIn('proactivity.md#coverage-scope', (ROOT/'skills/companion/references/briefings.md').read_text(encoding="utf-8"))
        self.assertIn('Calendar coverage retains all resource/event pagination', (ROOT/'prompts/proactive-watch.md').read_text(encoding="utf-8"))
        self.assertIn('Connector availability never expands scope.', policy)
        self.assertIn('never because a source merely seems irrelevant', policy)

    def test_scope_is_prose_not_a_new_schema_field(self):
        text = (ROOT/'templates/PROACTIVE_PREFERENCES.md').read_text(encoding="utf-8")
        metadata = json.loads(text.split('<!-- hermes-muse ', 1)[1].split(' -->', 1)[0])
        self.assertEqual(set(metadata), {'start_hour', 'end_hour', 'ordinary_per_day', 'time_sensitive_per_day', 'blocked_topics', 'feed_brief'})
        self.assertIn('## Coverage scope', text)
        self.assertIn('When this section is unfilled', text)
        self.assertIn('even without goals', text)
        self.assertIn('full default coverage', text)
