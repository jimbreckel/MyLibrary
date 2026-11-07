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

