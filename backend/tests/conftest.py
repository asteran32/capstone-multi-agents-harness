import os
os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ['APP_USERNAME'] = 'researcher'
os.environ['APP_PASSWORD'] = 'test-password'
os.environ['SANDBOX_MODE'] = 'mock'

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app import db
from app.seed import seed


@pytest.fixture()
def database(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool,
                           execution_options={'schema_translate_map': {'app': None}})
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, 'Session', sessionmaker(engine, expire_on_commit=False))
    seed()
    yield engine
    engine.dispose()
