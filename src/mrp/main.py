"""ASGI entry point: `uvicorn mrp.main:app`."""

from mrp.app import Settings, create_app
from mrp.telemetry import setup

setup()
app = create_app(Settings.from_env())
