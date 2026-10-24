import unittest
from datetime import date

from paper_pipeline.paper_dates import parse_published_date, sort_papers_by_date


class PaperDateTests(unittest.TestCase):
    def test_date_validation_and_unknown_values(self):
        self.assertEqual(parse_published_date("2024-02-29"), date(2024, 2, 29))
        self.assertIsNone(parse_published_date(None))
        self.assertIsNone(parse_published_date(""))
        for value in ("2023-02-29", "2026-13-01", "0000-01-01", "20260102",
                      "2026-1-02", "2026-01-02T12:00:00Z", " 2026-01-02", 20260102, False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_published_date(value)

    def test_both_orders_keep_unknown_dates_last_and_preserve_ties(self):
        records = [
            {"id": "missing"},
            {"id": "old", "published_date": "2023-06-23"},
            {"id": "new-first", "published_date": "2026-05-29"},
            {"id": "null", "published_date": None},
            {"id": "new-second", "published_date": "2026-05-29"},
            {"id": "empty", "published_date": ""},
        ]
        original = list(records)
        self.assertEqual([p["id"] for p in sort_papers_by_date(records)],
                         ["new-first", "new-second", "old", "missing", "null", "empty"])
        self.assertEqual([p["id"] for p in sort_papers_by_date(records, "oldest")],
                         ["old", "new-first", "new-second", "missing", "null", "empty"])
        self.assertEqual(records, original)

    def test_sort_uses_first_submission_not_revision_date(self):
        records = [
            {"arxiv_id": "2606.00422", "published_date": "2026-05-29", "updated": "2026-05-29"},
            {"arxiv_id": "2510.26104", "published_date": "2025-10-30", "updated": "2026-02-02"},
            {"arxiv_id": "2405.07314", "published_date": "2024-05-12", "updated": "2025-10-17"},
            {"arxiv_id": "2509.03236", "published_date": "2025-09-03", "updated": "2025-10-22"},
            {"arxiv_id": "2402.17152", "published_date": "2024-02-27", "updated": "2024-05-06"},
            {"arxiv_id": "2403.01744", "published_date": "2024-03-04", "updated": "2024-03-25"},
        ]
        self.assertEqual([p["arxiv_id"] for p in sort_papers_by_date(records)],
                         ["2606.00422", "2510.26104", "2509.03236", "2405.07314",
                          "2403.01744", "2402.17152"])

    def test_invalid_order_or_date_does_not_silently_change_sorting(self):
        with self.assertRaises(ValueError):
            sort_papers_by_date([], "random")
        with self.assertRaises(ValueError):
            sort_papers_by_date([{"published_date": "2026-02-30"}])
        self.assertEqual(sort_papers_by_date([]), [])


if __name__ == "__main__":
    unittest.main()
