from django.contrib import admin
from .models import CatalogItem, FileLocation

@admin.register(CatalogItem)
class CatalogItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'authors', 'item_type', 'isbn', 'asin', 'created_at')
    list_filter = ('item_type', 'created_at')
    search_fields = ('title', 'authors', 'isbn', 'asin', 'description')
    readonly_fields = ('id', 'created_at', 'updated_at')
    
    fieldsets = (
        ('General Information', {
            'fields': ('id', 'title', 'authors', 'publisher', 'pub_date')
        }),
        ('Identifiers & Type', {
            'fields': ('item_type', 'isbn', 'asin')
        }),
        ('Content & Imagery', {
            'fields': ('description', 'cover_image_url')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )

@admin.register(FileLocation)
class FileLocationAdmin(admin.ModelAdmin):
    list_display = ('file_name', 'file_format', 'file_size', 'scanned_at')
    list_filter = ('file_format', 'scanned_at')
    search_fields = ('file_name', 'file_path', 'file_hash')
    readonly_fields = ('id', 'scanned_at')
