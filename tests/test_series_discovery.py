import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import Book, Series


class SeriesDiscoveryRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
            self.db.add(
                Book(
                    title=f"Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_title_search_variants_cover_book_seven_patterns(self):
        agent = SeriesIntelligenceAgent()

        passes = agent._build_discovery_passes("Cherry Blossom Girls", 7, ["Harmon Cooper"])
        queries = [item["query"] for item in passes]

        expected_variants = [
            'Cherry Blossom Girls International: (Book Seven)',
            'Cherry Blossom Girls International (Book Seven)',
            'Cherry Blossom Girls International: Book Seven',
            'Cherry Blossom Girls International Book Seven',
            'Cherry Blossom Girls International #7',
            'Cherry Blossom Girls Book 7',
            'Cherry Blossom Girls: Book Seven',
            'Cherry Blossom Girls #7',
            'Cherry Blossom Girls Book Seven',
            'Cherry Blossom Girls International 7',
            'Cherry Blossom Girls International Seven',
            'Cherry Blossom Girls International: (Book Seven) Harmon Cooper',
        ]

        for variant in expected_variants:
            self.assertTrue(any(variant in query for query in queries), variant)

        self.assertGreaterEqual(len({item["stage"] for item in passes}), 2)

    def test_provider_order_is_enforced(self):
        agent = SeriesIntelligenceAgent()
        plan = agent._provider_plan()
        provider_names = [provider.name for _, providers in plan for provider in providers]
        self.assertEqual(
            provider_names,
            [
                "amazon",
                "fantastic_fiction",
                "author_site",
                "publisher",
                "book_database",
                "web_read",
                "catalog_fallback",
            ],
        )

    def test_manual_delete_recounts_series_aggregates(self):
        # Mark one surviving book as read so read/unread counters are non-zero after delete.
        keep_read = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 2.0).first()
        delete_target = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 1.0).first()
        self.assertIsNotNone(keep_read)
        self.assertIsNotNone(delete_target)
        keep_read.is_read = True
        self.db.commit()

        deleted = crud.delete_book(self.db, delete_target.id)
        self.assertTrue(deleted)

        refreshed = self.db.query(Series).filter(Series.id == self.series.id).first()
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.total_books, 7)
        self.assertEqual(refreshed.read_count, 1)
        self.assertEqual(refreshed.unread_count, 6)

    def test_strict_cleanup_purges_only_provably_invalid_non_user_books(self):
        self.series.is_finished = True
        self.series.series_status = "completed"
        self.series.total_books = 9
        self.db.commit()

        invalid = Book(
            title="Phantom Book 15",
            author="Harmon Cooper",
            series_id=self.series.id,
            series_order=15,
            book_number=15.0,
            record_status="active",
            is_read=False,
            import_source="unverified",
        )
        user_added = Book(
            title="User Added Phantom Book 16",
            author="Harmon Cooper",
            series_id=self.series.id,
            series_order=16,
            book_number=16.0,
            record_status="active",
            is_read=False,
            import_source="manual",
        )
        self.db.add(invalid)
        self.db.add(user_added)
        self.db.commit()

        agent = SeriesIntelligenceAgent()
        with patch.object(
            SeriesIntelligenceAgent,
            "discover",
            return_value={"results": [], "status": "no_hits"},
        ):
            result = agent._strict_post_discovery_cleanup(
                self.db,
                self.series,
                known_authors=["Harmon Cooper"],
                known_series_max=9,
                series_complete=True,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["deleted_entries"][0]["book_number"], 15.0)

        remaining = self.db.query(Book).filter(Book.series_id == self.series.id).all()
        self.assertEqual(len(remaining), 9)
        self.assertTrue(any(book.book_number == 16.0 for book in remaining))
        refreshed = self.db.query(Series).filter(Series.id == self.series.id).first()
        self.assertEqual(refreshed.total_books, 9)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch(
        "agents.series_agent.search_amazon_products",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Amazon listing",
                "cover_image": "https://images.example/cover.jpg",
                "publication_date": "2024-02-20",
                "source_url": "https://www.amazon.com/dp/B07RS4RN2G",
                "source": "amazon",
            }
        ],
    )
    def test_any_direct_provider_can_succeed(
        self,
        mock_amazon,
        mock_fantastic,
        mock_google,
        mock_openlibrary,
        mock_serpapi,
        mock_goodreads,
        mock_web_read,
        mock_author_site,
        mock_publisher,
        mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"])

        self.assertTrue(result["results"])
        for field in ["title", "author", "series_name", "series_position", "description", "cover_image", "publication_date", "source"]:
            self.assertIn(field, result["results"][0])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["discovery_mode"], "direct")
        self.assertIn("Amazon", result["sources_used"])
        self.assertIn("publication_date_filter", result["diagnostics"])
        self.assertTrue(result["diagnostics"]["publication_date_filter"]["accepted"])
        self.assertTrue(mock_amazon.called)
        self.assertTrue(mock_web_read.called)
        self.assertTrue(mock_google.called)
        self.assertTrue(mock_openlibrary.called)
        self.assertTrue(mock_serpapi.called)
        self.assertTrue(mock_goodreads.called)
        self.assertTrue(mock_author_site.called)
        self.assertTrue(mock_publisher.called)
        self.assertTrue(mock_book_db.called)

    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch("agents.series_agent.search_amazon_products", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    def test_discover_returns_missing_books_when_no_metadata_is_found(
        self,
        mock_google,
        mock_openlibrary,
        mock_amazon,
        mock_fantastic,
        mock_serpapi,
        mock_web_read,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"])

        self.assertEqual(result["results"], [])
        self.assertEqual(result["missing_books"], [7])
        self.assertEqual(result["status"], "no_hits")
        self.assertEqual(result["reason"], "no-hit-after-all-passes")
        self.assertEqual(result["discovery_engine"], "agent_v2")
        self.assertEqual(result["discovery_mode"], "web_read")
        self.assertTrue(result["agent_pipeline"])
        self.assertIn("publication_date_filter", result["diagnostics"])
        self.assertTrue(mock_web_read.called)
        self.assertTrue(mock_google.called)
        self.assertTrue(mock_openlibrary.called)
        self.assertTrue(mock_amazon.called)
        self.assertTrue(mock_fantastic.called)
        self.assertTrue(mock_serpapi.called)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch("agents.series_agent.search_amazon_products", return_value=[])
    def test_discover_finds_metadata_from_any_web_source(
        self,
        _mock_amazon,
        _mock_fantastic,
        mock_google,
        mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        source_cases = [
            ("amazon", "Amazon", "https://www.amazon.com/Cherry-Blossom-Girls-International-Seven-ebook/dp/B07RS4RN2G"),
            ("fantastic_fiction", "FantasticFiction", "https://www.fantasticfiction.com/c/harmon-cooper/cherry-blossom-girls-international-7.htm"),
            ("otherwebsources", "OtherWebSources", "https://example.org/blog/cherry-blossom-girls-book-seven-review"),
        ]

        for source_key, source_label, source_url in source_cases:
            with self.subTest(source=source_label):
                web_results = [
                    {
                        "title": "Cherry Blossom Girls International: (Book Seven)",
                        "author": "Harmon Cooper",
                        "description": "Book Seven metadata found on the web.",
                        "publication_date": "2021-01-01",
                        "cover_image": "https://example.org/cover.jpg",
                        "series_name": "Cherry Blossom Girls",
                        "source_url": source_url,
                        "series_position": 7,
                        "source": source_key,
                    }
                ]
                if source_key == "otherwebsources":
                    web_results.append(
                        {
                            "title": "Cherry Blossom Girls International: (Book Seven)",
                            "author": "Harmon Cooper",
                            "description": "Second source confirms metadata.",
                            "publication_date": "2021-01-01",
                            "cover_image": "https://example.net/cover.jpg",
                            "series_name": "Cherry Blossom Girls",
                            "source_url": "https://sample.net/library/cherry-blossom-girls-book-seven",
                            "series_position": 7,
                            "source": source_key,
                        }
                    )
                with patch("agents.series_agent.search_web_read_candidates", return_value=web_results) as mock_web_read:
                    result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"])

                self.assertTrue(result["results"])
                for field in ["title", "author", "series_name", "series_position", "description", "cover_image", "publication_date", "source"]:
                    self.assertIn(field, result["results"][0])
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["missing_books"], [])
                self.assertEqual(result["discovery_engine"], "agent_v2")
                self.assertEqual(result["discovery_mode"], "web_read")
                self.assertIn(source_label, result["sources_used"])
                self.assertTrue(mock_web_read.called)

            self.assertTrue(mock_google.called)
            self.assertTrue(mock_openlibrary.called)

    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch("agents.series_agent.search_amazon_products", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch(
        "agents.series_agent.search_google_books",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_position": 7,
                "description": "Catalog fallback",
                "publication_date": "2020-02-01",
                "cover_image": "https://books.google.com/cover.jpg",
                "series_name": "Cherry Blossom Girls",
                "source_url": "https://books.google.com/example",
                "source": "google_books",
            }
        ],
    )
    def test_catalog_fallback_only_runs_last(
        self,
        mock_google,
        mock_openlibrary,
        mock_serpapi,
        mock_goodreads,
        mock_book_db,
        mock_publisher,
        mock_author_site,
        mock_amazon,
        mock_fantastic,
        mock_web_read,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"])

        self.assertEqual(result["status"], "complete")
        for field in ["title", "author", "series_name", "series_position", "description", "cover_image", "publication_date", "source"]:
            self.assertIn(field, result["results"][0])
        self.assertEqual(result["discovery_mode"], "catalog_fallback")
        self.assertIn("GoogleBooks", result["sources_used"])
        self.assertTrue(mock_amazon.called)
        self.assertTrue(mock_fantastic.called)
        self.assertTrue(mock_author_site.called)
        self.assertTrue(mock_publisher.called)
        self.assertTrue(mock_book_db.called)
        self.assertTrue(mock_web_read.called)
        self.assertTrue(mock_google.called)
        self.assertFalse(mock_openlibrary.called)
        self.assertFalse(mock_serpapi.called)
        self.assertFalse(mock_goodreads.called)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch("agents.series_agent.search_amazon_products", return_value=[])
    @patch(
        "agents.series_agent.search_author_site_pages",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Found on a random indie site",
                "cover_image": "https://harmoncooper.com/covers/book7.jpg",
                "publication_date": "2022-03-01",
                "source_url": "https://harmoncooper.com/books/cherry-blossom-girls-7",
                "source": "author_site",
            }
        ],
    )
    def test_any_unknown_site_can_succeed_via_direct_generic_provider(
        self,
        mock_author_site,
        mock_amazon,
        mock_fantastic,
        mock_publisher,
        mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"])

        self.assertEqual(result["status"], "complete")
        for field in ["title", "author", "series_name", "series_position", "description", "cover_image", "publication_date", "source"]:
            self.assertIn(field, result["results"][0])
        self.assertEqual(result["discovery_mode"], "direct")
        self.assertIn("AuthorSite", result["sources_used"])
        self.assertTrue(mock_amazon.called)
        self.assertTrue(mock_fantastic.called)
        self.assertTrue(mock_author_site.called)
        self.assertTrue(mock_publisher.called)
        self.assertTrue(mock_book_db.called)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch(
        "agents.series_agent.search_amazon_products",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Fifteen)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 15,
                "description": "Speculative listing",
                "cover_image": "https://example.com/spec.jpg",
                "publication_date": "2021-01-01",
                "source_url": "https://www.amazon.com/speculative",
                "source": "amazon",
            }
        ],
    )
    def test_rejects_speculative_titles_above_known_series_length(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        for speculative_number in [15, 18, 20]:
            with self.subTest(book_number=speculative_number):
                result = agent.discover("Cherry Blossom Girls", speculative_number, ["Harmon Cooper"], known_series_max=9)
                self.assertEqual(result["status"], "no_hits")
                self.assertEqual(result["final_reason"], "no-hit-after-all-providers")
                self.assertTrue(result["diagnostics"]["false_positive_filter"]["series_number_check"])
                self.assertGreaterEqual(result["diagnostics"]["rejection_counts"].get("series_number_speculative", 0), 1)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch(
        "agents.series_agent.search_amazon_products",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Valid listing",
                "cover_image": "https://images.example/book7.jpg",
                "publication_date": "2022-05-01",
                "source_url": "https://www.amazon.com/valid",
                "source": "amazon",
            }
        ],
    )
    def test_accepts_valid_titles_within_known_series_length(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"], known_series_max=9)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["discovery_mode"], "direct")
        self.assertEqual(result["provider_selected"], "AmazonProvider")
        self.assertTrue(result["diagnostics"]["false_positive_filter"]["publication_date_check"])
        self.assertTrue(result["diagnostics"]["false_positive_filter"]["source_credibility_check"])
        self.assertTrue(result["diagnostics"]["false_positive_filter"]["series_number_check"])
        self.assertTrue(result["diagnostics"]["publication_date_filter"]["accepted"])

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch(
        "agents.series_agent.search_web_read_candidates",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Rumor and release date speculation post",
                "cover_image": "https://bookseriesupdates.net/cover.jpg",
                "publication_date": "2025-02-01",
                "source_url": "https://bookseriesupdates.net/cherry-blossom-girls-7-release-date-rumor",
                "source": "otherwebsources",
            },
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Rumor mirror",
                "cover_image": "https://bookseriesupdates.org/cover.jpg",
                "publication_date": "2025-02-01",
                "source_url": "https://bookseriesupdates.org/cherry-blossom-girls-7-release-date-rumor",
                "source": "otherwebsources",
            },
        ],
    )
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch("agents.series_agent.search_amazon_products", return_value=[])
    def test_rejects_unknown_domain_seo_network_and_keywords(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"], series_complete=False)

        self.assertEqual(result["status"], "no_hits")
        self.assertGreaterEqual(result["diagnostics"]["rejection_counts"].get("unknown_domain_network_blocked", 0), 1)
        self.assertGreaterEqual(result["diagnostics"]["rejection_counts"].get("unknown_domain_keyword_blocked", 0), 1)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch(
        "agents.series_agent.search_web_read_candidates",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Independent corroboration A",
                "cover_image": "https://novelarchive.net/cover.jpg",
                "publication_date": "2025-02-01",
                "source_url": "https://novelarchive.net/cherry-blossom-girls-7",
                "source": "otherwebsources",
            },
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Independent corroboration B",
                "cover_image": "https://readtracker.org/cover.jpg",
                "publication_date": "2025-02-01",
                "source_url": "https://readtracker.org/cherry-blossom-girls-7",
                "source": "otherwebsources",
            },
        ],
    )
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch("agents.series_agent.search_amazon_products", return_value=[])
    def test_rejects_unknown_domains_when_series_complete_even_with_corroboration(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"], series_complete=True)

        self.assertEqual(result["status"], "no_hits")
        self.assertGreaterEqual(result["diagnostics"]["rejection_counts"].get("unknown_domain_on_complete_series", 0), 1)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch(
        "agents.series_agent.search_amazon_products",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Trusted metadata should survive filtering",
                "cover_image": "https://images.example/book7.jpg",
                "publication_date": "2022-05-01",
                "source_url": "https://www.amazon.com/valid",
                "source": "amazon",
            },
            {
                "title": "Cherry Blossom Girls International: (Book Seven)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 7,
                "description": "Rumor release date speculation and spoilers",
                "cover_image": "https://bookseriesupdates.net/rumor.jpg",
                "publication_date": "2022-05-01",
                "source_url": "https://bookseriesupdates.net/cherry-blossom-girls-book-seven-rumor",
                "source": "otherwebsources",
            },
        ],
    )
    def test_response_results_use_filtered_candidates_only(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 7, ["Harmon Cooper"], series_complete=False)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["source"], "amazon")
        self.assertNotIn("bookseriesupdates.net", str(result["results"][0]))

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch(
        "agents.series_agent.search_amazon_products",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Ten)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 10,
                "description": "Announced listing",
                "cover_image": "https://images.example/book10.jpg",
                "publication_date": "2099-01-01",
                "source_url": "https://www.amazon.com/future",
                "source": "amazon",
            }
        ],
    )
    def test_future_date_is_allowed_when_series_not_complete(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 10, ["Harmon Cooper"], series_complete=False)

        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["diagnostics"]["publication_date_filter"]["accepted"])
        self.assertFalse(result["diagnostics"]["publication_date_filter"]["series_complete"])
        self.assertTrue(result["diagnostics"]["publication_date_filter"]["date_future"])
        self.assertEqual(result["diagnostics"]["publication_date_filter"]["reason"], "future_date_allowed_series_not_complete")

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch(
        "agents.series_agent.search_amazon_products",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Ten)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 10,
                "description": "Erroneous future listing",
                "cover_image": "https://images.example/book10.jpg",
                "publication_date": "2099-01-01",
                "source_url": "https://www.amazon.com/future",
                "source": "amazon",
            }
        ],
    )
    def test_future_date_is_rejected_when_series_complete(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover("Cherry Blossom Girls", 10, ["Harmon Cooper"], series_complete=True)

        self.assertEqual(result["status"], "no_hits")
        self.assertFalse(result["diagnostics"]["publication_date_filter"]["accepted"])
        self.assertTrue(result["diagnostics"]["publication_date_filter"]["series_complete"])
        self.assertTrue(result["diagnostics"]["publication_date_filter"]["date_future"])
        self.assertEqual(result["diagnostics"]["publication_date_filter"]["reason"], "publication_date_future_on_complete_series")
        self.assertGreaterEqual(result["diagnostics"]["rejection_counts"].get("publication_date_future", 0), 1)

    @patch("agents.series_agent.search_book_database_pages", return_value=[])
    @patch("agents.series_agent.search_publisher_pages", return_value=[])
    @patch("agents.series_agent.search_author_site_pages", return_value=[])
    @patch("agents.series_agent.search_web_read_candidates", return_value=[])
    @patch("agents.series_agent.search_goodreads_api", return_value=[])
    @patch("agents.series_agent.search_serpapi_web", return_value=[])
    @patch("agents.series_agent.search_openlibrary", return_value=[])
    @patch("agents.series_agent.search_google_books", return_value=[])
    @patch("agents.series_agent.search_fantastic_fiction", return_value=[])
    @patch(
        "agents.series_agent.search_amazon_products",
        return_value=[
            {
                "title": "Cherry Blossom Girls International: (Book Ten)",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls International",
                "series_position": 10,
                "description": "Filtered out speculative listing",
                "cover_image": "https://images.example/book10.jpg",
                "publication_date": "2025-01-01",
                "source_url": "https://www.amazon.com/speculative",
                "source": "amazon",
            }
        ],
    )
    def test_completed_series_no_hits_are_classified_as_complete_mode(
        self,
        _mock_amazon,
        _mock_fantastic,
        _mock_google,
        _mock_openlibrary,
        _mock_serpapi,
        _mock_goodreads,
        _mock_web_read,
        _mock_author_site,
        _mock_publisher,
        _mock_book_db,
    ):
        agent = SeriesIntelligenceAgent()

        result = agent.discover(
            "Cherry Blossom Girls",
            10,
            ["Harmon Cooper"],
            known_series_max=9,
            series_complete=True,
        )

        self.assertEqual(result["status"], "no_hits")
        self.assertEqual(result["discovery_mode"], "complete")
        self.assertEqual(result["final_reason"], "no-hit-after-all-providers")

    @patch.object(
        SeriesIntelligenceAgent,
        "discover",
        return_value={
            "query": "Cherry Blossom Girls International: (Book Seven)",
            "results": [],
            "diagnostics": {
                "selected_stage": "none",
                "provider_counts": {"web_read": 0, "google": 0, "openlibrary": 0, "serpapi": 0},
                "rejection_counts": {},
                "stages": [],
                "accepted_total": 0,
                "top_score": 0.0,
                "passes_completed": 0,
                "sources_used": [],
                "current_pass": "none",
                "publication_date_filter": {
                    "series_complete": False,
                    "date_future": False,
                    "date_missing": True,
                    "accepted": False,
                    "reason": "no_candidates",
                },
            },
            "passes_completed": 0,
            "sources_used": [],
            "missing_books": [7],
            "status": "no_hits",
            "reason": "no-hit-after-all-passes",
            "discovery_engine": "agent_v2",
            "discovery_mode": "web_read",
            "agent_pipeline": True,
        },
    )
    def test_missing_book_gap_is_prioritized_and_uses_agent_pipeline(self, mock_discover):
        agent = SeriesIntelligenceAgent()

        result = agent.run_series_check(self.db, self.series.id)

        self.assertEqual(result["discovery_engine"], "agent_v2")
        self.assertTrue(result["agent_pipeline"])
        self.assertEqual(result["missing_books"], ["7"])
        self.assertGreater(len(result["candidate_numbers"]), 0)
        self.assertEqual(result["candidate_numbers"][0], 7)
        mock_discover.assert_any_call(
            "Cherry Blossom Girls",
            7,
            ["Harmon Cooper"],
            known_series_max=9,
            series_complete=False,
        )

if __name__ == "__main__":
    unittest.main()
