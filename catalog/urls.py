from django.urls import path
from . import views
from . import api
from . import opds

app_name = 'catalog'

urlpatterns = [
    # Web views
    path('', views.DashboardView.as_view(), name='dashboard'),
    path('status/', views.StatusDashboardView.as_view(), name='status_dashboard'),
    path('duplicates/', views.DuplicatesDashboardView.as_view(), name='duplicates_dashboard'),
    path('login/', views.WebLoginView.as_view(), name='login'),
    path('logout/', views.WebLogoutView.as_view(), name='logout'),
    
    # Sync API endpoints
    path('api/duplicates/', views.DuplicatesAPIView.as_view(), name='api_duplicates'),
    path('api/sync/file/', api.FileSyncAPIView.as_view(), name='api_sync_file'),
    path('api/sync/cache/', api.CatalogCacheAPIView.as_view(), name='api_sync_cache'),
    path('api/sync/media/', api.MediaSyncAPIView.as_view(), name='api_sync_media'),
    path('api/sync/amazon/', api.AmazonBatchSyncAPIView.as_view(), name='api_sync_amazon'),
    path('api/sync/cleanup/', api.FileCleanupAPIView.as_view(), name='api_sync_cleanup'),
    path('api/calibre/add/', api.CalibreAddAPIView.as_view(), name='api_calibre_add'),
    path('api/enrich/', views.OnDemandEnrichAPIView.as_view(), name='api_enrich_on_demand'),
    path('api/revert-enrich/', views.RevertEnrichAPIView.as_view(), name='api_revert_enrich'),
    path('api/move-to-mag/', views.MoveToMagAPIView.as_view(), name='api_move_to_mag'),
    path('api/cover-search/', views.CoverSearchAPIView.as_view(), name='api_cover_search'),
    path('api/cover-apply/', views.CoverApplyAPIView.as_view(), name='api_cover_apply'),
    path('api/interesting/', views.ToggleInterestingAPIView.as_view(), name='api_toggle_interesting'),
    path('api/bad-metadata/', views.ToggleBadMetadataAPIView.as_view(), name='api_toggle_bad_metadata'),
    path('api/book-preview/', views.BookPreviewAPIView.as_view(), name='api_book_preview'),
    path('api/book-preview/raw/', views.BookRawStreamView.as_view(), name='api_book_preview_raw'),
    
    # UI Autocomplete suggestions & Status Dashboard live API
    path('api/suggest/', views.SuggestAPIView.as_view(), name='api_suggest'),
    path('api/status/', views.StatusDashboardAPIView.as_view(), name='api_status_dashboard'),
    path('api/status/items/', views.ConversionItemsAPIView.as_view(), name='api_status_items'),
    path('api/status/history/', views.ConversionHistoryAPIView.as_view(), name='api_status_history'),
    path('api/logs/', views.ScannerLogsAPIView.as_view(), name='api_scanner_logs'),

    # Programmatic Catalog Search & Item Detail API (used by CLI, MCP, and external integrations)
    path('api/catalog/', views.CatalogSearchAPIView.as_view(), name='api_catalog_search'),
    path('api/catalog/<str:identifier>/', views.CatalogItemDetailAPIView.as_view(), name='api_catalog_detail'),
    path('api/calibre/check-enriched/', views.CalibreCheckEnrichedAPIView.as_view(), name='api_calibre_check_enriched'),

    # OPDS 1.2 & Calibre Content Server Emulation (Moon+ Reader, KOReader, Calibre Sync, PocketBook)
    path('opds', opds.OPDSRootView.as_view(), name='opds_root_no_slash'),
    path('opds/', opds.OPDSRootView.as_view(), name='opds_root'),
    path('opds/opensearch.xml', opds.OPDSOpenSearchView.as_view(), name='opds_opensearch'),
    path('opds/search/', opds.OPDSSearchView.as_view(), name='opds_search'),
    path('opds/interesting/', opds.OPDSInterestingFeedView.as_view(), name='opds_interesting'),
    path('opds/bad-metadata/', opds.OPDSBadMetadataFeedView.as_view(), name='opds_bad_metadata'),
    path('opds/flag-bad-meta/<str:item_id>/', opds.OPDSFlagBadMetadataView.as_view(), name='opds_flag_bad_meta'),
    path('opds/bookmarked/', opds.OPDSBookmarkedFeedView.as_view(), name='opds_bookmarked'),
    path('opds/amazon/', opds.OPDSAmazonFeedView.as_view(), name='opds_amazon'),
    path('opds/tags/', opds.OPDSTagsView.as_view(), name='opds_tags'),
    path('opds/enriched/', opds.OPDSEnrichedFeedView.as_view(), name='opds_enriched'),
    path('opds/recent/', opds.OPDSRecentFeedView.as_view(), name='opds_recent'),
    path('opds/all/', opds.OPDSAllFeedView.as_view(), name='opds_all'),
    path('opds/authors/', opds.OPDSAuthorsIndexView.as_view(), name='opds_authors'),
    path('opds/formats/', opds.OPDSFormatsView.as_view(), name='opds_formats'),
    path('opds/cover/<str:item_id>.jpg', opds.OPDSCoverView.as_view(), name='opds_cover'),
    path('opds/download/<str:file_id>/', opds.OPDSDownloadFileView.as_view(), name='opds_download_short'),
    path('opds/download/<str:file_id>/<path:filename>', opds.OPDSDownloadFileView.as_view(), name='opds_download'),
    path('opds/get/<str:fmt>/<str:file_id>', opds.OPDSDownloadFileView.as_view(), name='calibre_get'),
    path('opds/ajax/books', opds.CalibreAjaxBooksView.as_view(), name='calibre_ajax_books'),
    path('opds/ajax/search', opds.CalibreAjaxBooksView.as_view(), name='calibre_ajax_search'),
    path('opds/ajax/book/<str:item_id>', opds.CalibreAjaxBooksView.as_view(), name='calibre_ajax_book_detail'),
]
