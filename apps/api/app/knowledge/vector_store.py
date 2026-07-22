import json
import logging
from typing import List
from app.knowledge.schemas import DocumentChunk
from app.core.database import get_db

logger = logging.getLogger(__name__)

class VectorStoreManager:
    """
    Handles generation of embeddings and pushing them into pgvector via Prisma raw queries.
    """

    @staticmethod
    async def generate_embedding(text: str) -> List[float]:
        """
        Calls the embedding provider.
        Mock implementation. In production, use e.g. openai.AsyncOpenAI().embeddings.create(...)
        """
        # Mocking a 1536-dimensional vector for OpenAI text-embedding-3-small
        return [0.01] * 1536

    @staticmethod
    async def store_chunks(chunks: List[DocumentChunk]):
        """
        Embeds a list of chunks and inserts them into the database.
        Uses raw SQL because Prisma schema `Unsupported("vector(1536)")` requires raw inserts.
        """
        db = await anext(get_db())
        
        for chunk in chunks:
            # 1. Generate Embedding
            embedding_vector = await VectorStoreManager.generate_embedding(chunk.text)
            
            # Format the vector as a string for Postgres e.g., '[0.1, 0.2, ...]'
            vector_str = f"[{','.join(map(str, embedding_vector))}]"
            metadata_json = json.dumps(chunk.metadata)
            
            # 2. Raw SQL Insert
            # We strictly include organization_id
            query = """
            INSERT INTO "DocumentChunk" (id, "documentId", "organizationId", text, embedding, metadata)
            VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
            ON CONFLICT (id) DO UPDATE 
            SET text = EXCLUDED.text, embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata;
            """
            
            try:
                await db.execute_raw(
                    query, 
                    chunk.chunk_id, 
                    chunk.document_id, 
                    chunk.organization_id, 
                    chunk.text, 
                    vector_str, 
                    metadata_json
                )
            except Exception as e:
                logger.error(f"Failed to insert chunk {chunk.chunk_id}: {str(e)}")
                raise
                
        logger.info(f"Successfully stored {len(chunks)} chunks in vector store.")
