"""Web layer. Re-exports the FastAPI app so uvicorn's existing "monitor.web:app" import
string keeps working now that this is a package (routes in app.py, frontend in
templates/ + static/) rather than a single module.

GOTCHA, worth knowing before you debug something confusing: the re-export below binds the
name `app` in this package's namespace to the FastAPI *instance*, which shadows the
`monitor.web.app` *submodule*. So `import monitor.web.app as m; m.some_module_global`
raises AttributeError -- `m` is the FastAPI object, not the module. Cost a real debugging
detour on 2026-08-24 while testing the export row cap. To reach the module itself (e.g. to
monkeypatch a module-level constant in a test), use one of:

    import sys; mod = sys.modules["monitor.web.app"]
    from monitor.web import app as app_module   # <- also the instance, same trap
    import importlib; mod = importlib.import_module("monitor.web.app")

Renaming the submodule (say to routes.py) would remove the collision, but `"monitor.web:app"`
is what systemd/uvicorn invocations and the README already use, and keeping that stable is
worth more than avoiding this one footgun -- hence a documented gotcha rather than a rename."""
from monitor.web.app import app

__all__ = ["app"]
