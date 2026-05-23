import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Use environment variables so Docker can override them
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "True") == "True"

    app.run(host=host, port=port, debug=debug)
