"""Create an isolated PostgreSQL test database, run integration verification, then remove it."""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[1]
url = make_url(os.environ['DATABASE_URL'])
name = 'guardrail_test_' + uuid4().hex[:12]
admin = url.set(drivername='postgresql', database='postgres').render_as_string(hide_password=False)
test_url = url.set(database=name).render_as_string(hide_password=False)
with psycopg.connect(admin, autocommit=True) as conn:
    conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        result = subprocess.run([sys.executable,'-m','pytest',str(root/'backend/tests/test_postgres.py'),'-q'],
                                env={**os.environ,'TEST_POSTGRES_URL':test_url,'SANDBOX_MODE':'mock',
                                     'PYTHONPATH':str(root/'backend')})
    finally:
        conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
sys.exit(result.returncode)
