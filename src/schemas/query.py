from typing import Literal

from pydantic import BaseModel, Field, field_validator

TENANT_ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"


class ConversationMessage(BaseModel):
    """Çok turlu sohbet geçmişindeki tekil kullanıcı veya asistan mesajı."""

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def validate_non_empty_content(cls, value: str) -> str:
        """Boşluktan ibaret geçmiş mesajlarını reddeder."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("History message content must not be empty or whitespace only")
        return cleaned


class QueryRequest(BaseModel):
    """Semantik RAG soru sorma isteğinin doğrulama şeması."""

    query: str = Field(..., min_length=1, max_length=2000, description="User question")
    tenant_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=TENANT_ID_PATTERN,
        description="Tenant identifier for multi-tenant isolation",
    )
    history: list[ConversationMessage] = Field(
        default_factory=list,
        max_length=20,
        description="Recent conversation messages (sliding window)",
    )
    top_k: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Maximum number of retrieved context chunks",
    )
    score_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Minimum similarity score threshold for retrieved chunks",
    )
    stream: bool = Field(
        default=False,
        description="Enable Server-Sent Events (SSE) token-by-token streaming",
    )

    @field_validator("query")
    @classmethod
    def validate_non_empty_query(cls, value: str) -> str:
        """Yalnızca boşluk içeren sorguları reddeder ve kenar boşluklarını temizler."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Query must not be empty or whitespace only")
        return cleaned


class CitationItem(BaseModel):
    """Yanıta dayanak oluşturan kaynak doküman parçası atfı."""

    document_id: str
    page_number: int
    chunk_index: int
    score: float
    text: str
    file_type: str
    filename: str = ""


class QueryResponse(BaseModel):
    """Semantik arama ve LLM üretimi sonrası standart RAG yanıt şeması."""

    query: str
    tenant_id: str
    answer: str
    citations: list[CitationItem]


class SSETokenEvent(BaseModel):
    """SSE akışı sırasında üretilen tekil kelime/token parçacığı paketi."""

    event: Literal["token"] = "token"
    delta: str


class SSECitationsEvent(BaseModel):
    """SSE akışı sonunda kullanılan doküman/sayfa referanslarını taşıyan paket."""

    event: Literal["citations"] = "citations"
    query: str
    tenant_id: str
    citations: list[CitationItem]


class SSEErrorEvent(BaseModel):
    """SSE akışı başladıktan sonra oluşan beklenmedik kesintileri bildiren paket."""

    event: Literal["error"] = "error"
    code: str
    message: str
    request_id: str = "-"
