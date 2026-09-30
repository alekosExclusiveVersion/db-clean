#!/usr/bin/env python3
"""tests/test_db_clean.py — отбор кандидатов без сети и секретов.

Запуск:  python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db_clean as d

S = {"CLOSED": "Y", "STAGE_SEMANTIC_ID": "S"}
F = {"CLOSED": "Y", "STAGE_SEMANTIC_ID": "F"}
P = {"CLOSED": "N", "STAGE_SEMANTIC_ID": "P"}
CLOSED_IN_PROGRESS = {"CLOSED": "Y", "STAGE_SEMANTIC_ID": "P"}


class ParseCandidate(unittest.TestCase):
    def test_single_id(self):
        self.assertEqual(d.parse_candidate("client_123"), [123])

    def test_several_ids(self):
        self.assertEqual(d.parse_candidate("client_123_456"), [123, 456])

    def test_digits_in_client_name_ignored(self):
        self.assertEqual(d.parse_candidate("firm2_11_x_7"), [11, 7])

    def test_without_underscore(self):
        self.assertIsNone(d.parse_candidate("master"))

    def test_tail_not_digit(self):
        self.assertIsNone(d.parse_candidate("client_123_backup"))

    def test_no_ids_at_all(self):
        self.assertIsNone(d.parse_candidate("some_db"))


class DealState(unittest.TestCase):
    def test_finished_requires_closed(self):
        self.assertFalse(d.deal_finished(P, frozenset(("S", "F"))))

    def test_finished_semantics(self):
        terminal = frozenset(("S", "F"))
        self.assertTrue(d.deal_finished(S, terminal))
        self.assertTrue(d.deal_finished(F, terminal))
        self.assertFalse(d.deal_finished(CLOSED_IN_PROGRESS, terminal))

    def test_success_only_excludes_failed(self):
        self.assertTrue(d.deal_finished(S, frozenset(("S",))))
        self.assertFalse(d.deal_finished(F, frozenset(("S",))))

    def test_missing_deal_not_finished(self):
        self.assertFalse(d.deal_finished(None, frozenset(("S", "F"))))
        self.assertEqual(d.deal_state(None), "нет сделки")

    def test_labels(self):
        self.assertEqual(d.deal_state(S), "успех")
        self.assertEqual(d.deal_state(F), "провал")
        self.assertEqual(d.deal_state(P), "в работе")
        self.assertEqual(d.deal_state({}), "нет сделки")
        self.assertEqual(d.deal_state({"CLOSED": "N"}), "?")

    def test_deal_states_keeps_ids(self):
        self.assertEqual(
            d.deal_states({1: S, 2: P}, [1, 2]),
            "1 (успех), 2 (в работе)",
        )


class TerminalSemantics(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("DB_CLEAN_TERMINAL", None)

    def tearDown(self):
        os.environ.pop("DB_CLEAN_TERMINAL", None)
        if self.saved is not None:
            os.environ["DB_CLEAN_TERMINAL"] = self.saved

    def test_default(self):
        self.assertEqual(d.terminal_semantics(), frozenset(("S", "F")))

    def test_only_success_flag_wins(self):
        os.environ["DB_CLEAN_TERMINAL"] = "F,P"
        self.assertEqual(d.terminal_semantics(True), frozenset(("S",)))

    def test_env_override(self):
        os.environ["DB_CLEAN_TERMINAL"] = " f , p "
        self.assertEqual(d.terminal_semantics(), frozenset(("F", "P")))

    def test_empty_env_falls_back_to_default(self):
        os.environ["DB_CLEAN_TERMINAL"] = " , "
        self.assertEqual(d.terminal_semantics(), frozenset(("S", "F")))


class DealCandidates(unittest.TestCase):
    def test_system_dbs_dropped(self):
        names = ["master", "msdb", "tempdb", "client_1"]
        self.assertEqual(d.deal_candidates(names), {"client_1": [1]})

    def test_only_client_and_deal_names_remain(self):
        names = ["client_1", "base_93010", "reports", "firm_x_9"]
        self.assertEqual(
            d.deal_candidates(names),
            {"client_1": [1], "base_93010": [93010], "firm_x_9": [9]},
        )

    def test_ids_dedup_keep_order(self):
        candidates = {"a_1": [3, 1], "b_2": [1, 2], "c_3": [2]}
        self.assertEqual(d.deal_id_list(candidates), [3, 1, 2])


class CollectCandidates(unittest.TestCase):
    def setUp(self):
        self.terminal = frozenset(("S", "F"))

    def test_all_finished_goes_to_drop(self):
        candidates = {"client_1": [1, 2]}
        deals = {1: S, 2: F}
        to_drop, skipped = d.collect_candidates(candidates, deals, self.terminal)
        self.assertEqual(to_drop, [("client_1", [1, 2])])
        self.assertEqual(skipped, [])

    def test_missing_deal_skipped(self):
        to_drop, skipped = d.collect_candidates({"c_9": [9]}, {}, self.terminal)
        self.assertEqual(to_drop, [])
        self.assertEqual(skipped, [("c_9", [9], "нет сделки в Б24 для [9]")])

    def test_one_unfinished_skipped(self):
        candidates = {"c_1": [1, 2]}
        deals = {1: S, 2: P}
        to_drop, skipped = d.collect_candidates(candidates, deals, self.terminal)
        self.assertEqual(to_drop, [])
        self.assertEqual(
            skipped,
            [("c_1", [1, 2], "не завершены: 2 (в работе)")],
        )

    def test_only_success_keeps_failed(self):
        candidates = {"c_1": [1]}
        to_drop, skipped = d.collect_candidates(
            candidates, {1: F}, frozenset(("S",))
        )
        self.assertEqual(to_drop, [])
        self.assertEqual(skipped, [("c_1", [1], "не завершены: 1 (провал)")])

    def test_closed_in_progress_never_dropped(self):
        candidates = {"c_1": [1]}
        to_drop, skipped = d.collect_candidates(
            candidates, {1: CLOSED_IN_PROGRESS}, frozenset(("P",))
        )
        self.assertEqual(to_drop, [("c_1", [1])])
        self.assertEqual(skipped, [])

    def test_mixed_sorted_order(self):
        candidates = {
            "z_client_3": [3],
            "a_client_1": [1],
            "m_client_2": [2],
            "n_client_4": [4],
        }
        deals = {1: S, 2: S, 3: F, 4: P}
        to_drop, skipped = d.collect_candidates(candidates, deals, self.terminal)
        self.assertEqual(to_drop, [("a_client_1", [1]), ("m_client_2", [2]),
                                   ("z_client_3", [3])])
        self.assertEqual([name for name, _ids, _reason in skipped], ["n_client_4"])

    def test_empty_input(self):
        self.assertEqual(d.collect_candidates({}, {}, self.terminal), ([], []))


class BannerList(unittest.TestCase):
    def test_short_list_joined(self):
        self.assertEqual(d.banner_list(["a", "b"]), "a, b")

    def test_long_list_truncated(self):
        names = [str(i) for i in range(9)]
        self.assertEqual(
            d.banner_list(names), "0, 1, 2, 3, 4, 5, и ещё 3"
        )

    def test_one_line(self):
        self.assertEqual(d.one_line("a\r\n  b\tc"), "a b c")


if __name__ == "__main__":
    unittest.main()
