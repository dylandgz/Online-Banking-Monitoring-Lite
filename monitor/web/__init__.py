"""Web layer. Re-exports the FastAPI app so uvicorn's existing "monitor.web:app" import
string keeps working now that this is a package (routes in app.py, frontend in
templates/ + static/) rather than a single module."""
from monitor.web.app import app

__all__ = ["app"]
