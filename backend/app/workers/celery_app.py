"""Optional Redis-backed worker; API startup does not require Redis."""
from celery import Celery
from app.config import settings

celery_app = Celery("model_risk", broker=settings.redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_ignore_result=True, timezone="UTC", enable_utc=True,
    worker_prefetch_multiplier=1,
    broker_connection_timeout=3,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    task_publish_retry=False,
    task_time_limit=settings.run_timeout_seconds + 30,
    beat_schedule={"recover-runs": {"task": "model_risk.recover_runs", "schedule": 30.0}},
)
