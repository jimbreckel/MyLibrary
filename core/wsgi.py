"""
WSGI config for core project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os
import time
import traceback
from pathlib import Path

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

try:
    application = get_wsgi_application()
    for _d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad')):
        try:
            if _d.exists():
                (_d / '.wsgi_boot.log').write_text(f"OK pid={os.getpid()} at {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding='utf-8')
                break
        except Exception:
            pass
except BaseException:
    for _d in (Path('/books_bad'), Path('/volume1/common/Media/Books_bad')):
        try:
            if _d.exists():
                (_d / '.wsgi_boot_error.log').write_text(traceback.format_exc(), encoding='utf-8')
                break
        except Exception:
            pass
    raise
