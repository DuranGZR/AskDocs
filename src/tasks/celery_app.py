from celery import Celery
from celery.signals import setup_logging as celery_setup_logging
from celery.signals import worker_process_shutdown, worker_shutdown

from src.core.config import get_settings
from src.core.logging import setup_logging
from src.core.observability import shutdown_observability


def create_celery_app() -> Celery:
    """Redis broker ve backend ile üretim ayarlı Celery örneği oluşturur."""
    settings = get_settings()

    app = Celery(
        "corerag_worker",
        broker=settings.CELERY_BROKER_URL,
        backend=settings.CELERY_RESULT_BACKEND,
        include=["src.tasks.document_tasks"],
    )

    app.conf.update(
        # Güvenlik: Pickle RCE açığını kapat, yalnızca JSON kabul et
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        # Zaman standardı
        timezone="UTC",
        enable_utc=True,
        # Güvenilirlik & adil iş dağıtımı (büyük PDF görevleri için kritik)
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        # Bellek & Redis şişmesini önleme
        result_expires=3600 * 24,  # 24 saat sonra eski görev sonuçlarını temizle
        worker_max_tasks_per_child=100,  # Native/C bellek sızıntılarına karşı yenileme
        # Başlangıç bağlantı dayanıklılığı & durum takibi
        broker_connection_retry_on_startup=True,
        task_track_started=True,
        # Askıda kalan görev koruması (Linux worker'da geçerli)
        task_soft_time_limit=300,  # 5 dk sonunda SoftTimeLimitExceeded (temiz çıkış)
        task_time_limit=360,  # 6 dk sonunda zorla sonlandır
    )

    return app


@celery_setup_logging.connect
def configure_celery_logging(**kwargs: object) -> None:
    """Celery root logger ezmesini önleyip proje log formatını uygular."""
    settings = get_settings()
    setup_logging(log_level=settings.LOG_LEVEL, log_format=settings.LOG_FORMAT)


@worker_process_shutdown.connect
@worker_shutdown.connect
def shutdown_celery_observability(**kwargs: object) -> None:
    """Worker alt süreci veya ana worker kapanırken bekleyen Langfuse izlerini gönderir."""
    shutdown_observability()


celery_app = create_celery_app()
