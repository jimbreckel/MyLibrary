"""
OrgBooks: Automatic Book Drop-Folder Organizer for `/toBooks`.
Reads embedded EPUB/PDF metadata (Title, Authors, Subjects/Tags, Publisher, Synopsis, ISBN)
and filename patterns to automatically route incoming books from `/toBooks` into the
appropriate `/books/<Category>/<3-Char-Shard>/` directory and index them into `BookDB`.
"""
from .organizer import (
    classify_book_target_folder,
    extract_book_metadata_for_routing,
    organize_book_file,
    process_tobooks_directory,
)

__all__ = [
    'classify_book_target_folder',
    'extract_book_metadata_for_routing',
    'organize_book_file',
    'process_tobooks_directory',
]
