"""应用装配入口：``python -m app.main`` 或 ``uvicorn app.main:app``。"""

from __future__ import annotations

from .api import create_app
from .config import Settings
from .logging_utils import RunLogger
from .service import JobService
from .store import JobStore


def build_components(settings: Settings | None = None):
    settings = settings or Settings.load()
    store = JobStore(settings.data_dir)
    runs = RunLogger(settings.runs_log_path)
    service = JobService(
        store,
        runs,
        max_samples_per_job=settings.max_samples_per_job,
        max_upload_bytes=settings.max_upload_bytes,
        stream_chunk_samples=settings.stream_chunk_samples,
        defaults={
            "min_silence_ms": settings.default_min_silence_ms,
            "min_activity_ms": settings.default_min_activity_ms,
            "pad_ms": settings.default_pad_ms,
            "merge_gap_ms": settings.default_merge_gap_ms,
            "enter_threshold": settings.default_enter_threshold,
            "exit_threshold": settings.default_exit_threshold,
        },
    )
    app = create_app(service)
    return app, service, settings


app, _service, _settings = build_components()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=_settings.host,
        port=_settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
