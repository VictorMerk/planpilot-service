import os
from pathlib import Path

from flask import Flask
from flask_cors import CORS
from .persistence.db import db
from .persistence import models
from .rest.sessions import sessions_bp


def configure_extensions(app: Flask):
    allowed_origins = [
        origin.strip()
        for origin in os.environ.get(
            "PLANPILOT_ALLOWED_ORIGINS", "http://localhost:4200"
        ).split(",")
        if origin.strip()
    ]
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})


def configure_blueprints(app: Flask):
    app.register_blueprint(sessions_bp, url_prefix="/api")


def configure_database(app: Flask):
    database_path = Path(app.instance_path) / "planpilot.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "PLANPILOT_DATABASE_URL", f"sqlite:///{database_path}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.create_all()


def create_app() -> Flask:
    app = Flask(__name__)

    configure_extensions(app)
    configure_blueprints(app)
    configure_database(app)

    return app
