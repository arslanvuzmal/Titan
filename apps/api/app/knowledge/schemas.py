from pydantic import BaseModel, Field
from typing import Dict, Any


class DocumentChunk(BaseModel):
    """
    Schema representing a chunk of a document to be embedded.
    """

    chunk_id: str = Field(..., description="Unique identifier for the chunk.")
    document_id: str = Field(..., description="Parent document ID.")
    organization_id: str = Field(..., description="Strict tenant isolation key.")
    text: str = Field(..., description="The actual text content of the chunk.")
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Source, page number, author, etc."
    )


class RetrievedContext(BaseModel):
    """
    Schema representing a document chunk returned from hybrid search.
    """

    chunk_id: str
    document_id: str
    text: str
    score: float = Field(
        ..., description="The combined RRF score or raw similarity score."
    )
    metadata: Dict[str, Any]


class MemoryItem(BaseModel):
    """
    Schema for persisting memory items.
    """

    id: str
    organization_id: str
    memory_type: str = Field(..., description="'BUSINESS', 'EPISODIC', or 'SHORT_TERM'")
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
