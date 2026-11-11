#!/usr/bin/env python3
"""Root-level launcher for scanner/sync_amazon.py."""
import os
import sys

if __name__ == "__main__":
    root_dir = os.path.dirname(os.path.abspath(__file__))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    from scanner.sync_amazon import main
    sys.exit(main())

