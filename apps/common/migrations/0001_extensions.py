"""
Create the Postgres extensions the schema depends on.

These are also created by docker/db/init/01-extensions.sql, but that script
only runs once, on first initialisation of the main database. Test databases
and any database created by other means would not get them, so the
dependency belongs in a migration where Django can guarantee it.

``apps.employees`` depends on this migration, which is what puts it ahead of
the first table that uses a ``vector`` column.
"""

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    initial = True

    dependencies: list = []

    operations = [
        # Face embeddings (ArcFace, 512-d) and cosine similarity search.
        VectorExtension(),
        # Fast employee name / code lookup in the directory search.
        TrigramExtension(),
    ]
