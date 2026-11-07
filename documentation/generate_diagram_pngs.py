import os
import base64
import urllib.request
import urllib.error

def generate_png(mmd_path, png_path):
    if not os.path.exists(mmd_path):
        print(f"Source file not found: {mmd_path}")
        return

    with open(mmd_path, 'r', encoding='utf-8') as f:
        code = f.read()
    
    # Base64 encode the mermaid markup. 
    # mermaid.ink takes standard base64.
    encoded = base64.b64encode(code.encode('utf-8')).decode('ascii')
    url = f"https://mermaid.ink/img/{encoded}"
    
    print(f"Rendering {os.path.basename(mmd_path)} via mermaid.ink API...")
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            with open(png_path, 'wb') as f:
                f.write(response.read())
            print(f"  Successfully wrote {os.path.basename(png_path)}")
    except urllib.error.HTTPError as e:
        print(f"  HTTP Error: {e.code} - {e.reason}")
    except Exception as e:
        print(f"  Error rendering diagram: {e}")

if __name__ == '__main__':
    base_dir = os.path.dirname(os.path.abspath(__file__))
    diag_dir = os.path.join(base_dir, 'diagrams')
    
    # Render architecture and schema
    generate_png(os.path.join(diag_dir, 'architecture.mmd'), os.path.join(diag_dir, 'architecture.png'))
    generate_png(os.path.join(diag_dir, 'database_schema.mmd'), os.path.join(diag_dir, 'database_schema.png'))
