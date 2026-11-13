"""
Django settings for core project.
"""

import os
import environ
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Initialize environ
env = environ.Env()
# Read .env file
environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env('SECRET_KEY', default='django-insecure-replace-this-with-a-real-secure-key')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env.bool('DEBUG', default=True)

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['*'])
if '*' not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append('*')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',  # Comma-formatted large numbers (intcomma)
    'django.contrib.postgres',  # Enabled for PostgreSQL full-text search features
    
    # Third party apps
    'rest_framework',
    'corsheaders',
    
    # Local apps
    'catalog.apps.CatalogConfig',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',  # Added for CORS headers
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'catalog.security.ExternalAccessSecurityMiddleware',  # Protects web pages, APIs & OPDS outside LAN
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'


# Database
DATABASES = {
    'default': env.db('DATABASE_URL')
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = env('TIME_ZONE', default='America/New_York')
USE_I18N = True
USE_TZ = True

FORCE_SCRIPT_NAME = env('FORCE_SCRIPT_NAME', default=None)

# Static files (CSS, JavaScript, Images)
if FORCE_SCRIPT_NAME:
    STATIC_URL = f"{FORCE_SCRIPT_NAME.rstrip('/')}/static/"
    MEDIA_URL = f"{FORCE_SCRIPT_NAME.rstrip('/')}/media/"
else:
    STATIC_URL = '/static/'
    MEDIA_URL = '/media/'

STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# API Scanner configurations
SCANNER_API_TOKEN = env('SCANNER_API_TOKEN', default='super-secret-sync-token-12345')

# External Web & OPDS Security Configuration
# WEB_AUTH_MODE / OPDS_AUTH_MODE options:
#   - 'external' (default): Zero friction on home LAN (192.168.x.x / Tailscale), requires login/Basic Auth when accessed outside LAN
#   - 'always': Always require login/Basic Auth everywhere (including on LAN)
#   - 'off': Never require login
WEB_AUTH_MODE = env('WEB_AUTH_MODE', default='external')
OPDS_AUTH_MODE = env('OPDS_AUTH_MODE', default='external')
AUTH_USERNAME = env('AUTH_USERNAME', default='oscar')
AUTH_PASSWORD = env('AUTH_PASSWORD', default='')
OPDS_USERNAME = env('OPDS_USERNAME', default='')
OPDS_PASSWORD = env('OPDS_PASSWORD', default='')
OPDS_API_KEY = env('OPDS_API_KEY', default='')
TRUSTED_LAN_CIDRS = env.list(
    'TRUSTED_LAN_CIDRS',
    default=['127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10'],
)

# CORS policy for remote scanner
CORS_ALLOW_ALL_ORIGINS = True

# Maximum request body size (10MB safety margin for bulk scanner updates)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760
