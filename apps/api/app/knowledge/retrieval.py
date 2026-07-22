import json
import logging
from typing import List, Dict, Any
from app.knowledge.schemas import RetrievedContext
from app.knowledge.vector_store import VectorStoreManager
from app.core.database import get_db

logger = logging.getLogger(__name__)

class RetrievalEngine:
    """
    Executes Hybrid Search (Keyword + Semantic) and fuses results using Reciprocal Rank Fusion (RRF).
    """

    @staticmethod
    def _compute_rrf(keyword_results: List[Dict], vector_results: List[Dict], k: int = 60) -> List[Dict]:
        """
        Implements Reciprocal Rank Fusion.
        RRF_score = 1 / (k + rank)
        """
        rrf_scores: Dict[str, float] = {}
        chunks_map: Dict[str, Dict] = {}

        # Process Keyword Results
        for rank, row in enumerate(keyword_results):
            chunk_id = row["id"]
            chunks_map[chunk_id] = row
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + (1.0 / (k + rank + 1))

        # Process Vector Results
        for rank, row in enumerate(vector_results):
            chunk_id = row["id"]
            if chunk_id not in chunks_map:
                chunks_map[chunk_id] = row
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + (1.0 / (k + rank + 1))

        # Sort combined results by RRF score
        sorted_results = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        final_fused = []
        for chunk_id, score in sorted_results:
            chunk_data = chunks_map[chunk_id]
            chunk_data["rrf_score"] = score
            final_fused.append(chunk_data)

        return final_fused

    @staticmethod
    async def retrieve_context(query: str, organization_id: str, top_k: int = 5) -> List[RetrievedContext]:
        """
        Performs the full hybrid retrieval pipeline.
        CRITICAL: Every query strictly filters by organization_id.
        """
        db = await anext(get_db())

        # 1. Keyword Search (PostgreSQL Full Text Search using plainto_tsquery)
        # Using simple FTS for demonstration. We fetch top 20 to ensure good fusion pool.
        keyword_query = """
        SELECT id, "documentId", text, metadata, 
               ts_rank(to_tsvector('english', text), plainto_tsquery('english', $1)) as score
        FROM "DocumentChunk"
        WHERE "organizationId" = $2 AND to_tsvector('english', text) @@ plainto_tsquery('english', $1)
        ORDER BY score DESC
        LIMIT 20;
        """
        keyword_results = await db.query_raw(keyword_query, query, organization_id)

        # 2. Vector Search (pgvector cosine similarity <=>)
        embedding_vector = await VectorStoreManager.generate_embedding(query)
        vector_str = f"[{','.join(map(str, embedding_vector))}]"
        
        vector_query = """
        SELECT id, "documentId", text, metadata,
               1 - (embedding <=> $1::vector) as score
        FROM "DocumentChunk"
        WHERE "organizationId" = $2
        ORDER BY embedding <=> $1::vector
        LIMIT 20;
        """
        vector_results = await db.query_raw(vector_query, vector_str, organization_id)

        # 3. Reciprocal Rank Fusion (RRF)
        fused_results = RetrievalEngine._compute_rrf(keyword_results, vector_results)

        # 4. Map to strict Pydantic Output
        # Return only the requested top_k
        output = []
        for row in fused_results[:top_k]:
            output.append(RetrievedContext(
                chunk_id=row["id"],
                document_id=row["documentId"],
                text=row["text"],
                score=row["rrf_score"],
                # Prisma query_raw returns JSONB as dict or string depending on driver, 
                # ensure we parse it if it's a string.
                metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
            ))

        logger.info(f"Retrieved {len(output)} chunks using Hybrid Search for org {organization_id}")
        return output
