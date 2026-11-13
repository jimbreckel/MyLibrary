"""
Security & Authentication Middleware + OPDS / External Access Protection
========================================================================
Provides unified authentication for both:
  1. Web pages & REST APIs when accessed outside the trusted home LAN (`WEB_AUTH_MODE`), and
  2. OPDS 1.2 / Calibre Content Server emulation (`/mylibrary/opds/...`, `OPDS_AUTH_MODE`).

Supported Authentication Methods:
  - Standard browser session login (`/mylibrary/login/`, `/mylibrary/logout/`)
  - HTTP Basic Authentication (`Authorization: Basic <base64(user:pass)>`) — native to Moon+ Reader,
    KOReader, Calibre Sync, PocketBook, and web browsers
  - Bearer / Header / Query API Token (`Authorization: Bearer <token>`, `X-API-Key: <token>`, `?api_key=<token>`)
  - Django `User` accounts (`django.contrib.auth.authenticate`) OR `.env` credentials (`AUTH_USERNAME` / `AUTH_PASSWORD`)

Brute-force rate limiting:
  - Automatically blocks an IP for 15 minutes after 8 failed login attempts within 10 minutes.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render

# In-memory brute-force tracker: ip -> list of failure timestamps
_FAILED_ATTEMPTS: Dict[str, List[float]] = defaultdict(list)
_MAX_FAILURES = 8
_WINDOW_SECONDS = 600   # 10 minutes
_LOCKOUT_SECONDS = 900  # 15 minutes


def get_client_ip(request: HttpRequest) -> str:
    """
    Determine client IP address. If the direct peer is a trusted local/Docker proxy
    (e.g. DevStack Nginx on 172.x / 192.168.x / 127.0.0.1), inspect X-Forwarded-For / X-Real-IP.
    """
    remote_addr = (request.META.get('REMOTE_ADDR') or '').strip()
    xff = (request.META.get('HTTP_X_FORWARDED_FOR') or '').strip()
    x_real = (request.META.get('HTTP_X_REAL_IP') or '').strip()

    if xff:
        first_hop = xff.split(',')[0].strip()
        if first_hop:
            return first_hop
    if x_real:
        return x_real
    return remote_addr or '127.0.0.1'


def is_lan_or_trusted_ip(ip_str: str) -> bool:
    """
    Return True if `ip_str` is within RFC1918 private LAN space, loopback,
    Docker bridge network, or Tailscale CGNAT (`100.64.0.0/10`), unless overridden
    by `TRUSTED_LAN_CIDRS` in settings.
    """
    if not ip_str:
        return False
    custom_cidrs = getattr(settings, 'TRUSTED_LAN_CIDRS', None)
    default_cidrs = [
        '127.0.0.0/8',
        '::1/128',
        '10.0.0.0/8',
        '172.16.0.0/12',
        '192.168.0.0/16',
        '100.64.0.0/10',  # Tailscale VPN
    ]
    cidr_list = custom_cidrs if custom_cidrs else default_cidrs
    try:
        ip_obj = ipaddress.ip_address(ip_str)
        for cidr in cidr_list:
            try:
                if ip_obj in ipaddress.ip_network(cidr.strip(), strict=False):
                    return True
            except ValueError:
                continue
    except ValueError:
        return False
    return False


def is_request_external(request: HttpRequest) -> bool:
    """
    Determine whether a request originated outside the home LAN:
      1. Client IP is not in `TRUSTED_LAN_CIDRS` (e.g. public internet IP forwarded by reverse proxy), OR
      2. Request `Host` header is an external domain not in local LAN hosts (`localhost`, `127.0.0.1`, `192.168.*`, `nas`), OR
      3. `CF-Connecting-IP` header is present (Cloudflare Tunnel / proxy).
    """
    if request.META.get('HTTP_CF_CONNECTING_IP'):
        return True

    client_ip = get_client_ip(request)
    if not is_lan_or_trusted_ip(client_ip):
        return True

    host = (request.get_host() or '').split(':')[0].strip().lower()
    if host and host not in ('localhost', '127.0.0.1', 'mylibrary-web', 'mylibrary_web', 'nas', 'nas.local'):
        try:
            ip_obj = ipaddress.ip_address(host)
            if not is_lan_or_trusted_ip(str(ip_obj)):
                return True
        except ValueError:
            # Hostname is a custom domain name (e.g., library.example.com)
            return True

    return False


def _is_ip_locked_out(ip_str: str) -> bool:
    now = time.time()
    recent = [ts for ts in _FAILED_ATTEMPTS.get(ip_str, []) if now - ts < _LOCKOUT_SECONDS]
    _FAILED_ATTEMPTS[ip_str] = recent
    return len(recent) >= _MAX_FAILEMPTS_THRESHOLD()


def _MAX_FAILEMPTS_THRESHOLD() -> int:
    return int(getattr(settings, 'AUTH_MAX_FAILURES', _MAX_FAILURES))


def _record_failed_attempt(ip_str: str) -> None:
    now = time.time()
    recent = [ts for ts in _FAILED_ATTEMPTS.get(ip_str, []) if now - ts < _WINDOW_SECONDS]
    recent.append(now)
    _FAILED_ATTEMPTS[ip_str] = recent


def _clear_failed_attempts(ip_str: str) -> None:
    _FAILED_ATTEMPTS.pop(ip_str, None)


def verify_credentials(request: HttpRequest, username: str, password: str) -> bool:
    """
    Verify `username` and `password` against:
      1. `.env` / settings `AUTH_USERNAME` + `AUTH_PASSWORD` (and `OPDS_USERNAME` + `OPDS_PASSWORD`),
      2. Django's `authenticate(request, username=..., password=...)` user table.
    """
    if not username or not password:
        return False

    cfg_user = str(getattr(settings, 'AUTH_USERNAME', 'oscar') or 'oscar').strip()
    cfg_pass = str(getattr(settings, 'AUTH_PASSWORD', '') or '').strip()
    if cfg_user and cfg_pass:
        if hmac.compare_digest(username.strip(), cfg_user) and hmac.compare_digest(password, cfg_pass):
            return True

    opds_user = str(getattr(settings, 'OPDS_USERNAME', '') or '').strip()
    opds_pass = str(getattr(settings, 'OPDS_PASSWORD', '') or '').strip()
    if opds_user and opds_pass:
        if hmac.compare_digest(username.strip(), opds_user) and hmac.compare_digest(password, opds_pass):
            return True

    user = authenticate(request, username=username, password=password)
    if user is not None and user.is_active:
        return True

    return False


def verify_api_token(token: str) -> bool:
    if not token:
        return False
    valid_tokens = [
        str(getattr(settings, 'SCANNER_API_TOKEN', '') or '').strip(),
        str(getattr(settings, 'AUTH_PASSWORD', '') or '').strip(),
        str(getattr(settings, 'OPDS_API_KEY', '') or '').strip(),
    ]
    for vt in valid_tokens:
        if vt and hmac.compare_digest(token.strip(), vt):
            return True
    return False


def check_request_authenticated(request: HttpRequest) -> Tuple[bool, Optional[str]]:
    """
    Check if the current request carries valid authentication via:
      1. Active session (`request.session.get('mylibrary_authenticated')` or `request.user.is_authenticated`)
      2. API token (`Authorization: Bearer <token>`, `X-API-Key`, or `?api_key=`)
      3. HTTP Basic Auth (`Authorization: Basic <base64>`)
    Returns `(is_authenticated, error_reason)`.
    """
    client_ip = get_client_ip(request)
    if _is_ip_locked_out(client_ip):
        return False, 'Too many failed login attempts. Try again in 15 minutes.'

    # 1. Django user or session flag
    if getattr(request, 'user', None) and request.user.is_authenticated:
        return True, None
    if hasattr(request, 'session') and request.session.get('mylibrary_authenticated') is True:
        return True, None

    # 2. Bearer / API Key header or query param
    auth_header = (request.META.get('HTTP_AUTHORIZATION') or '').strip()
    api_key = (
        request.META.get('HTTP_X_API_KEY')
        or request.GET.get('api_key')
        or ''
    ).strip()
    if auth_header.lower().startswith('bearer '):
        api_key = auth_header[7:].strip()
    if api_key:
        if verify_api_token(api_key):
            _clear_failed_attempts(client_ip)
            return True, None
        _record_failed_attempt(client_ip)
        return False, 'Invalid API key.'

    # 3. HTTP Basic Auth
    if auth_header.lower().startswith('basic '):
        b64_part = auth_header[6:].strip()
        try:
            decoded = base64.b64decode(b64_part).decode('utf-8', errors='replace')
            if ':' in decoded:
                u, p = decoded.split(':', 1)
                if verify_credentials(request, u, p):
                    _clear_failed_attempts(client_ip)
                    if hasattr(request, 'session'):
                        request.session['mylibrary_authenticated'] = True
                        request.session['mylibrary_user'] = u
                    return True, None
        except Exception:
            pass
        _record_failed_attempt(client_ip)
        return False, 'Invalid username or password.'

    return False, None


class ExternalAccessSecurityMiddleware:
    """
    Protects MyLibrary web pages, REST APIs, and OPDS feeds based on configurable modes:
      - `WEB_AUTH_MODE`:
          - `'external'` (default): Zero friction on home LAN (`192.168.x.x`), requires login/auth when accessed from outside LAN or via public domain.
          - `'always'`: Always require login/auth everywhere (even on LAN).
          - `'off'`: Disable web page auth guard.
      - `OPDS_AUTH_MODE`:
          - `'external'` (default): Requires HTTP Basic Auth or API key when accessed externally; zero friction on LAN unless credentials are sent.
          - `'always'`: Always require HTTP Basic Auth on `/mylibrary/opds/` (even on LAN).
          - `'off'`: Never require auth on `/mylibrary/opds/`.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path_info or request.path or ''

        # Always allow static assets and login/logout endpoints
        if (
            path.startswith('/static/')
            or path.startswith('/mylibrary/static/')
            or path.endswith('/login/')
            or path.endswith('/logout/')
        ):
            return self.get_response(request)

        # Internal scanner sync endpoints already enforce Bearer SCANNER_API_TOKEN
        if '/api/sync/' in path:
            try:
                return self.get_response(request)
            finally:
                try:
                    from django.db import reset_queries
                    reset_queries()
                except Exception:
                    pass

        is_opds = '/opds' in path
        external = is_request_external(request)

        if is_opds:
            mode = str(getattr(settings, 'OPDS_AUTH_MODE', 'external') or 'external').strip().lower()
        else:
            mode = str(getattr(settings, 'WEB_AUTH_MODE', 'external') or 'external').strip().lower()

        require_auth = (mode == 'always') or (mode == 'external' and external)

        # Also validate credentials if the client proactively sent Authorization header
        has_auth_header = bool(request.META.get('HTTP_AUTHORIZATION') or request.META.get('HTTP_X_API_KEY') or request.GET.get('api_key'))

        if require_auth or has_auth_header:
            ok, err_msg = check_request_authenticated(request)
            if not ok and require_auth:
                if is_opds or '/api/' in path:
                    resp = HttpResponse(
                        err_msg or 'Authentication required to access MyLibrary OPDS / API.',
                        status=401,
                        content_type='text/plain; charset=utf-8',
                    )
                    resp['WWW-Authenticate'] = 'Basic realm="MyLibrary OPDS & Catalog"'
                    return resp

                # Web browser request -> redirect to sleek login page
                script_prefix = getattr(settings, 'FORCE_SCRIPT_NAME', '') or ''
                login_url = f"{script_prefix.rstrip('/')}/login/?next={request.get_full_path()}"
                return HttpResponseRedirect(login_url)

        try:
            return self.get_response(request)
        finally:
            try:
                from django.db import reset_queries
                reset_queries()
            except Exception:
                pass

