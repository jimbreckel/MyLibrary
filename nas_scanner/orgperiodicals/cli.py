import os
import sys
from pathlib import Path
import click

try:
    import dotenv
    dotenv.load_dotenv()
except ImportError:
    pass

from .crawler import find_files
from .parser import parse_filename, extract_pdf_metadata, extract_epub_metadata
from .cover import extract_cover
from .gemini import analyze_cover
from .organizer import organize_file, identify_and_organize_periodical
from .classifier import PeriodicalClassifier

# Patch click.echo to handle encoding errors on Windows console
_click_echo_orig = click.echo
def safe_echo(message="", **kwargs):
    msg_str = str(message)
    encoding = sys.stdout.encoding or 'utf-8'
    try:
        _click_echo_orig(msg_str, **kwargs)
    except UnicodeEncodeError:
        safe_msg = msg_str.encode(encoding, errors='replace').decode(encoding)
        _click_echo_orig(safe_msg, **kwargs)
click.echo = safe_echo

def clean_empty_dirs(path: Path, output_dir: Path):
    """Recursively remove empty directories under path, excluding path itself and output_dir."""
    for root, dirs, files in os.walk(path, topdown=False):
        for d in dirs:
            dir_path = Path(root) / d
            # Avoid deleting output_dir, any of its parent folders, or any folder inside output_dir
            if dir_path == output_dir or dir_path in output_dir.parents or output_dir in dir_path.parents:
                continue
            try:
                if not any(dir_path.iterdir()):
                    dir_path.rmdir()
                    click.echo(f"Removed empty directory: {dir_path.relative_to(path)}")
            except Exception:
                pass

@click.command()
@click.argument('source_path', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path), default=Path('.'))
@click.option('-o', '--output-dir', type=click.Path(file_okay=False, dir_okay=True, path_type=Path), help="Destination directory for organized periodicals. Defaults to source path.")
@click.option('-l', '--levels', default=2, show_default=True, help="Number of levels to traverse for searching files.")
@click.option('-c', '--copy', is_flag=True, help="Copy files instead of moving them.")
@click.option('-d', '--dry-run', is_flag=True, help="Preview all changes without executing them.")
@click.option('--api-key', help="Google Gemini API key (can also use GEMINI_API_KEY environment variable).")
@click.option('--force-cover', is_flag=True, help="Force cover scan via Gemini API for all files, bypassing filename/metadata checks.")
def main(source_path: Path, output_dir: Path | None, levels: int, copy: bool, dry_run: bool, api_key: str | None, force_cover: bool):
    """
    Organize PDF, EPUB, CBR, CBZ, WEBP, MOBI, DJVU, and APK periodicals (magazines) into a structured folder hierarchy:
    [destination]/[magazine_name]/[year]/[filename]
    """
    # 1. Resolve paths
    source_path = source_path.resolve()
    if output_dir is None:
        output_dir = source_path
    else:
        output_dir = output_dir.resolve()
        
    # 2. Get API key
    gemini_key = api_key or os.environ.get('GEMINI_API_KEY')
    
    # Initialize PeriodicalClassifier
    classifier = PeriodicalClassifier()
    
    click.echo(f"Scanning directory: {source_path}")
    click.echo(f"Output directory: {output_dir}")
    click.echo(f"Search depth: {levels} levels")
    if copy:
        click.echo("Mode: Copy files")
    else:
        click.echo("Mode: Move files")
    if dry_run:
        click.echo("=== DRY RUN MODE: No files will be moved/copied ===")
        
    click.echo("-" * 50)
    
    # 3. Find files
    files = list(find_files(source_path, levels))
    if not files:
        click.echo("No PDF, EPUB, CBR, CBZ, WEBP, MOBI, DJVU, or APK files found.")
        return
        
    click.echo(f"Found {len(files)} files to process.")
    
    success_count = 0
    fail_count = 0
    unorganized_count = 0
    
    for file_path in files:
        # Don't organize files that are already inside the output_dir's target structure
        try:
            rel = file_path.relative_to(output_dir)
            if len(rel.parts) >= 3 and rel.parts[0] != "Unorganized":
                # Already organized under Title/Year
                continue
        except ValueError:
            pass
            
        click.echo(f"\nProcessing: {file_path.name}")
        
        title, year = None, None
        method = ""
        
        # 1. Match against known patterns in magazines.json
        matched = classifier.match_filename(file_path.name)
        if matched and not force_cover:
            bucket_name, matched_pattern, pub_year_avail = matched
            title = matched_pattern
            method = "magazines.json pattern match"
            
            # If year is expected to be available, try to extract it
            if pub_year_avail:
                _, extracted_year = parse_filename(file_path.name)
                if not extracted_year:
                    ext = file_path.suffix.lower()
                    if ext == '.pdf':
                        _, extracted_year = extract_pdf_metadata(file_path)
                    elif ext == '.epub':
                        _, extracted_year = extract_epub_metadata(file_path)
                
                if not extracted_year:
                    cover_bytes = extract_cover(file_path)
                    if cover_bytes and gemini_key:
                        click.echo("  -> Year not in filename. Extracted cover page. Querying Gemini API...")
                        _, c_year = analyze_cover(cover_bytes, gemini_key)
                        extracted_year = c_year
                
                year = extracted_year
            else:
                year = None
        else:
            # 2. No pattern matched, run the extraction pipeline to determine title and year
            detected_title, detected_year = None, None
            
            if not force_cover:
                detected_title, detected_year = parse_filename(file_path.name)
                if detected_title and detected_year:
                    method = "Filename parsing"
                    
            if not (detected_title and detected_year) and not force_cover:
                ext = file_path.suffix.lower()
                if ext == '.pdf':
                    detected_title, detected_year = extract_pdf_metadata(file_path)
                elif ext == '.epub':
                    detected_title, detected_year = extract_epub_metadata(file_path)
                if detected_title and detected_year:
                    method = "Internal metadata parsing"
                    
            if not (detected_title and detected_year) or force_cover:
                cover_bytes = extract_cover(file_path)
                if cover_bytes:
                    if gemini_key:
                        click.echo("  -> Extracted cover page. Querying Gemini API...")
                        c_title, c_year = analyze_cover(cover_bytes, gemini_key)
                        detected_title = c_title or detected_title
                        detected_year = c_year or detected_year
                        method = "Gemini cover analysis"
                    else:
                        if not (detected_title and detected_year):
                            click.echo("  -> Cover page extracted, but GEMINI_API_KEY is not provided. Skipping cover analysis.")
            
            # Determine the fallback title if parsing completely failed
            if not detected_title:
                parent = file_path.parent.name
                if parent and not (len(parent) == 3 and parent.isupper()) and parent.lower() != 'tomags':
                    detected_title = parent
                else:
                    detected_title = file_path.stem
            
            # 3. Classify this new title, dynamically update magazines.json
            bucket_name = classifier.classify_new_title(detected_title, file_path, detected_year is not None)
            click.echo(f"  -> Found new periodical '{detected_title}'. Classified into '{bucket_name}' and updated magazines.json.")
            
            title = detected_title
            year = detected_year
            if not method:
                method = "Dynamic classification fallback"
                
        # Report results
        if title or year:
            click.echo(f"  Result: Title = '{title or 'Unknown'}', Year = '{year or 'Unknown'}' (via {method})")
        else:
            click.echo("  Result: Could not determine title or year.")
            unorganized_count += 1
            
        # Organize file
        target_path, status_msg = organize_file(file_path, output_dir, title, year, copy_mode=copy, dry_run=dry_run)
        click.echo(f"  {status_msg}")
        
        if "SUCCESS" in status_msg or "DRY-RUN" in status_msg:
            success_count += 1
        else:
            fail_count += 1
            
    # Clean up empty directories in source_path if not in copy mode and not dry-run
    if not copy and not dry_run:
        clean_empty_dirs(source_path, output_dir)

    click.echo("\n" + "=" * 50)
    click.echo("Summary:")
    click.echo(f"  Total processed: {len(files)}")
    click.echo(f"  Successfully organized: {success_count}")
    click.echo(f"  Unorganized: {unorganized_count}")
    click.echo(f"  Failed: {fail_count}")
    click.echo("=" * 50)

if __name__ == '__main__':
    main()
