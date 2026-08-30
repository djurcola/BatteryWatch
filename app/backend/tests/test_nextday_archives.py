"""Tests for bounded Next Day monthly archive discovery and planning."""

from datetime import date, datetime, timedelta, timezone
import unittest

from batterywatch_api.nextday_archives import (
    NEXTDAY_ARCHIVE_INDEX_URL,
    NextDayArchiveError,
    discover_nextday_monthly_archives,
    plan_nextday_monthly_archives,
)

UTC = timezone.utc
NEM_TIME = timezone(timedelta(hours=10))

INDEX_HTML = """<html><body><pre>
<A HREF="/Reports/ARCHIVE">[To Parent Directory]</A><br><br>
Monday, September 1, 2025 01:10 AM    213422082 <A HREF="/Reports/ARCHIVE/Next_Day_Dispatch/PUBLIC_NEXT_DAY_DISPATCH_20250701.zip">PUBLIC_NEXT_DAY_DISPATCH_20250701.zip</A><br>
Wednesday, October 1, 2025 01:03 AM    217741574 <A HREF="/Reports/ARCHIVE/Next_Day_Dispatch/PUBLIC_NEXT_DAY_DISPATCH_20250801.zip">PUBLIC_NEXT_DAY_DISPATCH_20250801.zip</A><br>
</pre></body></html>"""


class NextDayArchiveDiscoveryTests(unittest.TestCase):
    def test_discovers_canonical_months_with_listing_metadata(self) -> None:
        references = discover_nextday_monthly_archives(
            INDEX_HTML,
            index_url=NEXTDAY_ARCHIVE_INDEX_URL,
        )

        self.assertEqual(
            tuple(
                (
                    item.report_month,
                    item.filename,
                    item.url,
                    item.size_bytes,
                    item.listing_timestamp,
                )
                for item in references
            ),
            (
                (
                    date(2025, 7, 1),
                    "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
                    NEXTDAY_ARCHIVE_INDEX_URL
                    + "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip",
                    213_422_082,
                    datetime(2025, 9, 1, 1, 10, tzinfo=NEM_TIME).astimezone(UTC),
                ),
                (
                    date(2025, 8, 1),
                    "PUBLIC_NEXT_DAY_DISPATCH_20250801.zip",
                    NEXTDAY_ARCHIVE_INDEX_URL
                    + "PUBLIC_NEXT_DAY_DISPATCH_20250801.zip",
                    217_741_574,
                    datetime(2025, 10, 1, 1, 3, tzinfo=NEM_TIME).astimezone(UTC),
                ),
            ),
        )

    def test_plans_intersecting_nem_months_and_reports_missing_archives(self) -> None:
        references = discover_nextday_monthly_archives(
            INDEX_HTML,
            index_url=NEXTDAY_ARCHIVE_INDEX_URL,
        )
        start = datetime(2025, 7, 15, 0, 0, tzinfo=UTC)
        end = datetime(2025, 9, 15, 0, 0, tzinfo=UTC)

        plan = plan_nextday_monthly_archives(start, end, references)

        self.assertEqual((plan.start, plan.end), (start, end))
        self.assertEqual(
            tuple(item.report_month for item in plan.items),
            (date(2025, 7, 1), date(2025, 8, 1)),
        )
        self.assertEqual(plan.missing_months, (date(2025, 9, 1),))

    def test_rejects_malformed_duplicate_and_oversized_listing_rows(self) -> None:
        malformed_cases = (
            INDEX_HTML.replace(
                "/Reports/ARCHIVE/Next_Day_Dispatch/",
                "/Reports/ARCHIVE/Other/",
                1,
            ),
            INDEX_HTML.replace("213422082", str(256 * 1024 * 1024 + 1)),
            INDEX_HTML.replace("Monday, September", "Tuesday, September"),
            INDEX_HTML.replace(
                "</pre>",
                "Monday, September 1, 2025 01:10 AM    213422082 "
                '<A HREF="/Reports/ARCHIVE/Next_Day_Dispatch/'
                'PUBLIC_NEXT_DAY_DISPATCH_20250701.zip">'
                "PUBLIC_NEXT_DAY_DISPATCH_20250701.zip</A><br></pre>",
            ),
        )
        for payload in malformed_cases:
            with self.subTest(payload_size=len(payload)):
                with self.assertRaises(NextDayArchiveError):
                    discover_nextday_monthly_archives(
                        payload,
                        index_url=NEXTDAY_ARCHIVE_INDEX_URL,
                    )

    def test_rejects_non_utc_and_unbounded_plans(self) -> None:
        references = discover_nextday_monthly_archives(
            INDEX_HTML,
            index_url=NEXTDAY_ARCHIVE_INDEX_URL,
        )
        with self.assertRaises(NextDayArchiveError):
            plan_nextday_monthly_archives(
                datetime(2025, 7, 1, tzinfo=NEM_TIME),
                datetime(2025, 8, 1, tzinfo=NEM_TIME),
                references,
            )
        with self.assertRaises(NextDayArchiveError):
            plan_nextday_monthly_archives(
                datetime(2025, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 3, tzinfo=UTC),
                references,
            )
        with self.assertRaises(NextDayArchiveError):
            plan_nextday_monthly_archives(
                datetime(2025, 7, 1, tzinfo=UTC),
                datetime(2025, 9, 1, tzinfo=UTC),
                references,
                max_months=2,
            )


if __name__ == "__main__":
    unittest.main()
