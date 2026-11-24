import datetime
import os
from django.conf import settings
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import BasePermission
from django.utils.dateparse import parse_date, parse_datetime
from .models import CatalogItem, FileLocation
from .search import find_similar_item

def clean_val(val):
    if isinstance(val, str):
        return val.replace('\x00', '')
    elif isinstance(val, dict):
        return {k: clean_val(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [clean_val(x) for x in val]
    return val

def truncate_str(val, max_len):
    if val is None:
        return None
    s = str(val).strip()
    return s[:max_len] if len(s) > max_len else s

def safe_parse_date(val):
    if not val:
        return None
    try:
        return parse_date(str(val)[:10])
    except (ValueError, TypeError):
        return None

def safe_parse_datetime(val):
    if not val:
        return None
    try:
        dt = parse_datetime(str(val))
        if dt and timezone.is_naive(dt):
            dt = timezone.make_aware(dt, datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


class IsScannerTokenAuthenticated(BasePermission):
    """Custom permission to authorize sync scripts using the API token."""
    def has_permission(self, request, view):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return False
        token = auth_header.split('Bearer ')[1].strip()
        return token == settings.SCANNER_API_TOKEN

class FileSyncAPIView(APIView):
    """
    API endpoint for remote folder scanners to sync physical files (EPUB, PDF, MOBI).
    Handles incremental updates, file location changes, and fuzzy matching to catalog items.
    """
    permission_classes = [IsScannerTokenAuthenticated]

    def post(self, request):
        data = clean_val(request.data)
        
        # Required file fields
        file_hash = truncate_str(data.get('file_hash'), 64)
        file_path = data.get('file_path')
        file_name = truncate_str(data.get('file_name'), 512)
        file_size = data.get('file_size')
        file_format = truncate_str(data.get('file_format', 'UNKNOWN'), 16)
        is_invalid = data.get('is_invalid', False)
        has_drm = data.get('has_drm', False)
        
        # Optional library details
        library_name = truncate_str(data.get('library_name'), 256)
        library_path = data.get('library_path')
        
        if not all([file_hash, file_path, file_name]) or file_size is None:
            return Response(
                {"error": "Missing required file metadata fields."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        # Parse optional timestamps safely
        created_at = safe_parse_datetime(data.get('file_created_at'))
        modified_at = safe_parse_datetime(data.get('file_modified_at'))

        # Required catalog metadata for fuzzy match
        title = truncate_str(data.get('title') or '', 512)
        if title:
            try:
                from .opds import _clean_book_stem_for_grouping
                cleaned_t = _clean_book_stem_for_grouping(title)
                if cleaned_t and len(cleaned_t) >= 2:
                    title = truncate_str(cleaned_t, 512)
            except Exception:
                pass
        if not title:
            title = file_name
        authors = truncate_str(data.get('authors') or '', 512)
        if not authors:
            authors = 'Unknown Author'
        isbn = truncate_str(data.get('isbn'), 32)
        asin = truncate_str(data.get('asin'), 32)
        if isbn and str(isbn).strip().upper().startswith('B') and len(str(isbn).strip()) == 10:
            asin = asin or str(isbn).strip().upper()
            isbn = None
        publisher = truncate_str(data.get('publisher'), 256)
        description = data.get('description')
        item_type = truncate_str(data.get('item_type', 'BOOK'), 16)
        fp_norm = str(file_path or '').replace('\\', '/')
        if (
            fp_norm.startswith(('/periodicals/', '/toMags/', '/xAdult/', '/xtoAdult/'))
            or '/Media/Periodicals/' in fp_norm
            or '/Media/toMags/' in fp_norm
            or '/Media/xAdult/' in fp_norm
            or '/Media/xtoAdult/' in fp_norm
        ):
            item_type = 'MAGAZINE'
        cover_thumbnail = data.get('cover_thumbnail')
        pub_date = safe_parse_date(data.get('pub_date'))

        # 1. Check if this exact file (by hash) is already registered
        file_loc = FileLocation.objects.select_related('catalog_item').filter(file_hash=file_hash).first()
        if file_loc:
            if item_type == 'MAGAZINE' and file_loc.catalog_item and file_loc.catalog_item.item_type != 'MAGAZINE':
                file_loc.catalog_item.item_type = 'MAGAZINE'
                file_loc.catalog_item.save(update_fields=['item_type', 'updated_at'])
            # File is already in database. Check if its location/path or library info has changed
            if file_loc.file_path != file_path or file_loc.library_name != library_name or file_loc.library_path != library_path:
                file_loc.file_path = file_path
                file_loc.file_name = file_name
                file_loc.library_name = library_name
                file_loc.library_path = library_path
                file_loc.save()
                return Response({"status": "updated_location", "id": file_loc.id}, status=status.HTTP_200_OK)
            return Response({"status": "already_synced", "id": file_loc.id}, status=status.HTTP_200_OK)

        # 2. Check if a file exists by orig_file_hash (indexed) or at the exact path, with a different hash (file modified)
        orig_file_hash = data.get('orig_file_hash')
        file_loc = None
        if orig_file_hash:
            file_loc = FileLocation.objects.filter(file_hash=orig_file_hash).first()
        if not file_loc:
            file_loc = FileLocation.objects.filter(file_path=file_path).first()
        if file_loc:
            # File has changed, update metadata
            file_loc.file_hash = file_hash
            file_loc.file_size = file_size
            file_loc.file_name = file_name
            if modified_at is not None:
                file_loc.file_modified_at = modified_at
            if created_at is not None and not file_loc.file_created_at:
                file_loc.file_created_at = created_at
            file_loc.is_invalid = is_invalid
            file_loc.has_drm = has_drm
            if library_name:
                file_loc.library_name = library_name
            if library_path:
                file_loc.library_path = library_path
            file_loc.save()
            
            # Update the catalog item with enriched metadata from the modified file
            item = file_loc.catalog_item
            try:
                from .opds import ensure_isolated_catalog_item_for_file
                item = ensure_isolated_catalog_item_for_file(
                    item,
                    file_loc,
                    new_title=title or '',
                    new_authors=authors or '',
                )
            except Exception:
                pass
            updated = False
            if title and title != file_name and item.title != title:
                item.title = title
                updated = True
            if authors and authors != 'Unknown Author' and item.authors != authors:
                item.authors = authors
                updated = True
            if description and item.description != description:
                item.description = description
                updated = True
            if isbn and item.isbn != isbn:
                item.isbn = isbn
                updated = True
            if asin and item.asin != asin:
                item.asin = asin
                updated = True
            if publisher and item.publisher != publisher:
                item.publisher = publisher
                updated = True
            if pub_date and item.pub_date != pub_date:
                item.pub_date = pub_date
                updated = True
            if item_type == 'MAGAZINE' and item.item_type != 'MAGAZINE':
                item.item_type = 'MAGAZINE'
                updated = True
            if cover_thumbnail:
                if not item.cover_thumbnail:
                    item.cover_thumbnail = cover_thumbnail
                    updated = True
                else:
                    try:
                        from .opds import (
                            classify_cover_image,
                            get_original_cover_backup_for_item,
                            should_prefer_candidate_cover,
                        )
                        if (
                            classify_cover_image(item.cover_thumbnail, item.cover_image_url) == 'text_only'
                            and classify_cover_image(cover_thumbnail) is None
                        ) or (
                            get_original_cover_backup_for_item(item.id) is None
                            and should_prefer_candidate_cover(item.cover_thumbnail, cover_thumbnail)
                        ):
                            item.cover_thumbnail = cover_thumbnail
                            updated = True
                    except Exception:
                        pass
            if updated:
                item.save()
            try:
                from .opds import evaluate_and_record_item_cover_status, is_file_enriched, merge_duplicate_catalog_items_for_item
                if orig_file_hash or is_file_enriched(file_path):
                    merge_duplicate_catalog_items_for_item(item)
                    evaluate_and_record_item_cover_status(item)
            except Exception:
                pass
            return Response({"status": "updated_content", "id": file_loc.id}, status=status.HTTP_200_OK)

        # 3. Completely new file instance. Try to find a matching CatalogItem (fuzzy match)
        item = find_similar_item(title=title, authors=authors, isbn=isbn, asin=asin)
        if item and item_type == 'MAGAZINE' and item.item_type != 'MAGAZINE':
            existing_paths = list(item.files.values_list('file_path', flat=True))
            if not existing_paths or all(
                str(p or '').startswith(('/periodicals/', '/toMags/', '/xAdult/', '/xtoAdult/'))
                for p in existing_paths
            ):
                item.item_type = 'MAGAZINE'
                item.save(update_fields=['item_type', 'updated_at'])
            else:
                item = None
        
        created_new_item = False
        if not item:
            # Create a new central CatalogItem
            item = CatalogItem.objects.create(
                title=title,
                authors=authors,
                publisher=publisher,
                pub_date=pub_date,
                isbn=isbn,
                asin=asin,
                item_type=item_type,
                description=description,
                cover_thumbnail=cover_thumbnail
            )
            created_new_item = True
        else:
            # Update existing CatalogItem missing details with new data
            updated = False
            if item_type == 'MAGAZINE' and item.item_type != 'MAGAZINE':
                item.item_type = 'MAGAZINE'
                updated = True
            if isbn and not item.isbn:
                item.isbn = isbn
                updated = True
            if asin and not item.asin:
                item.asin = asin
                updated = True
            if publisher and not item.publisher:
                item.publisher = publisher
                updated = True
            if description and not item.description:
                item.description = description
                updated = True
            if cover_thumbnail:
                if not item.cover_thumbnail:
                    item.cover_thumbnail = cover_thumbnail
                    updated = True
                else:
                    try:
                        from .opds import get_original_cover_backup_for_item, should_prefer_candidate_cover
                        if (
                            get_original_cover_backup_for_item(item.id) is None
                            and should_prefer_candidate_cover(item.cover_thumbnail, cover_thumbnail)
                        ):
                            item.cover_thumbnail = cover_thumbnail
                            updated = True
                    except Exception:
                        pass
            if updated:
                item.save()

        # Create FileLocation linked to the CatalogItem
        file_loc = FileLocation.objects.create(
            catalog_item=item,
            file_path=file_path,
            file_name=file_name,
            file_size=file_size,
            file_format=file_format,
            file_created_at=created_at,
            file_modified_at=modified_at,
            file_hash=file_hash,
            is_invalid=is_invalid,
            has_drm=has_drm,
            library_name=library_name,
            library_path=library_path
        )

        resp_status = status.HTTP_201_CREATED if created_new_item else status.HTTP_200_OK
        return Response({
            "status": "created" if created_new_item else "linked",
            "catalog_item_id": item.id,
            "file_location_id": file_loc.id
        }, status=resp_status)


class MediaSyncAPIView(APIView):
    """
    API endpoint for syncing digital purchases (Kindle, Audible).
    Integrates items into the central database and fuzzy matches with local physical files.
    """
    permission_classes = [IsScannerTokenAuthenticated]

    def post(self, request):
        def clean_val(val):
            if isinstance(val, str):
                return val.replace('\x00', '')
            elif isinstance(val, dict):
                return {k: clean_val(v) for k, v in val.items()}
            elif isinstance(val, list):
                return [clean_val(x) for x in val]
            return val
            
        data = clean_val(request.data)
        title = truncate_str(data.get('title'), 512)
        authors = truncate_str(data.get('authors'), 512)
        item_type = truncate_str(data.get('item_type'), 16) # KINDLE or AUDIBLE
        
        if not all([title, authors, item_type]):
            return Response(
                {"error": "Missing required media fields (title, authors, item_type)."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
            
        asin = truncate_str(data.get('asin'), 32)
        isbn = truncate_str(data.get('isbn'), 32)
        publisher = truncate_str(data.get('publisher'), 256)
        description = data.get('description')
        cover_image_url = truncate_str(data.get('cover_image_url'), 1024)
        cover_thumbnail = data.get('cover_thumbnail')

        # Find existing match
        item = find_similar_item(title=title, authors=authors, isbn=isbn, asin=asin)
        
        if item:
            # Linked to existing item. Let's merge details.
            updated = False
            
            # If the item was a generic file-based 'BOOK', but now we get a specific 'KINDLE' or 'AUDIBLE'
            # version, we keep the catalog item unified, but we store the ASIN and cover images.
            if asin and not item.asin:
                item.asin = asin
                updated = True
            if cover_image_url and not item.cover_image_url:
                item.cover_image_url = cover_image_url
                updated = True
            if cover_thumbnail:
                if not item.cover_thumbnail:
                    item.cover_thumbnail = cover_thumbnail
                    updated = True
                else:
                    try:
                        from .opds import get_original_cover_backup_for_item, should_prefer_candidate_cover
                        if (
                            get_original_cover_backup_for_item(item.id) is None
                            and should_prefer_candidate_cover(item.cover_thumbnail, cover_thumbnail)
                        ):
                            item.cover_thumbnail = cover_thumbnail
                            updated = True
                    except Exception:
                        pass
            if description and not item.description:
                item.description = description
                updated = True
            if publisher and not item.publisher:
                item.publisher = publisher
                updated = True
                
            # If the existing entry was created from local file, but this is Kindle/Audible,
            # we keep the item_type as BOOK if it has files, but if it was created without files,
            # we can keep it or change it. Unified catalog handles this.
            if updated:
                item.save()
                
            return Response({"status": "linked", "catalog_item_id": item.id}, status=status.HTTP_200_OK)
        else:
            # Create a brand new catalog entry
            item = CatalogItem.objects.create(
                title=title,
                authors=authors,
                publisher=publisher,
                asin=asin,
                isbn=isbn,
                item_type=item_type,
                description=description,
                cover_image_url=cover_image_url,
                cover_thumbnail=cover_thumbnail
            )
            return Response({"status": "created", "catalog_item_id": item.id}, status=status.HTTP_201_CREATED)


class AmazonBatchSyncAPIView(APIView):
    """
    Batch API endpoint (`/mylibrary/api/sync/amazon/`) for `scanner/sync_amazon.py`
    to sync Purchased on Amazon, Kindle+ (Kindle Unlimited / Prime Reading),
    Kindle Bookmarks/Highlights, and Audible Audiobooks.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from .opds import get_dynamic_amazon_shelf

        amz = get_dynamic_amazon_shelf()
        resp_data = {
            "ok": True,
            "counts": amz.get("counts", {}),
        }
        if request.GET.get("missing") == "1":
            missing_kindle = []
            missing_audible = []
            for cid in amz.get("amazon_only_ids") or []:
                rec = (amz.get("by_item_id") or {}).get(str(cid)) or {}
                if rec.get("item_type") == "AUDIBLE":
                    missing_audible.append(rec)
                else:
                    missing_kindle.append({
                        "id": str(cid),
                        "asin": rec.get("asin") or "",
                        "title": rec.get("title") or "",
                        "authors": rec.get("authors") or "",
                        "origin_type": rec.get("origin_type") or "",
                        "is_kindle_plus": bool(rec.get("is_kindle_plus")),
                    })
            resp_data["missing_kindle"] = missing_kindle
            resp_data["missing_audible_count"] = len(missing_audible)
        return Response(resp_data)

    def post(self, request):
        from .opds import save_and_link_amazon_items
        from .views import _MAIN_DASHBOARD_CACHE

        body = clean_val(request.data or {})
        items = body.get("items") or []
        if not isinstance(items, list):
            return Response({"error": "'items' must be a list."}, status=status.HTTP_400_BAD_REQUEST)

        synced_types = body.get("synced_types") or None
        is_final_chunk = bool(body.get("is_final_chunk", False))
        res = save_and_link_amazon_items(items, synced_types=synced_types, is_final_chunk=is_final_chunk)
        _MAIN_DASHBOARD_CACHE["expires_at"] = 0.0
        return Response(res, status=status.HTTP_200_OK)


class PublisherOwnedBatchSyncAPIView(APIView):
    """
    Batch API endpoint (`/mylibrary/api/sync/owned/`) for `scanner/sync_owned.py`
    (`sync_owned.bat`) to sync Packt Owned Titles (`https://subscription.packtpub.com/owned`)
    and Springer Owned Titles (`https://library.springer.com/`) into MyLibrary.
    """
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        from .opds import get_dynamic_publisher_owned_shelf

        pub = get_dynamic_publisher_owned_shelf()
        resp_data = {
            "ok": True,
            "counts": pub.get("counts", {}),
        }
        if request.GET.get("missing") == "1":
            missing_list = []
            for cid in pub.get("publisher_only_ids") or []:
                rec = (pub.get("by_item_id") or {}).get(str(cid)) or {}
                missing_list.append({
                    "id": str(cid),
                    "source": rec.get("source"),
                    "isbn": rec.get("isbn") or rec.get("ebook_isbn") or rec.get("print_isbn") or "",
                    "title": rec.get("title"),
                    "authors": rec.get("authors"),
                    "is_video": bool(rec.get("is_video")),
                    "url": rec.get("web_reader_url") or "",
                })
            resp_data["missing_items"] = missing_list
        if request.GET.get("disappeared") == "1":
            from .opds import get_dynamic_amazon_shelf
            amz = get_dynamic_amazon_shelf()
            disappeared = []
            for cid in amz.get("amazon_missing_owned_ids") or []:
                rec = (amz.get("by_item_id") or {}).get(str(cid)) or {}
                disappeared.append({
                    "id": str(cid),
                    "source": rec.get("item_type") or "KINDLE",
                    "identifier": rec.get("asin") or "",
                    "title": rec.get("title") or "",
                    "authors": rec.get("authors") or "",
                    "missing_since": rec.get("missing_since") or "",
                    "has_local_file": bool(rec.get("has_local_file")),
                })
            for cid in pub.get("publisher_missing_owned_ids") or []:
                rec = (pub.get("by_item_id") or {}).get(str(cid)) or {}
                disappeared.append({
                    "id": str(cid),
                    "source": rec.get("source") or "PACKT",
                    "identifier": rec.get("isbn") or "",
                    "title": rec.get("title") or "",
                    "authors": rec.get("authors") or "",
                    "missing_since": rec.get("missing_since") or "",
                    "has_local_file": bool(rec.get("has_local_file")),
                })
            resp_data["disappeared_items"] = disappeared
        return Response(resp_data)

    def post(self, request):
        from .opds import save_and_link_publisher_owned_items
        from .views import _MAIN_DASHBOARD_CACHE

        body = clean_val(request.data or {})
        items = body.get("items") or []
        if not isinstance(items, list):
            return Response({"error": "'items' must be a list."}, status=status.HTTP_400_BAD_REQUEST)

        synced_sources = body.get("synced_sources") or None
        is_final_chunk = bool(body.get("is_final_chunk", False))
        res = save_and_link_publisher_owned_items(items, synced_sources=synced_sources, is_final_chunk=is_final_chunk)
        _MAIN_DASHBOARD_CACHE["expires_at"] = 0.0
        return Response(res, status=status.HTTP_200_OK)


class CalibreAddAPIView(APIView):
    """
    API endpoint to add a physical file to the Calibre library using 'calibredb'.
    """
    def post(self, request):
        import os
        import subprocess
        import re
        import json
        import sqlite3
        from scanner.utils.metadata_parser import extract_metadata
        
        file_location_id = request.data.get('file_location_id')
        if not file_location_id:
            return Response({"error": "Missing file_location_id"}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            file_loc = FileLocation.objects.get(id=file_location_id)
        except FileLocation.DoesNotExist:
            return Response({"error": "File location not found"}, status=status.HTTP_404_NOT_FOUND)
            
        # Get Calibre libraries from config
        calibre_libraries = []
        fallback_dir = r"P:\Books-Add Only"
        config_path = os.path.join(settings.BASE_DIR, 'scanner', 'config.json')
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    calibre_libraries = config.get('calibre_libraries', [])
                    fallback_dir = config.get('fallback_add_only_dir', fallback_dir)
            except Exception:
                pass
                
        if not calibre_libraries:
            return Response({"error": "No Calibre libraries configured in config.json"}, status=status.HTTP_400_BAD_REQUEST)
            
        # Use primary library (first configured)
        primary_lib = calibre_libraries[0]
        lib_path = primary_lib.get('path')
        lib_name = primary_lib.get('name', 'Calibre Library')
        
        file_path = file_loc.file_path
        if not os.path.exists(file_path):
            return Response({"error": f"Source file does not exist on server: {file_path}"}, status=status.HTTP_400_BAD_REQUEST)

        if not lib_path or not os.path.exists(lib_path):
            # Fallback path to NAS
            try:
                import shutil
                os.makedirs(fallback_dir, exist_ok=True)
                dest_file_path = os.path.join(fallback_dir, file_loc.file_name)
                
                # Check for name collisions
                base, ext_name = os.path.splitext(file_loc.file_name)
                counter = 1
                while os.path.exists(dest_file_path):
                    dest_file_path = os.path.join(fallback_dir, f"{base}_{counter}{ext_name}")
                    counter += 1
                    
                shutil.copy2(file_path, dest_file_path)
                
                # Extract and register new file location
                meta = extract_metadata(dest_file_path)
                new_loc = FileLocation.objects.create(
                    catalog_item=file_loc.catalog_item,
                    file_path=dest_file_path,
                    file_name=os.path.basename(dest_file_path),
                    file_size=meta['file_size'],
                    file_format=meta['file_format'],
                    file_created_at=parse_datetime(meta['file_created_at']),
                    file_modified_at=parse_datetime(meta['file_modified_at']),
                    file_hash=meta['file_hash'],
                    is_invalid=meta.get('is_invalid', False),
                    has_drm=meta.get('has_drm', False),
                    library_name="NAS Add-Only Backup",
                    library_path=fallback_dir
                )
                
                return Response({
                    "status": "success",
                    "message": f"Calibre library was not available. Successfully copied book to NAS Add-Only backup: {new_loc.file_name}",
                    "added_locations": [{
                        "id": str(new_loc.id),
                        "file_name": new_loc.file_name,
                        "file_path": new_loc.file_path,
                        "library_name": new_loc.library_name
                    }]
                }, status=status.HTTP_201_CREATED)
            except Exception as ex:
                return Response({"error": f"Calibre library not available, and backup copy to NAS failed: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            
        # Add book using calibredb CLI
        file_path = file_loc.file_path
        if not os.path.exists(file_path):
            return Response({"error": f"Source file does not exist on server: {file_path}"}, status=status.HTTP_400_BAD_REQUEST)
            
        cmd = ["calibredb", "add", "--with-library", lib_path, file_path]
        try:
            # Run calibredb command
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            stdout = result.stdout
            
            # Find added book ID
            match = re.search(r"Added book ids:\s*(\d+)", stdout)
            if not match:
                return Response({"error": f"Failed to parse calibredb output: {stdout}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
            calibre_book_id = int(match.group(1))
            
            # Query Calibre's metadata.db to locate the new file path
            db_file = os.path.join(lib_path, 'metadata.db')
            calibre_conn = sqlite3.connect(db_file)
            calibre_cursor = calibre_conn.cursor()
            calibre_cursor.execute('SELECT path FROM books WHERE id = ?', (calibre_book_id,))
            book_row = calibre_cursor.fetchone()
            if not book_row:
                calibre_conn.close()
                return Response({"error": "Could not find added book in Calibre database"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
                
            book_dir = book_row[0]
            
            calibre_cursor.execute('SELECT format, name FROM data WHERE book = ?', (calibre_book_id,))
            data_rows = calibre_cursor.fetchall()
            calibre_conn.close()
            
            added_locations = []
            for d_row in data_rows:
                fmt = d_row[0].lower()
                name = d_row[1]
                calibre_file_path = os.path.abspath(os.path.join(lib_path, book_dir, f"{name}.{fmt}"))
                
                if os.path.exists(calibre_file_path):
                    # Process and register this new file location
                    meta = extract_metadata(calibre_file_path)
                    
                    # Create FileLocation linked to same CatalogItem
                    new_loc = FileLocation.objects.create(
                        catalog_item=file_loc.catalog_item,
                        file_path=calibre_file_path,
                        file_name=os.path.basename(calibre_file_path),
                        file_size=meta['file_size'],
                        file_format=meta['file_format'],
                        file_created_at=parse_datetime(meta['file_created_at']),
                        file_modified_at=parse_datetime(meta['file_modified_at']),
                        file_hash=meta['file_hash'],
                        is_invalid=meta.get('is_invalid', False),
                        has_drm=meta.get('has_drm', False),
                        library_name=lib_name,
                        library_path=lib_path
                    )
                    added_locations.append({
                        "id": str(new_loc.id),
                        "file_name": new_loc.file_name,
                        "file_path": new_loc.file_path,
                        "library_name": new_loc.library_name
                    })
            
            return Response({
                "status": "success",
                "message": f"Successfully added book {file_loc.file_name} to Calibre Library",
                "added_locations": added_locations
            }, status=status.HTTP_201_CREATED)
            
        except FileNotFoundError:
            return Response({
                "error": "calibredb executable not found on server system path. Please ensure Calibre is installed and added to the path."
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except subprocess.CalledProcessError as e:
            return Response({
                "error": "calibredb command failed",
                "details": e.stderr or e.stdout
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({"error": f"An unexpected error occurred: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class FileCleanupAPIView(APIView):
    """
    API endpoint for remote folder scanners to prune stale database entries.
    Supports two modes:
    1. 'deleted_paths': A list of file paths that were deleted on disk. Directly deletes
       matching FileLocation records and purges empty CatalogItems. (Highly efficient).
    2. 'library_path' and 'active_paths': Fallback mode comparing active paths against
       the database records for the library directory.
    """
    permission_classes = [IsScannerTokenAuthenticated]

    def post(self, request):
        data = clean_val(request.data) if hasattr(request, 'data') else request.data
        deleted_paths = data.get('deleted_paths')
        library_path = data.get('library_path')
        active_paths = data.get('active_paths')

        # Fast path: targeted deletion of known deleted paths
        if deleted_paths is not None:
            if not isinstance(deleted_paths, list):
                return Response({"error": "deleted_paths must be a list of file paths."}, status=status.HTTP_400_BAD_REQUEST)

            # Safety guard: never prune a FileLocation whose physical file still exists on disk
            verified_deleted_paths = []
            for p in deleted_paths:
                if not p or not isinstance(p, str):
                    continue
                root_seg = '/' + p.lstrip('/').split('/', 1)[0] if p.startswith('/') else ''
                if root_seg and os.path.isdir(root_seg) and os.path.exists(p):
                    continue
                verified_deleted_paths.append(p)

            if not verified_deleted_paths:
                return Response({
                    "status": "success",
                    "deleted_locations": 0,
                    "deleted_catalog_items": 0
                }, status=status.HTTP_200_OK)

            # Query locations matching any of the verified deleted paths
            matching_locs = FileLocation.objects.filter(file_path__in=verified_deleted_paths)
            catalog_item_ids = set(matching_locs.values_list('catalog_item_id', flat=True))
            deleted_count, _ = matching_locs.delete()

            deleted_items_count = 0
            if catalog_item_ids:
                orphans = CatalogItem.objects.filter(id__in=catalog_item_ids, files__isnull=True)
                deleted_items_count, _ = orphans.delete()

            return Response({
                "status": "success",
                "deleted_locations": deleted_count,
                "deleted_catalog_items": deleted_items_count
            }, status=status.HTTP_200_OK)

        # Fallback path: active_paths comparison
        if not library_path or active_paths is None:
            return Response(
                {"error": "Missing required fields: deleted_paths OR (library_path and active_paths)."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Standardize paths to use consistent slashes and case
        library_path_std = os.path.abspath(library_path).lower()
        active_paths_set = {os.path.abspath(p).lower() for p in active_paths}

        # Query all file locations
        all_locations = FileLocation.objects.all()
        to_delete_ids = []
        catalog_items_to_check = set()

        for loc in all_locations:
            loc_path_std = os.path.abspath(loc.file_path).lower()
            # Check if this file resides under the library root path
            if loc_path_std == library_path_std or loc_path_std.startswith(library_path_std + os.sep) or (library_path_std.endswith(os.sep) and loc_path_std.startswith(library_path_std)):
                if loc_path_std not in active_paths_set:
                    to_delete_ids.append(loc.id)
                    catalog_items_to_check.add(loc.catalog_item_id)

        deleted_count = 0
        if to_delete_ids:
            deleted_count, _ = FileLocation.objects.filter(id__in=to_delete_ids).delete()

        # Check and delete orphaned catalog items (which have 0 files left)
        deleted_items_count = 0
        if catalog_items_to_check:
            orphans = CatalogItem.objects.filter(id__in=catalog_items_to_check, files__isnull=True)
            deleted_items_count, _ = orphans.delete()

        return Response({
            "status": "success",
            "deleted_locations": deleted_count,
            "deleted_catalog_items": deleted_items_count
        }, status=status.HTTP_200_OK)


class CatalogCacheAPIView(APIView):
    """
    API endpoint (`GET /api/sync/cache/`) for external metadata tools (such as `epubmeta`)
    to pre-seed their local metadata cache from BookDB and skip corrupt, DRM-locked,
    or already-complete ebook files.
    """
    permission_classes = [IsScannerTokenAuthenticated]

    def get(self, request):
        from django.db.models import Q

        invalid_locs = FileLocation.objects.filter(
            Q(is_invalid=True) | Q(has_drm=True)
        ).values_list('file_hash', 'file_path')
        invalid_hashes = [h for h, _ in invalid_locs if h]
        invalid_paths = [p for _, p in invalid_locs if p]

        complete_locs = FileLocation.objects.filter(
            is_invalid=False,
            has_drm=False,
            catalog_item__isbn__isnull=False,
            catalog_item__description__isnull=False,
            catalog_item__publisher__isnull=False,
        ).exclude(
            Q(catalog_item__isbn='')
            | Q(catalog_item__description='')
            | Q(catalog_item__publisher='')
            | Q(catalog_item__authors='Unknown Author')
            | Q(catalog_item__authors='')
        ).values_list('file_path', flat=True)
        complete_hashes = []
        complete_paths = [p for p in complete_locs if p]

        skip_items = request.query_params.get('skip_items') == '1'
        catalog_items = []
        if not skip_items:
            curated_items = CatalogItem.objects.filter(
                Q(isbn__isnull=False) & ~Q(isbn='')
            ).values('title', 'authors', 'isbn', 'publisher', 'pub_date', 'description')

            catalog_items = [
                {
                    'title': item['title'],
                    'authors': item['authors'],
                    'isbn': item['isbn'],
                    'publisher': item['publisher'],
                    'pub_date': str(item['pub_date']) if item['pub_date'] else None,
                    'description': item['description'],
                }
                for item in curated_items
            ]

        return Response({
            "catalog_items": catalog_items,
            "invalid_or_drm_hashes": invalid_hashes,
            "invalid_or_drm_paths": invalid_paths,
            "complete_hashes": complete_hashes,
            "complete_paths": complete_paths,
        }, status=status.HTTP_200_OK)


ACTIVE_KINDLE_DEVICE_ACCOUNT_IDS = {
    "A101329625FBGFO0QECXO",  # JAMES's Kindle for Windows (PC) - oscarman007_8YLYgU@kindle.com
    "A015285311F3W73S7EA31",  # JAMES's 18th Android Device (Tab S11) - oscarman007_RfOPZk@kindle.com
    "A018607231PVOX7INFXHE",  # JAMES's 12th Android Device (Galaxy S25 Ultra) - oscarman007_GNgiXh@kindle.com
    "A0440102192GKG2EE19T3",  # JAMES's 8th Android Device (Tab S8 Ultra) - oscarman007_vlEewv@kindle.com
    "A02063483UBXG3PKOGGOX",  # Jimmy's tablet (Tab A8)
    "A05507311ZARZZO8ORYGN",  # JAMES's Main (Cloud Reader)
}

DEFAULT_KINDLE_DEVICES = [
    {
        "deviceName": "JAMES's Kindle for Windows (Default - PC)",
        "deviceAccountID": "A101329625FBGFO0QECXO",
        "deviceSerialNumber": "C36839180E3E566C07FCEF7DEFD96D32",
        "deviceTypeID": "A1UOUY6O7B48AR",
        "deviceFamily": "KINDLE_APP",
        "deviceTypeString": "PC (Kindle for Windows)",
        "friendlyLabel": "JAMES's Kindle for Windows",
        "isDefault": True,
    },
    {
        "deviceName": "Jimmy's Tab S11",
        "deviceAccountID": "A015285311F3W73S7EA31",
        "deviceSerialNumber": "fbc9082131a2444bb978c0bf861ac3bc",
        "deviceTypeID": "A2Y8LFC259B97P",
        "deviceFamily": "KINDLE_APP",
        "deviceTypeString": "Kindle for Android",
        "friendlyLabel": "Jimmy's Tab S11",
        "isDefault": False,
    },
    {
        "deviceName": "Jimmy's Galaxy S25 Ultra",
        "deviceAccountID": "A018607231PVOX7INFXHE",
        "deviceSerialNumber": "657a28b98877458fbdcae5c9069c0976",
        "deviceTypeID": "A3VNNDO1I14V03",
        "deviceFamily": "KINDLE_APP",
        "deviceTypeString": "Kindle for Android",
        "friendlyLabel": "Jimmy's Galaxy S25 Ultra",
        "isDefault": False,
    },
    {
        "deviceName": "Jimmy's Tab S8 Ultra",
        "deviceAccountID": "A0440102192GKG2EE19T3",
        "deviceSerialNumber": "61bacf27b0444b12825b3bd51a8031f2",
        "deviceTypeID": "A2Y8LFC259B97P",
        "deviceFamily": "KINDLE_APP",
        "deviceTypeString": "Kindle for Android",
        "friendlyLabel": "Jimmy's Tab S8 Ultra",
        "isDefault": False,
    },
    {
        "deviceName": "Jimmy's Tab A8",
        "deviceAccountID": "A02063483UBXG3PKOGGOX",
        "deviceSerialNumber": "e73d7ef3536d46aaae7f8156125ede47",
        "deviceTypeID": "A2Y8LFC259B97P",
        "deviceFamily": "KINDLE_APP",
        "deviceTypeString": "Kindle for Android",
        "friendlyLabel": "Jimmy's Tab A8",
        "isDefault": False,
    },
    {
        "deviceName": "JAMES's Kindle for Web",
        "deviceAccountID": "A05507311ZARZZO8ORYGN",
        "deviceSerialNumber": "A3SF9CPPQ8XQON00A2CTZ977SKFQZY",
        "deviceTypeID": "A2CTZ977SKFQZY",
        "deviceFamily": "KINDLE_APP",
        "deviceTypeString": "Kindle Cloud Reader",
        "friendlyLabel": "JAMES's Kindle for Web",
        "isDefault": False,
    },
]


class KindlePCControlAPIView(APIView):
    """
    API endpoint (`/mylibrary/api/kindle-pc/`) for the Amazon Only (Not on NAS) shelf:
    - GET: Returns registered Kindle devices (defaulting to JAMES's Kindle for Windows),
           all Amazon-Only Kindle books (with ASIN, title, originType), and local Kindle for Windows
           download status or PowerShell one-liner to empty downloaded entries on the Windows PC.
    - POST:
      - `action="empty_local"`: Empties downloaded `<ASIN>_EBOK` folders and clears `book_asset.db`
        when running on the Windows PC (and always returns the ready-to-run PowerShell command &
        `.ps1` download script if MyLibrary is hosted on the NAS).
      - `action="save_devices"`: Persists updated device list fetched from Amazon MYCD.
    """
    authentication_classes = []
    permission_classes = []

    @staticmethod
    def _get_devices_cache_path():
        from pathlib import Path
        base = Path(settings.BASE_DIR)
        sc = base / "scanner"
        return (sc if sc.exists() else base) / "amazon_kindle_devices.json"

    @classmethod
    def _load_devices(cls):
        import json
        p = cls._get_devices_cache_path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except Exception:
                pass
        return DEFAULT_KINDLE_DEVICES

    @staticmethod
    def _map_mycd_origin_type(raw_origin: str) -> str:
        ot = str(raw_origin or "PURCHASE").strip().upper()
        if ot in ("KINDLE_UNLIMITED", "KU"):
            return "Ku"
        if ot in ("PRIME_READING", "PRIME", "KOLL"):
            return "Prime"
        if ot in ("COMIXOLOGY", "COMIXOLOGY_UNLIMITED"):
            return "Comixology"
        if ot == "SAMPLE":
            return "Sample"
        return "Purchase"

    @staticmethod
    def _get_windows_empty_ps_command(close_app: bool = True) -> str:
        kill_part = "Stop-Process -Name Kindle -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 600; " if close_app else ""
        return (
            f"{kill_part}"
            "$c = \"$env:LOCALAPPDATA\\Packages\\AMZNKindle.AmazonKindleReadingApp_m1sc522ngdk36\\LocalState\\Classic\\Content\"; "
            "if (Test-Path $c) { "
            "$dirs = Get-ChildItem -Path $c -Directory; "
            "$cnt = $dirs.Count; "
            "$dirs | Remove-Item -Recurse -Force; "
            "python -c \"import os,sqlite3; db=os.path.expandvars(r'%LOCALAPPDATA%\\Packages\\AMZNKindle.AmazonKindleReadingApp_m1sc522ngdk36\\LocalState\\Classic\\Content\\book_asset.db'); "
            "conn=sqlite3.connect(db) if os.path.exists(db) else None; "
            "[conn.execute(q) for q in ('DELETE FROM AssetDownloadInfo','DELETE FROM Asset','DELETE FROM BookDownloadInfo','DELETE FROM Book')] if conn else None; "
            "conn.commit() if conn else None; conn.execute('VACUUM') if conn else None; conn.close() if conn else None\"; "
            "Write-Host \"Emptied $cnt downloaded Kindle books and cleared book_asset.db!\" -ForegroundColor Green "
            "} else { Write-Host \"Kindle Content folder not found: $c\" -ForegroundColor Yellow }"
        )

    @staticmethod
    def _try_empty_local_windows_kindle(close_app: bool = True) -> dict:
        import shutil
        import sqlite3
        import subprocess
        from pathlib import Path

        local_app_data = os.environ.get("LOCALAPPDATA") or r"C:\Users\oscar\AppData\Local"
        content_dir = Path(local_app_data) / "Packages" / "AMZNKindle.AmazonKindleReadingApp_m1sc522ngdk36" / "LocalState" / "Classic" / "Content"
        if not content_dir.exists():
            alt_dir = Path(r"C:\Users\oscar\AppData\Local\Packages\AMZNKindle.AmazonKindleReadingApp_m1sc522ngdk36\LocalState\Classic\Content")
            if alt_dir.exists():
                content_dir = alt_dir

        if not content_dir.exists():
            return {
                "ran_locally": False,
                "reason": "Server is running inside NAS Docker container (not directly on Windows host).",
            }

        if close_app and os.name == "nt":
            try:
                subprocess.run(["taskkill", "/F", "/IM", "Kindle.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

        removed_folders = 0
        for d in list(content_dir.iterdir()):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                removed_folders += 1

        db_path = content_dir / "book_asset.db"
        rem_books = 0
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            for q in ("DELETE FROM AssetDownloadInfo", "DELETE FROM Asset", "DELETE FROM BookDownloadInfo", "DELETE FROM Book"):
                try:
                    cur.execute(q)
                except Exception:
                    pass
            conn.commit()
            try:
                cur.execute("VACUUM")
                rem_books = cur.execute("SELECT COUNT(*) FROM Book").fetchone()[0]
            except Exception:
                pass
            conn.close()

        return {
            "ran_locally": True,
            "removed_folders": removed_folders,
            "remaining_books_in_db": rem_books,
            "content_dir": str(content_dir),
        }

    def get(self, request):
        from .opds import get_dynamic_amazon_shelf

        if request.GET.get("download_script") == "1":
            from django.http import HttpResponse
            ps_body = (
                "# Empty Downloaded Kindle for Windows Books & Clear book_asset.db\r\n"
                "Write-Host '====================================================================' -ForegroundColor Cyan\r\n"
                "Write-Host ' EMPTYING KINDLE FOR WINDOWS DOWNLOADED BOOKS (Classic\\Content)' -ForegroundColor Cyan\r\n"
                "Write-Host '====================================================================' -ForegroundColor Cyan\r\n"
                + self._get_windows_empty_ps_command(close_app=True)
                + "\r\n"
            )
            resp = HttpResponse(ps_body, content_type="application/octet-stream")
            resp["Content-Disposition"] = 'attachment; filename="empty_kindle_pc.ps1"'
            return resp

        amz = get_dynamic_amazon_shelf()
        missing_kindle = []
        for cid in amz.get("amazon_only_ids") or []:
            rec = (amz.get("by_item_id") or {}).get(str(cid)) or {}
            if rec.get("item_type") == "AUDIBLE":
                continue
            asin = str(rec.get("asin") or "").strip()
            if not asin:
                continue
            missing_kindle.append({
                "id": str(cid),
                "asin": asin,
                "title": rec.get("title") or "Untitled",
                "authors": rec.get("authors") or "",
                "originType": self._map_mycd_origin_type(rec.get("origin_type")),
                "is_kindle_plus": bool(rec.get("is_kindle_plus")),
            })

        devices = self._load_devices()
        return Response({
            "ok": True,
            "devices": devices,
            "default_device_account_id": "A101329625FBGFO0QECXO",
            "missing_kindle_count": len(missing_kindle),
            "missing_kindle": missing_kindle,
            "powershell_empty_cmd": self._get_windows_empty_ps_command(close_app=True),
        })

    def post(self, request):
        import json
        body = clean_val(request.data or {})
        action = str(body.get("action") or "").strip().lower()

        if action == "save_devices":
            raw_devs = body.get("devices") or []
            if isinstance(raw_devs, list) and raw_devs:
                p = self._get_devices_cache_path()
                p.write_text(json.dumps(raw_devs, indent=2, ensure_ascii=False), encoding="utf-8")
                return Response({"ok": True, "saved_count": len(raw_devs), "devices": raw_devs})
            return Response({"ok": False, "error": "No devices provided"}, status=status.HTTP_400_BAD_REQUEST)

        if action == "empty_local":
            close_app = bool(body.get("close_app", True))
            res = self._try_empty_local_windows_kindle(close_app=close_app)
            res["ok"] = True
            res["powershell_empty_cmd"] = self._get_windows_empty_ps_command(close_app=close_app)
            return Response(res)

        if action in ("ingest_tobooks", "process_tobooks"):
            from .opds import (
                _AMAZON_SHELF_CACHE,
                _QUARANTINED_MISSING_SHELF_CACHE,
                _RECENTLY_ADDED_BOOKS_CACHE,
                get_dynamic_amazon_shelf,
                ingest_tobooks_now,
            )
            ing = ingest_tobooks_now(sync_db=True)
            if ing.get("processed_count"):
                _AMAZON_SHELF_CACHE["expires_at"] = 0.0
                _RECENTLY_ADDED_BOOKS_CACHE["expires_at"] = 0.0
                _QUARANTINED_MISSING_SHELF_CACHE["expires_at"] = 0.0
            amz = get_dynamic_amazon_shelf()
            return Response({
                "ok": True,
                "ingest": ing,
                "counts": amz.get("counts") or {},
            })

        return Response({"ok": False, "error": f"Unknown action: {action}"}, status=status.HTTP_400_BAD_REQUEST)



