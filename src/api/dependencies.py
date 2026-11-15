from typing import Annotated

from fastapi import Depends, Request

from src.core.config import Settings, get_settings
from src.services.base import BaseLLMClient, BaseStorage, BaseVectorDB, DocumentTaskDispatcher
from src.services.llm_service import AsyncOpenAICompatibleLLMClient
from src.services.qdrant_service import AsyncQdrantService
from src.services.storage_service import LocalStorageService
from src.tasks.document_tasks import CeleryDocumentTaskDispatcher

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_storage_service(settings: SettingsDep) -> BaseStorage:
    """Ayarlardaki dizin ve boyut limitiyle dosya depolama servisini oluşturur."""
    return LocalStorageService(
        base_dir=settings.UPLOAD_DIR,
        max_size_bytes=settings.max_upload_size_bytes,
    )


StorageDep = Annotated[BaseStorage, Depends(get_storage_service)]


def get_task_dispatcher() -> DocumentTaskDispatcher:
    """Doküman işleme görevlerini Celery kuyruğuna ileten servisi oluşturur."""
    return CeleryDocumentTaskDispatcher()


TaskDispatcherDep = Annotated[DocumentTaskDispatcher, Depends(get_task_dispatcher)]


def get_vector_db(request: Request, settings: SettingsDep) -> BaseVectorDB:
    """Lifespan'de hazırlanan paylaşımlı asenkron Qdrant istemcisini döndürür."""
    vector_db: BaseVectorDB | None = getattr(request.app.state, "vector_db", None)
    if vector_db is not None:
        return vector_db
    return AsyncQdrantService(
        url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
        vector_size=settings.QDRANT_VECTOR_SIZE,
    )


VectorDBDep = Annotated[BaseVectorDB, Depends(get_vector_db)]


def get_llm_client(request: Request, settings: SettingsDep) -> BaseLLMClient:
    """Lifespan'de hazırlanan paylaşımlı asenkron LLM/Embedding istemcisini döndürür."""
    llm_client: BaseLLMClient | None = getattr(request.app.state, "llm_client", None)
    if llm_client is not None:
        return llm_client
    return AsyncOpenAICompatibleLLMClient(
        base_url=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY.get_secret_value(),
        llm_model=settings.LLM_MODEL,
        embedding_model=settings.EMBEDDING_MODEL,
        timeout_seconds=settings.LLM_TIMEOUT_SECONDS,
    )


LLMClientDep = Annotated[BaseLLMClient, Depends(get_llm_client)]
