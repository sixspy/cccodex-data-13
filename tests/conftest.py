import os
import tempfile

import pytest

from app import create_app
from app.db import init_db


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    application = create_app()
    application.config["DATABASE"] = path
    application.config["TESTING"] = True
    with application.app_context():
        init_db(application)
    yield application
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def client(app):
    return app.test_client()
