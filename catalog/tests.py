from django.test import TestCase
from django.urls import reverse
from django.conf import settings
from rest_framework import status
from .models import CatalogItem, FileLocation
from .search import perform_search

class CatalogSyncTests(TestCase):
    
    def setUp(self):
        self.api_token = "super-secret-sync-token-12345"
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {self.api_token}"}
        
    def test_api_requires_auth(self):
        """Verify endpoints return 403 Forbidden without valid Bearer Token."""
        file_url = reverse('catalog:api_sync_file')
        media_url = reverse('catalog:api_sync_media')
        
        # Missing auth header
        resp1 = self.client.post(file_url, {}, content_type='application/json')
        self.assertEqual(resp1.status_code, status.HTTP_403_FORBIDDEN)
        
        # Invalid auth header
        resp2 = self.client.post(media_url, {}, content_type='application/json', HTTP_AUTHORIZATION="Bearer badtoken")
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)

    def test_file_sync_creates_item_and_location(self):
        """Verify syncing a file creates both a CatalogItem and FileLocation."""
        url = reverse('catalog:api_sync_file')
        payload = {
            "file_name": "foundation_asimov.epub",
            "file_path": "/path/to/foundation_asimov.epub",
            "file_size": 1048576,
            "file_format": "EPUB",
            "file_hash": "hash_foundation_123",
            "title": "Foundation",
            "authors": "Isaac Asimov",
            "publisher": "Gnome Press",
            "isbn": "9780553293357"
        }
        
        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'created')
        
        # Check database
        self.assertEqual(CatalogItem.objects.count(), 1)
        self.assertEqual(FileLocation.objects.count(), 1)
        
        item = CatalogItem.objects.get()
        self.assertEqual(item.title, "Foundation")
        self.assertEqual(item.authors, "Isaac Asimov")
        
        file_loc = FileLocation.objects.get()
        self.assertEqual(file_loc.file_name, "foundation_asimov.epub")
        self.assertEqual(file_loc.catalog_item, item)

    def test_file_sync_fuzzy_matches_existing_item(self):
        """Verify a second file format with a similar name links (fuzzy matches) to the existing CatalogItem."""
        # 1. Create original item
        CatalogItem.objects.create(
            title="The Hobbit",
            authors="J.R.R. Tolkien",
            item_type="BOOK"
        )
        
        url = reverse('catalog:api_sync_file')
        # 2. Sync a PDF file of the same book with slight title/author difference
        payload = {
            "file_name": "the_hobbit_tolkien.pdf",
            "file_path": "/path/to/the_hobbit_tolkien.pdf",
            "file_size": 2048576,
            "file_format": "PDF",
            "file_hash": "hash_hobbit_pdf_321",
            "title": "The Hobbit: Or There and Back Again", # longer title
            "authors": "J. R. R. Tolkien", # spaced initials
            "publisher": "Allen & Unwin"
        }
        
        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'linked')
        
        # Check database: should NOT create a new CatalogItem, but link to the existing one
        self.assertEqual(CatalogItem.objects.count(), 1)
        self.assertEqual(FileLocation.objects.count(), 1)
        
        file_loc = FileLocation.objects.get()
        self.assertEqual(file_loc.catalog_item.title, "The Hobbit") # Linked to the original CatalogItem

    def test_media_sync_links_audible_to_existing_book(self):
        """Verify syncing an Audible audiobook links to the existing book in the catalog."""
        # 1. Create original book
        item = CatalogItem.objects.create(
            title="Dune",
            authors="Frank Herbert",
            item_type="BOOK"
        )
        
        url = reverse('catalog:api_sync_media')
        # 2. Sync Audible catalog entry
        payload = {
            "title": "Dune (Audible Audiobook)",
            "authors": "Frank Herbert",
            "asin": "B000R34DNE",
            "item_type": "AUDIBLE",
            "description": "Epic sci-fi audiobook",
            "cover_image_url": "http://example.com/dune_cover.jpg"
        }
        
        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'linked')
        
        # Dune item should now contain the ASIN and cover image
        item.refresh_from_db()
        self.assertEqual(item.asin, "B000R34DNE")
        self.assertEqual(item.cover_image_url, "http://example.com/dune_cover.jpg")
        self.assertEqual(CatalogItem.objects.count(), 1)

    def test_full_text_search(self):
        """Verify search query relevance mapping."""
        # Setup test data
        c1 = CatalogItem.objects.create(
            title="Neuromancer",
            authors="William Gibson",
            description="A classic cyberpunk science fiction novel.",
            item_type="BOOK"
        )
        c2 = CatalogItem.objects.create(
            title="Snow Crash",
            authors="Neal Stephenson",
            description="Cyberpunk metaverse deliverator delivery boy adventure.",
            item_type="BOOK"
        )
        
        # Search for "cyberpunk" (both have it, c1 has it in title description, c2 in description)
        results = perform_search("cyberpunk")
        self.assertEqual(len(results), 2)
        
        # Search for "Gibson" (should match c1)
        results = perform_search("Gibson")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], c1)
        
        # Search for "metaverse" (should match c2)
        results = perform_search("metaverse")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], c2)

    def test_audiobook_file_sync(self):
        """Verify syncing an audiobook file creates a CatalogItem with item_type='AUDIOBOOK'."""
        url = reverse('catalog:api_sync_file')
        payload = {
            "file_name": "dune_audiobook.m4b",
            "file_path": "/path/to/dune_audiobook.m4b",
            "file_size": 150485760,
            "file_format": "M4B",
            "file_hash": "hash_dune_m4b_999",
            "title": "Dune",
            "authors": "Frank Herbert",
            "item_type": "AUDIOBOOK"
        }
        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'created')
        
        item = CatalogItem.objects.get(title="Dune")
        self.assertEqual(item.item_type, "AUDIOBOOK")
        self.assertEqual(item.files.count(), 1)
        self.assertEqual(item.files.first().file_format, "M4B")

    def test_file_cleanup_api(self):
        """Verify file pruning and catalog item orphan cleanup via REST API."""
        item = CatalogItem.objects.create(
            title="Clean Code",
            authors="Robert C. Martin",
            item_type="BOOK"
        )
        loc1 = FileLocation.objects.create(
            catalog_item=item,
            file_path="/books/clean_code.epub",
            file_name="clean_code.epub",
            file_size=1024,
            file_format="EPUB",
            file_hash="hash_clean_code"
        )
        loc2 = FileLocation.objects.create(
            catalog_item=item,
            file_path="/books/subdir/clean_code.pdf",
            file_name="clean_code.pdf",
            file_size=2048,
            file_format="PDF",
            file_hash="hash_clean_code_pdf"
        )

        url = reverse('catalog:api_sync_cleanup')
        payload = {
            "library_path": "/books",
            "active_paths": [
                "/books/clean_code.epub"
            ]
        }

        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['deleted_locations'], 1)
        self.assertEqual(response.data['deleted_catalog_items'], 0)

        self.assertTrue(FileLocation.objects.filter(id=loc1.id).exists())
        self.assertFalse(FileLocation.objects.filter(id=loc2.id).exists())

        payload = {
            "library_path": "/books",
            "active_paths": []
        }
        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['deleted_locations'], 1)
        self.assertEqual(response.data['deleted_catalog_items'], 1)

        self.assertFalse(FileLocation.objects.filter(id=loc1.id).exists())
        self.assertFalse(CatalogItem.objects.filter(id=item.id).exists())

    def test_file_sync_invalid_pub_date(self):
        """Verify syncing a file with an invalid pub_date does not cause a 500 ValueError."""
        url = reverse('catalog:api_sync_file')
        payload = {
            "file_name": "corrupt_date_book.epub",
            "file_path": "/books/corrupt_date_book.epub",
            "file_size": 1048576,
            "file_format": "EPUB",
            "file_hash": "hash_corrupt_date_999",
            "title": "Book With Corrupt Date",
            "authors": "Some Author",
            "pub_date": "0000-00-00T00:00:00"  # Invalid month 0
        }
        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'created')
        item = CatalogItem.objects.get(title="Book With Corrupt Date")
        self.assertIsNone(item.pub_date)

    def test_file_cleanup_with_deleted_paths(self):
        """Verify targeted pruning via deleted_paths directly deletes locations and orphan items."""
        item = CatalogItem.objects.create(
            title="Refactoring",
            authors="Martin Fowler",
            item_type="BOOK"
        )
        loc = FileLocation.objects.create(
            catalog_item=item,
            file_path="/books/refactoring.epub",
            file_name="refactoring.epub",
            file_size=5000,
            file_format="EPUB",
            file_hash="hash_refactoring"
        )

        url = reverse('catalog:api_sync_cleanup')
        payload = {
            "deleted_paths": ["/books/refactoring.epub"]
        }
        response = self.client.post(url, payload, content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['deleted_locations'], 1)
        self.assertEqual(response.data['deleted_catalog_items'], 1)
        self.assertFalse(FileLocation.objects.filter(id=loc.id).exists())
        self.assertFalse(CatalogItem.objects.filter(id=item.id).exists())

    def test_wishlist_check_excludes_quarantined_and_invalid_files(self):
        """Verify /api/wishlist/check/ does not report books whose only files are quarantined or invalid."""
        from unittest.mock import patch
        from .opds import _ENRICHED_PATHS_CACHE

        q_item = CatalogItem.objects.create(
            title="oreilly_ssh_the_secure_shell_the_definitive_guide",
            authors="Unknown Author",
            isbn="9780596008956",
            item_type="BOOK",
        )
        FileLocation.objects.create(
            catalog_item=q_item,
            file_path="/books/Computer Hardware/ORE/oreilly_ssh_the_secure_shell_the_definitive_guide.pdf",
            file_name="oreilly_ssh_the_secure_shell_the_definitive_guide.pdf",
            file_size=5726208,
            file_format="PDF",
            file_hash="hash_quarantined_ssh",
            is_invalid=False,
        )

        inv_item = CatalogItem.objects.create(
            title="Corrupt Only Book",
            authors="Jane Doe",
            isbn="9780131103627",
            item_type="BOOK",
        )
        FileLocation.objects.create(
            catalog_item=inv_item,
            file_path="/books/corrupt_only.pdf",
            file_name="corrupt_only.pdf",
            file_size=0,
            file_format="PDF",
            file_hash="hash_corrupt_only",
            is_invalid=True,
        )

        valid_item = CatalogItem.objects.create(
            title="Valid Active Book",
            authors="Alice Smith",
            isbn="9780201616224",
            item_type="BOOK",
        )
        FileLocation.objects.create(
            catalog_item=valid_item,
            file_path="/books/valid_active_book.epub",
            file_name="valid_active_book.epub",
            file_size=4096,
            file_format="EPUB",
            file_hash="hash_valid_active",
            is_invalid=False,
        )

        url = reverse('catalog:api_wishlist_check')
        fake_status_map = {
            "/books/Computer Hardware/ORE/oreilly_ssh_the_secure_shell_the_definitive_guide.pdf": {
                "status": "QUARANTINED",
                "error": "PDF has 0 pages / is empty",
            }
        }
        with patch('catalog.opds.get_enriched_paths_set', return_value=set()), \
             patch.dict(_ENRICHED_PATHS_CACHE, {'status_by_path': fake_status_map}, clear=False):
            resp = self.client.post(
                url,
                {
                    "books": [
                        {"key": "q1", "isbn": "9780596008956", "title": "oreilly_ssh_the_secure_shell_the_definitive_guide"},
                        {"key": "q2", "isbn": "9780131103627", "title": "Corrupt Only Book", "author": "Jane Doe"},
                        {"key": "v1", "isbn": "9780201616224", "title": "Valid Active Book", "author": "Alice Smith"},
                    ]
                },
                content_type='application/json',
                HTTP_HOST='localhost',
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = {r['key']: r for r in resp.data['results']}
        self.assertFalse(results['q1']['in_library'])
        self.assertFalse(results['q2']['in_library'])
        self.assertTrue(results['v1']['in_library'])

    def test_quarantined_missing_shelf_and_dismiss(self):
        """Verify Quarantined (Missing) shelf separates missing vs covered books and supports dismiss/restore."""
        import sqlite3
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        # Create a valid copy of SSH, The Secure Shell in BookDB
        ssh_item = CatalogItem.objects.create(
            title="SSH, The Secure Shell: The Definitive Guide",
            authors="Daniel J. Barrett, Richard E. Silverman",
            isbn="9780596000110",
            item_type="BOOK",
        )
        FileLocation.objects.create(
            catalog_item=ssh_item,
            file_path="/books/Computer Mags & FAQ/SSH/SSH, The Secure Shell.epub",
            file_name="SSH, The Secure Shell.epub",
            file_size=3633770,
            file_format="EPUB",
            file_hash="hash_valid_ssh_epub",
            is_invalid=False,
        )

        # Create a temporary SQLite scanner_state.db with two QUARANTINED rows:
        # 1) oreilly_ssh_the_secure_shell_the_definitive_guide.pdf -> ALREADY covered by ssh_item
        # 2) OReilly Unix for Oracle DBAs Pocket Reference.pdf -> MISSING valid copy!
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_db = Path(tmpdir) / "scanner_state.db"
            conn = sqlite3.connect(str(tmp_db))
            conn.execute(
                """
                CREATE TABLE processed_files (
                    file_path TEXT PRIMARY KEY,
                    file_name TEXT,
                    file_ext TEXT,
                    file_size INTEGER,
                    status TEXT,
                    isbn TEXT,
                    title TEXT,
                    author TEXT,
                    orig_title TEXT,
                    orig_author TEXT,
                    orig_isbn TEXT,
                    error_message TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO processed_files
                (file_path, file_name, file_ext, file_size, status, error_message, updated_at)
                VALUES (?, ?, ?, ?, 'QUARANTINED', ?, '2026-11-24 09:24:51')
                """,
                (
                    "/books/Computer Hardware/ORE/oreilly_ssh_the_secure_shell_the_definitive_guide.pdf",
                    "oreilly_ssh_the_secure_shell_the_definitive_guide.pdf",
                    ".pdf",
                    5726208,
                    "PDF has 0 pages / is empty",
                ),
            )
            conn.execute(
                """
                INSERT INTO processed_files
                (file_path, file_name, file_ext, file_size, status, error_message, updated_at)
                VALUES (?, ?, ?, ?, 'QUARANTINED', ?, '2026-11-24 09:27:59')
                """,
                (
                    "/books/Computer Hardware/ORE/OReilly Unix for Oracle DBAs Pocket Reference.pdf",
                    "OReilly Unix for Oracle DBAs Pocket Reference.pdf",
                    ".pdf",
                    1048576,
                    "PDF has 0 pages / is empty",
                ),
            )
            conn.commit()
            conn.close()

            with patch('catalog.opds._get_scanner_db_path', return_value=tmp_db), \
                 patch('catalog.opds.is_postgres_scanner_enabled', return_value=False), \
                 patch('catalog.opds.connect_scanner_db', side_effect=lambda p, timeout=5.0: sqlite3.connect(str(p))):
                api_url = reverse('catalog:api_quarantined_missing')
                r1 = self.client.get(f"{api_url}?refresh=1", HTTP_HOST='localhost')
                self.assertEqual(r1.status_code, status.HTTP_200_OK)
                self.assertEqual(r1.data['counts']['missing'], 1)
                self.assertEqual(r1.data['counts']['covered'], 1)
                missing_item = r1.data['items'][0]
                self.assertIn("Oracle DBAs Pocket Reference", missing_item['title'])
                self.assertEqual(missing_item['publisher_hint'], "O'Reilly")

                # Verify DashboardView ?shelf=quarantined_missing renders cleanly
                dash_resp = self.client.get("/?shelf=quarantined_missing", HTTP_HOST='localhost')
                self.assertEqual(dash_resp.status_code, 200)
                self.assertContains(dash_resp, "Oracle DBAs Pocket Reference")

                # Dismiss the missing item
                qk = missing_item['quar_key']
                r_dism = self.client.post(
                    api_url,
                    {"action": "dismiss", "quar_key": qk},
                    content_type='application/json',
                    HTTP_HOST='localhost',
                )
                self.assertEqual(r_dism.status_code, status.HTTP_200_OK)
                self.assertEqual(r_dism.data['counts']['missing'], 0)
                self.assertEqual(r_dism.data['counts']['dismissed'], 1)

                # Restore it
                r_rest = self.client.post(
                    api_url,
                    {"action": "restore", "quar_key": qk},
                    content_type='application/json',
                    HTTP_HOST='localhost',
                )
                self.assertEqual(r_rest.status_code, status.HTTP_200_OK)
                self.assertEqual(r_rest.data['counts']['missing'], 1)
                self.assertEqual(r_rest.data['counts']['dismissed'], 0)

    def test_trailing_year_in_title_cleaned_and_matched(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path('nas_scanner').resolve()))
        from epubmeta.filename_parser import clean_book_title, strip_trailing_year_tag
        from epubmeta.matcher import score_title

        self.assertEqual(clean_book_title("Way Of the Lawless (1921)"), "Way Of the Lawless")
        self.assertEqual(clean_book_title("The Time Machine [1895]"), "The Time Machine")
        self.assertEqual(clean_book_title("1984"), "1984")
        self.assertEqual(strip_trailing_year_tag("2001: A Space Odyssey"), "2001: A Space Odyssey")
        self.assertGreaterEqual(score_title("The Way of the Lawless", "Way Of the Lawless (1921)"), 0.95)

    def test_user_selected_cover_indicator(self):
        from PIL import Image
        import io
        import base64
        from catalog.opds import apply_book_cover_to_item, has_user_selected_cover, revert_book_cover_to_original

        im = Image.new('RGB', (300, 450), (30, 120, 220))
        buf = io.BytesIO()
        im.save(buf, format='JPEG')
        data_uri = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')

        item = CatalogItem.objects.create(
            title="Virtue",
            authors="Serena Mackesy",
            item_type="BOOK",
        )
        res = apply_book_cover_to_item(item, data_uri, user_selected=True)
        self.assertTrue(res.get('ok'))
        self.assertTrue(res.get('cover_updated'))
        self.assertTrue(has_user_selected_cover(item.id))

        rev = revert_book_cover_to_original(item)
        self.assertTrue(rev.get('ok'))
        self.assertFalse(rev.get('cover_updated'))
        self.assertFalse(has_user_selected_cover(item.id))

    def test_truncated_description_detection_and_merge(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path('nas_scanner').resolve()))
        from epubmeta.models import BookMetadata, is_truncated_description

        trunc = (
            "THOMAS VAN ZANDT is 16, bored, and impatient. Born with the ability to control his "
            "autonomic nervous system, he has one goal in to bring his abilities outside his own body to \u2026"
        )
        dramatic_full = (
            "Saints, sinners, and the mere mortals in between. What do you need to be a saint these days? "
            "Ambition, determination and good PR. Happily settled in a chaotic flat and armed with a wicked "
            "sense of humour, they set out to paint the town red. And for a while life goes swimmingly. "
            "But when they tread on one toe too many, they find that they have only their worst instincts "
            "- and each other - to rely on. Be good. And if you can't be good, be careful..."
        )
        self.assertTrue(is_truncated_description(trunc))
        self.assertTrue(is_truncated_description("Short blurb cut off mid-thought..."))
        self.assertFalse(is_truncated_description(dramatic_full))

        m_trunc = BookMetadata(title="Systematic", authors=["Andrea Ring"], description=trunc)
        m_full = BookMetadata(title="Systematic", authors=["Andrea Ring"], description=dramatic_full)
        merged = m_trunc.merge_with(m_full)
        self.assertEqual(merged.description, dramatic_full)

    def test_non_english_description_discarded_for_english_book(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path('nas_scanner').resolve()))
        from epubmeta.models import BookMetadata, is_non_english_for_book

        german_desc = (
            "„Der tapfere kleine Held zieht erneut aus, um gegen die Feinde des Geborgenen Landes "
            "anzutreten. Diesmal machen ihm die gefährlichsten Wesen zu schaffen: üble Halbkreaturen, "
            "teils Albae, teils Orks, die sich mit todbringenden Maschinen umgeben.“"
        )
        en_desc = (
            "Though hailed a hero by his people, the course of life has not run smooth for the "
            "battle-weary Tungdil the dwarf. Now he must face the most formidable enemy the kingdom has encountered."
        )
        self.assertTrue(is_non_english_for_book(german_desc, "Revenge of the Dwarves"))
        self.assertFalse(is_non_english_for_book(german_desc, "Die Rache der Zwerge"))
        self.assertFalse(is_non_english_for_book(en_desc, "Revenge of the Dwarves"))

        ol_meta = BookMetadata(title="Revenge of the Dwarves", authors=["Markus Heitz"], description=german_desc)
        emb_meta = BookMetadata(title="Revenge of the Dwarves", authors=["Markus Heitz"], description=en_desc)
        self.assertEqual(ol_meta.merge_with(emb_meta).description, en_desc)

    def test_switched_title_and_author_enrichment_and_matcher(self):
        import sys
        import tempfile
        import zipfile
        from pathlib import Path

        sys.path.insert(0, str(Path('nas_scanner').resolve()))
        from epubmeta.authors import (
            clean_swapped_author_candidates,
            clean_swapped_title_candidates,
            looks_like_swapped_title_and_author,
        )
        from epubmeta.matcher import calculate_match_confidence, score_authors
        from epubmeta.models import BookMetadata
        from epubmeta.processor import BookProcessor
        from epubmeta.readers.epub import EpubReader

        # 1. Verify '(ed)' role stripping, 'Last, First', and semicolon-split title reconstruction
        primary_a, all_a = clean_swapped_author_candidates("Martin H. Greenberg (ed)")
        self.assertEqual(primary_a, "Martin H. Greenberg")
        self.assertEqual(all_a, ["Martin H. Greenberg"])
        self.assertEqual(score_authors(["Martin H. Greenberg"], ["Martin H. Greenberg (ed)"]), 1.0)
        self.assertEqual(score_authors(["Maureen Driscoll"], ["Driscoll, Maureen"]), 1.0)

        sw_titles = clean_swapped_title_candidates("The Emperor;the Maula")
        self.assertEqual(sw_titles[0], "The Emperor and the Maula")
        self.assertTrue(looks_like_swapped_title_and_author("Martin H. Greenberg (ed)", "Journey to Infinity"))
        self.assertTrue(looks_like_swapped_title_and_author("Lester Del Rey", "The Man Without A Planet"))
        self.assertTrue(looks_like_swapped_title_and_author("Robert Asprin - Thieves World 03", "Shadows of Sanctuary"))
        self.assertFalse(looks_like_swapped_title_and_author("Journey to Infinity", "Martin H. Greenberg"))

        # 1b. Verify compound '<Author> - <Series #>' author extraction, redundant author/series stripping, and 'Title by Author' extraction
        from epubmeta.authors import (
            extract_inline_author_and_clean_title,
            strip_series_and_redundant_author_from_title,
        )
        from epubmeta.filename_parser import parse_filename
        from .models import parse_and_normalize_authors

        tw_a, tw_all = clean_swapped_author_candidates("Robert Asprin - Thieves World 03")
        self.assertEqual(tw_a, "Robert Asprin")
        self.assertEqual(tw_all, ["Robert Asprin"])

        phule_clean = strip_series_and_redundant_author_from_title(
            "Robert Asprin - Phule 04 - Phule Me Twice",
            ["Robert Asprin", "Peter J. Heck"],
        )
        self.assertEqual(phule_clean, "Phule Me Twice")

        fn_phule = parse_filename("Robert Asprin - [Phule 04] - Phule Me Twice Robert Asprin & Peter J Heck (epub).epub")
        self.assertEqual(fn_phule["title"], "Phule Me Twice")
        self.assertEqual(fn_phule["authors"], ["Robert Asprin"])

        fn_weeks = parse_filename("Night Angel Trilogy 01 - Way of Shadows - Brent Weeks.epub")
        self.assertEqual(fn_weeks["title"], "Way of Shadows")
        self.assertEqual(fn_weeks["authors"], ["Brent Weeks"])

        self.assertEqual(
            parse_and_normalize_authors("Robert Lynn Asprin - editor, Lynn Abbey - editor"),
            ["Robert Lynn Asprin", "Lynn Abbey"],
        )

        # Inline 'Title by Author' and 'Title (by Author)' extraction vs false-positive protection
        self.assertEqual(
            extract_inline_author_and_clean_title("Long Calculation by Edward Winter", file_name="Edward Winter - Long Calculation.pdf"),
            ("Long Calculation", ["Edward Winter"]),
        )
        self.assertEqual(
            extract_inline_author_and_clean_title("Reinfeld’s Non-Chess Books (by Edward Winter)"),
            ("Reinfeld’s Non-Chess Books", ["Edward Winter"]),
        )
        self.assertEqual(
            extract_inline_author_and_clean_title("Science Fiction by Gaslight"),
            (None, []),
        )
        self.assertEqual(
            extract_inline_author_and_clean_title("Death by Black Hole"),
            (None, []),
        )

        # 2. Verify calculate_match_confidence accepts genuine switched match and rejects false positives
        emb_swapped = BookMetadata(title="Bertrand R. Brinley", authors=["The Mad Scientists' Club"])
        cand_real = BookMetadata(title="The Mad Scientists' Club", authors=["Bertrand R. Brinley"])
        res_ok = calculate_match_confidence(cand_real, filename_info={}, existing_meta=emb_swapped, min_confidence=0.50)
        self.assertTrue(res_ok.is_confident)
        self.assertGreaterEqual(res_ok.score, 0.95)

        cand_wrong_author = BookMetadata(title="The Mad Scientists' Club", authors=["Someone Else"])
        res_bad = calculate_match_confidence(cand_wrong_author, filename_info={}, existing_meta=emb_swapped, min_confidence=0.50)
        self.assertFalse(res_bad.is_confident)

        # 3. Verify BookProcessor.process_file fixes an EPUB whose embedded OPF has Title & Author switched
        class DummySwitchedAggregator:
            def fetch_by_isbn(self, isbn):
                return None

            def fetch_by_query(self, title, author=None):
                if title.lower() == "journey to infinity" and author and "greenberg" in author.lower():
                    return BookMetadata(
                        title="Journey to Infinity",
                        authors=["Martin H. Greenberg"],
                        publisher="Gnome Press",
                        published_date="1951",
                        description="Classic science fiction anthology.",
                        subjects=["Science fiction"],
                        source="dummy-catalog",
                    )
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            epub_path = Path(tmpdir) / "The Mad File.epub"
            with zipfile.ZipFile(epub_path, "w") as zf:
                zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
                zf.writestr(
                    "META-INF/container.xml",
                    '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
                )
                zf.writestr(
                    "content.opf",
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    '<dc:title>Martin H. Greenberg (ed)</dc:title>'
                    '<dc:creator>Journey to Infinity</dc:creator>'
                    '</metadata><manifest/><spine/></package>',
                )

            proc = BookProcessor(aggregator=DummySwitchedAggregator(), ai_extractor=None, backup=False)
            out = proc.process_file(epub_path)
            self.assertEqual(out["status"], "SUCCESS")
            self.assertEqual(out["title"], "Journey to Infinity")
            self.assertEqual(out["author"], "Martin H. Greenberg")

            updated_opf = EpubReader(epub_path).read_metadata()
            self.assertEqual(updated_opf.title, "Journey to Infinity")
            self.assertEqual(updated_opf.authors, ["Martin H. Greenberg"])






