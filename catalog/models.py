import uuid
from django.db import models

class CatalogItem(models.Model):
    ITEM_TYPES = [
        ('BOOK', 'Book'),
        ('MAGAZINE', 'Magazine'),
        ('KINDLE', 'Kindle Title'),
        ('AUDIBLE', 'Audible Title'),
        ('AUDIOBOOK', 'Audiobook'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=512, db_index=True)
    authors = models.CharField(max_length=512, default='Unknown Author', db_index=True)
    publisher = models.CharField(max_length=256, blank=True, null=True)
    pub_date = models.DateField(blank=True, null=True, db_index=True)
    
    # Standard identifiers
    isbn = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    asin = models.CharField(max_length=32, blank=True, null=True, db_index=True)
    
    item_type = models.CharField(max_length=16, choices=ITEM_TYPES, default='BOOK', db_index=True)
    description = models.TextField(blank=True, null=True)
    cover_image_url = models.URLField(max_length=1024, blank=True, null=True)
    cover_thumbnail = models.TextField(blank=True, null=True)  # Base64 encoded thumbnail
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def file_date(self):
        """Return the most recent file modification/creation date across attached files, or created_at."""
        try:
            dates = [
                f.file_modified_at or f.file_created_at
                for f in self.files.all()
                if (f.file_modified_at or f.file_created_at)
            ]
            if dates:
                return max(dates)
        except Exception:
            pass
        return self.created_at

    @property
    def author_list(self):
        """Return individual author names for clickable author links while preserving 'Last, First' names."""
        raw = (self.authors or '').strip()
        if not raw:
            return ['Unknown Author']
        result = []
        for semi_part in raw.split(';'):
            semi_part = semi_part.strip()
            if not semi_part:
                continue
            comma_parts = [p.strip() for p in semi_part.split(',') if p.strip()]
            if len(comma_parts) > 1 and all(' ' in p for p in comma_parts):
                result.extend(comma_parts)
            else:
                result.append(semi_part)
        return result or [raw]

    def files_json(self):
        import json
        try:
            from .opds import (
                _ENRICHED_PATHS_CACHE,
                get_file_enrich_diagnostic,
                get_file_tags,
                is_file_enriched,
            )
            excl_set = _ENRICHED_PATHS_CACHE.get('excluded_from_needs_enriching_ids_set')
            item_in_processed = (not isinstance(excl_set, set)) or (not excl_set) or (self.id in excl_set)
        except Exception:
            is_file_enriched = lambda p: False
            get_file_tags = lambda p: []
            get_file_enrich_diagnostic = lambda p: None
            item_in_processed = False
        out = []
        for f in self.files.all():
            enr = bool(is_file_enriched(f.file_path)) if item_in_processed else False
            diag = None if (enr or not item_in_processed) else get_file_enrich_diagnostic(f.file_path)
            out.append({
                'id': str(f.id),
                'file_name': f.file_name,
                'file_path': f.file_path,
                'file_size': f.file_size,
                'file_format': f.file_format,
                'file_hash': f.file_hash,
                'file_date': (f.file_modified_at or f.file_created_at).strftime('%Y-%m-%d') if (f.file_modified_at or f.file_created_at) else '',
                'is_invalid': f.is_invalid,
                'has_drm': f.has_drm,
                'library_name': f.library_name,
                'library_path': f.library_path,
                'is_enriched': enr,
                'enrich_status': (diag or {}).get('status', ''),
                'enrich_error': (diag or {}).get('error', ''),
                'tags': get_file_tags(f.file_path) if enr else [],
            })
        return json.dumps(out)

    def __str__(self):
        return f"{self.title} by {self.authors} ({self.get_item_type_display()})"

    class Meta:
        ordering = ['title']
        verbose_name = 'Catalog Item'
        verbose_name_plural = 'Catalog Items'


class FileLocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    catalog_item = models.ForeignKey(
        CatalogItem,
        on_delete=models.CASCADE,
        related_name='files',
        db_index=True
    )
    
    file_path = models.TextField()  # Paths can be very long (especially on networks)
    file_name = models.CharField(max_length=512)
    file_size = models.BigIntegerField()  # Bytes
    file_format = models.CharField(max_length=16, db_index=True) # EPUB, PDF, MOBI, etc.
    
    file_created_at = models.DateTimeField(blank=True, null=True)
    file_modified_at = models.DateTimeField(blank=True, null=True)
    file_hash = models.CharField(max_length=64, db_index=True)  # SHA-256 hash
    is_invalid = models.BooleanField(default=False, db_index=True)
    has_drm = models.BooleanField(default=False, db_index=True)
    library_name = models.CharField(max_length=256, blank=True, null=True, db_index=True)
    library_path = models.TextField(blank=True, null=True)
    scanned_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.file_name} ({self.file_format})"

    class Meta:
        ordering = ['file_name']
        verbose_name = 'File Location'
        verbose_name_plural = 'File Locations'
