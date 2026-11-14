import os
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
import pymupdf

def extract_pdf_cover(pdf_path: Path) -> Optional[bytes]:
    """
    Renders the first page of the PDF to a PNG image byte array.
    """
    try:
        doc = pymupdf.open(pdf_path)
        if len(doc) > 0:
            page = doc[0]
            # Use 150 DPI (approx 2x zoom) for high enough resolution for OCR / Gemini
            zoom = 150 / 72
            mat = pymupdf.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img_bytes = pix.tobytes("png")
            doc.close()
            return img_bytes
        doc.close()
    except Exception as e:
        # Silently fail or log for CLI user
        pass
    return None

def extract_epub_cover(epub_path: Path) -> Optional[bytes]:
    """
    Extracts the cover image from an EPUB file as a byte array.
    """
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            # 1. Read container.xml to locate OPF file
            container_content = z.read('META-INF/container.xml')
            root = ET.fromstring(container_content)
            
            opf_path = None
            for elem in root.iter():
                if elem.tag.endswith('rootfile'):
                    opf_path = elem.attrib.get('full-path')
                    break
            
            if not opf_path:
                return None
                
            # 2. Read OPF file
            opf_content = z.read(opf_path)
            opf_root = ET.fromstring(opf_content)
            
            # Set up namespaces
            ns = {
                'opf': 'http://www.idpf.org/2007/opf',
                'dc': 'http://purl.org/dc/elements/1.1/'
            }
            
            # We want to search for cover references
            cover_id = None
            
            # Search metadata for <meta name="cover" content="id"/>
            # We look both with namespaces and without
            for meta in opf_root.findall('.//meta'):
                if meta.attrib.get('name') == 'cover':
                    cover_id = meta.attrib.get('content')
                    break
            
            # Find manifest items
            manifest_items = {}
            cover_item_href = None
            
            for item in opf_root.findall('.//item'):
                item_id = item.attrib.get('id')
                href = item.attrib.get('href')
                properties = item.attrib.get('properties', '')
                media_type = item.attrib.get('media-type', '')
                
                if item_id and href:
                    manifest_items[item_id] = (href, media_type)
                    
                # EPUB 3 standard: properties="cover-image"
                if 'cover-image' in properties.split() and href:
                    cover_item_href = href
                    break
                    
                # Direct check for cover ID
                if item_id in ('cover', 'cover-image', 'cover-img') and href:
                    cover_item_href = href
            
            # If we found cover_id from metadata, look it up in manifest
            if not cover_item_href and cover_id and cover_id in manifest_items:
                cover_item_href = manifest_items[cover_id][0]
                
            # Fallback: scan manifest items for anything that looks like a cover image
            if not cover_item_href:
                for item_id, (href, media_type) in manifest_items.items():
                    if 'image' in media_type:
                        lower_href = href.lower()
                        if 'cover' in lower_href or 'folder' in lower_href:
                            cover_item_href = href
                            break
                            
            # If still not found, take the first image as a last resort
            if not cover_item_href:
                for item_id, (href, media_type) in manifest_items.items():
                    if 'image' in media_type:
                        cover_item_href = href
                        break
                        
            if cover_item_href:
                # Resolve full path inside ZIP
                opf_dir = os.path.dirname(opf_path)
                # Normalize path
                if opf_dir:
                    full_img_path = os.path.normpath(os.path.join(opf_dir, cover_item_href)).replace('\\', '/')
                else:
                    full_img_path = cover_item_href
                
                return z.read(full_img_path)
                
    except Exception:
        pass
    return None

def extract_comic_cover(comic_path: Path) -> Optional[bytes]:
    """
    Extracts the first image page of a CBR/CBZ file as a PNG byte array.
    """
    try:
        doc = pymupdf.open(comic_path)
        if len(doc) > 0:
            page = doc[0]
            # Comic book pages are already images, so we can get the pixmap directly
            pix = page.get_pixmap()
            img_bytes = pix.tobytes("png")
            doc.close()
            return img_bytes
        doc.close()
    except Exception:
        pass
    return None

def extract_mobi_cover(mobi_path: Path) -> Optional[bytes]:
    """
    Renders the first page of the MOBI file to a PNG image byte array.
    """
    return extract_pdf_cover(mobi_path)

def extract_djvu_cover(djvu_path: Path) -> Optional[bytes]:
    """
    Renders the first page of the DjVu file to a PNG image byte array.
    """
    return extract_pdf_cover(djvu_path)

def extract_apk_cover(apk_path: Path) -> Optional[bytes]:
    """
    Extracts the app icon from an APK file as a byte array to use as its 'cover'.
    """
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            # Search for common icon file locations
            common_paths = [
                'res/drawable-xxxhdpi/ic_launcher.png',
                'res/drawable-xxhdpi/ic_launcher.png',
                'res/drawable-xhdpi/ic_launcher.png',
                'res/drawable-hdpi/ic_launcher.png',
                'res/drawable/ic_launcher.png',
                'res/mipmap-xxxhdpi/ic_launcher.png',
                'res/mipmap-xxhdpi/ic_launcher.png',
                'res/mipmap-xhdpi/ic_launcher.png',
                'res/mipmap-hdpi/ic_launcher.png',
                'res/mipmap/ic_launcher.png',
            ]
            for path in common_paths:
                if path in z.namelist():
                    return z.read(path)
            
            # Heuristic search for icon files
            for name in z.namelist():
                if name.endswith('.png') and ('icon' in name.lower() or 'launcher' in name.lower()):
                    return z.read(name)
    except Exception:
        pass
    return None

def extract_cover(file_path: Path) -> Optional[bytes]:
    """
    Convenience function to extract cover from PDF, EPUB, CBR, CBZ, WEBP, MOBI, DJVU, or APK.
    """
    ext = file_path.suffix.lower()
    if ext == '.pdf':
        return extract_pdf_cover(file_path)
    elif ext == '.epub':
        return extract_epub_cover(file_path)
    elif ext in ('.cbr', '.cbz'):
        return extract_comic_cover(file_path)
    elif ext == '.webp':
        try:
            return file_path.read_bytes()
        except Exception:
            pass
    elif ext == '.mobi':
        return extract_mobi_cover(file_path)
    elif ext == '.djvu':
        return extract_djvu_cover(file_path)
    elif ext == '.apk':
        return extract_apk_cover(file_path)
    return None
