"""Tracing and logs that describe requests without ever containing them.

Spans and log lines carry ids, model choices, token counts, costs and timings. Message content,
completions and secrets are never attributes; a test checks every span and log record of a run.
"""

from __future__ import annotations

import logging
import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from mrp.secrets import RedactingFilter

tracer = trace.get_tracer("mrp")


def setup(service: str = "mrp-gateway", exporter: SpanExporter | None = None) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if exporter is None and os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter()
        except ImportError:  # the OTLP exporter is an optional dependency
            exporter = None
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def logger(name: str = "mrp") -> logging.Logger:
    log = logging.getLogger(name)
    if not any(isinstance(f, RedactingFilter) for f in log.filters):
        log.addFilter(RedactingFilter())
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter('{"t":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'))
        h.addFilter(RedactingFilter())
        log.addHandler(h)
        log.setLevel(os.environ.get("MRP_LOG_LEVEL", "INFO"))
    return log
