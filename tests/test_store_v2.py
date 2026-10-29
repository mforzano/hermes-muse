"""Store v2 contract: table-driven state machines, CAS, TTL exits, dedupe, concurrency."""
import tempfile
import threading
import unittest
from pathlib import Path

from hermes_muse.kernel.store import MACHINES, Store, notice_key

NOW = 1_800_000_000.0
D = 86400


class StoreContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / "state-v2.db", clock=lambda: NOW)

    def new(self, kind, state=None, **kw):
        m = MACHINES[kind]
        state = state or m.initial
        deadline = NOW + 60 if state in m.ttl and m.ttl[state][0] is None else None
        return self.store.create(kind, f"{kind}:{state}:{len(self.store.query(kind))}", {}, state=state,
                                 deadline=deadline, dedupe=False, **kw)

    def test_every_edge_and_deadline(self):
        for kind, m in MACHINES.items():
            for a, b in sorted(m.edges):
                with self.subTest(kind=kind, edge=(a, b)):
                    e = self.new(kind, a)
                    self.assertEqual(e["deadline"] is None, m.terminal(a))
                    deadline = NOW + 60 if b in m.ttl and m.ttl[b][0] is None else None
                    moved = self.store.transition(e["id"], b, state=a, version=e["version"], reason="t", deadline=deadline)
                    self.assertEqual(moved["state"], b)
                    self.assertEqual(moved["deadline"] is None, m.terminal(b))
            for a in m.states:
                for b in m.states - {a}:
                    if (a, b) not in m.edges:
                        e = self.new(kind, a)
                        with self.assertRaises(ValueError, msg=f"{kind} {a}->{b}"):
                            self.store.transition(e["id"], b, state=a, version=e["version"], reason="t", deadline=NOW + 60)

    def test_nonterminal_needs_deadline(self):
        with self.assertRaises(ValueError):
            self.store.create("notice", "src:x")  # drafted ends at the notice's expiry; caller must give it

    def test_tick_exits_once(self):
        expected = {}
        for kind, m in MACHINES.items():
            for state, (_, exit_state) in m.ttl.items():
                expected[self.new(kind, state)["id"]] = (state, exit_state)
        moved = {m["id"]: (m["from"], m["to"]) for m in self.store.tick(NOW + 100 * D)}
        self.assertEqual(moved, {k: v for k, v in expected.items() if v[1] is not None})
        self.assertEqual(self.store.tick(NOW + 100 * D), [])
        for entity_id, (state, exit_state) in expected.items():
            if exit_state:
                self.assertEqual(self.store.events(entity_id)[-1]["reason"], "ttl")
        # Owner-decided exits (goal, run) stay due rather than moving.
        self.assertEqual({e["kind"] for e in self.store.due(NOW + 100 * D)}, {"goal", "run"})

    def test_signal_parks_after_three_packets(self):
        e = self.new("signal")
        self.store.attempt([e["id"]] * 2)
        self.assertEqual(self.store.tick(NOW), [])
        self.store.attempt([e["id"]])
        self.assertEqual(self.store.tick(NOW)[0]["reason"], "attempts")
        self.assertEqual(self.store.get(e["id"])["state"], "parked")

    def test_cas_and_concurrent_writers(self):
        e = self.new("finding")
        self.assertIsNone(self.store.transition(e["id"], "resolved", state="open", version=e["version"] + 1, reason="stale"))
        wins = []
        threads = [threading.Thread(target=lambda: wins.append(self.store.transition(
            e["id"], "resolved", state="open", version=e["version"], reason="race"))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(w is not None for w in wins), 1)

    def test_notice_dedupe_returns_existing(self):
        key, strong = notice_key(None, ["gmail:message:ABC"], "x")
        self.assertEqual((key, strong), notice_key("abc"))
        first = self.store.create("notice", key, state="sent")
        again = self.store.create("notice", key, deadline=NOW + D, now=NOW + 6 * D)
        self.assertEqual((again["id"], again["existing"]), (first["id"], True))
        later = self.store.create("notice", key, deadline=NOW + 9 * D, now=NOW + 8 * D)
        self.assertFalse(later["existing"])
        weak, strong = notice_key(None, [], "Domain expires soon!", NOW)
        self.assertFalse(strong)
        self.assertEqual(weak, notice_key(None, [], "domain  expires soon", NOW)[0])
        unsent = ({"drafted", "scheduled"}, None)
        self.store.create("notice", weak, state="sent")
        self.assertFalse(self.store.create("notice", weak, deadline=NOW + D, dedupe=unsent)["existing"])
        self.assertTrue(self.store.create("notice", weak, deadline=NOW + D, dedupe=unsent)["existing"])

    def test_readonly_reader(self):
        e = self.new("feed")
        reader = Store(self.store.path, readonly=True)
        self.assertEqual(reader.get(e["id"])["state"], "published")
        with self.assertRaises(Exception):
            reader.create("feed", "x")


if __name__ == "__main__":
    unittest.main()
