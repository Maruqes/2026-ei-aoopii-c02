import os
import uuid
from urllib.parse import quote

import psycopg2
import pytest

from data.apply_migrations import apply_migrations
from data.repository import DataRepository


@pytest.fixture
def repository():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    schema = "bot_test_" + uuid.uuid4().hex
    admin = psycopg2.connect(url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{schema}"')
    scoped_url = (
        url
        + ("&" if "?" in url else "?")
        + "options="
        + quote(f"-csearch_path={schema}")
    )
    apply_migrations(scoped_url)
    try:
        yield DataRepository(scoped_url)
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
