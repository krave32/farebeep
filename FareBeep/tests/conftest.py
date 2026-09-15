import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from FareBeep.models import Base


@pytest.fixture(autouse=True)
def _reset_rate_limit_buckets():
    """The per-phone throttle lives in module-global dicts; without this,
    a message-heavy test file (concierge pipeline) throttles later tests
    for the same phone."""
    from FareBeep import main
    main._rate_hits.clear()
    main._rate_cooled.clear()
    yield
    main._rate_hits.clear()
    main._rate_cooled.clear()


@pytest.fixture
def db():
    """In-memory SQLite session - models are portable (Uuid, no PG-only types)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    Base.metadata.drop_all(engine)
