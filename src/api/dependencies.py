from typing import Annotated

from fastapi import Depends

from src.core.config import Settings, get_settings
from src.services.base import BaseStorage
from src.services.storage_service import LocalStorageService

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_storage_service(settings: SettingsDep) -> BaseStorage:
    """Ayarlardaki dizin ve boyut limitiyle dosya depolama servisini oluşturur."""
    return LocalStorageService(
        base_dir=settings.UPLOAD_DIR,
        max_size_bytes=settings.max_upload_size_bytes,
    )


StorageDep = Annotated[BaseStorage, Depends(get_storage_service)]