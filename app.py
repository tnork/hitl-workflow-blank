"""HuggingFace Spaces entrypoint — exposes Flask app for gunicorn."""
from web_app import make_flask_app

application = make_flask_app()
