from django.db import migrations


SQL_FORWARD = """
CREATE OR REPLACE FUNCTION catalog_top_folder(fp text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN fp LIKE '/books/%' THEN
            CASE
                WHEN split_part(substr(fp, 8), '/', 1) IN ('', 'Computer Book Code', 'File Formats', 'toMags') THEN NULL
                ELSE split_part(substr(fp, 8), '/', 1)
            END
        WHEN fp LIKE '/volume1/common/Media/Books/%' THEN
            CASE
                WHEN split_part(substr(fp, 29), '/', 1) IN ('', 'Computer Book Code', 'File Formats', 'toMags') THEN NULL
                ELSE split_part(substr(fp, 29), '/', 1)
            END
        WHEN fp LIKE '/periodicals/%' OR fp LIKE '/volume1/common/Media/Periodicals/%' THEN 'Periodicals'
        WHEN fp LIKE '/xAdult/%' OR fp LIKE '/xtoAdult/%'
          OR fp LIKE '/volume1/jims_documents/Media/xAdult/%'
          OR fp LIKE '/volume1/jims_documents/Media/xtoAdult/%' THEN 'Archive-X'
        WHEN fp LIKE '/audiobooks/%' OR fp LIKE '/volume1/common/Media/Audiobooks/%' THEN 'Audiobooks'
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION catalog_periodical_series(fp text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN fp LIKE '/periodicals/%' THEN NULLIF(split_part(substr(fp, 14), '/', 1), '')
        WHEN fp LIKE '/volume1/common/Media/Periodicals/%' THEN NULLIF(split_part(substr(fp, 35), '/', 1), '')
        WHEN fp LIKE '/xAdult/%' THEN NULLIF(split_part(substr(fp, 9), '/', 1), '')
        WHEN fp LIKE '/volume1/jims_documents/Media/xAdult/%' THEN NULLIF(split_part(substr(fp, 38), '/', 1), '')
        ELSE NULL
    END;
$$;

CREATE INDEX IF NOT EXISTS idx_filelocation_top_folder
    ON catalog_filelocation (catalog_top_folder(file_path), catalog_item_id);

CREATE INDEX IF NOT EXISTS idx_filelocation_top_folder_lower
    ON catalog_filelocation (LOWER(catalog_top_folder(file_path)), catalog_item_id);

CREATE INDEX IF NOT EXISTS idx_filelocation_periodical_series
    ON catalog_filelocation (catalog_periodical_series(file_path), catalog_item_id)
    WHERE catalog_periodical_series(file_path) IS NOT NULL;
"""

SQL_REVERSE = """
DROP INDEX IF EXISTS idx_filelocation_periodical_series;
DROP INDEX IF EXISTS idx_filelocation_top_folder_lower;
DROP INDEX IF EXISTS idx_filelocation_top_folder;
DROP FUNCTION IF EXISTS catalog_periodical_series(text);
DROP FUNCTION IF EXISTS catalog_top_folder(text);
"""


def _apply_pg_functions(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(SQL_FORWARD)


def _reverse_pg_functions(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(SQL_REVERSE)


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0005_alter_catalogitem_item_type'),
    ]

    operations = [
        migrations.RunPython(_apply_pg_functions, _reverse_pg_functions),
    ]
