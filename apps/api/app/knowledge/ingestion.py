import uuid
from typing import List, Dict, Any
from app.knowledge.schemas import DocumentChunk
import logging

logger = logging.getLogger(__name__)

class DocumentIngestionPipeline:
    """
    Handles parsing raw documents and splitting them into intelligent chunks.
    """
    
    def __init__(self, chunk_size: int = 500, overlap: int = 50):
        self.chunk_size = chunk_size
        self.overlap = overlap

    async def process_document(
        self, 
        raw_text: str, 
        organization_id: str, 
        document_id: str, 
        metadata: Dict[str, Any]
    ) -> List[DocumentChunk]:
        """
        Parses raw text, chunks it, and strictly attaches the organization_id.
        """
        if not organization_id:
            raise ValueError("organization_id is strictly required for ingestion.")
            
        logger.info(f"Ingesting document {document_id} for org {organization_id}")
        
        # MOCK IMPLEMENTATION of a sliding window chunker
        # In production, use langchain.text_splitter.RecursiveCharacterTextSplitter
        words = raw_text.split()
        chunks = []
        
        if not words:
            return chunks

        i = 0
        chunk_index = 0
        while i < len(words):
            # Take a slice of words up to chunk_size
            chunk_words = words[i:i + self.chunk_size]
            chunk_text = " ".join(chunk_words)
            
            chunk_metadata = metadata.copy()
            chunk_metadata["chunk_index"] = chunk_index
            
            chunk = DocumentChunk(
                chunk_id=str(uuid.uuid4()),
                document_id=document_id,
                organization_id=organization_id,
                text=chunk_text,
                metadata=chunk_metadata
            )
            chunks.append(chunk)
            
            # Move forward by chunk_size minus overlap
            i += (self.chunk_size - self.overlap)
            chunk_index += 1
            
        logger.info(f"Generated {len(chunks)} chunks for document {document_id}")
        return chunks
