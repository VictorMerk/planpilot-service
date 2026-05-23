from flask import Flask
from flask_cors import CORS
from .persistence.db import db
from .persistence import models
from .rest.sessions import sessions_bp


def configure_extensions(app: Flask):
    CORS(app)


def configure_blueprints(app: Flask):
    app.register_blueprint(sessions_bp, url_prefix="/api")


def configure_database(app: Flask):
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///mydatabase.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        db.drop_all()
        db.create_all()


def create_app() -> Flask:
    app = Flask(__name__)

    configure_extensions(app)
    configure_blueprints(app)
    configure_database(app)

    return app
