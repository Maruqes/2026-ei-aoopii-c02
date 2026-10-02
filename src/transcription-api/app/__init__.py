"""Load the API lazily so the standalone OAuth helper needs no API dependencies."""


def __getattr__(name):
    if name in {"app", "create_app"}:
        from .main import app, create_app

        return {"app": app, "create_app": create_app}[name]
    raise AttributeError(name)


__all__ = ["app", "create_app"]
