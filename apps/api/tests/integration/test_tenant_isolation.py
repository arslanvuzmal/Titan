import pytest
import uuid
from app.knowledge.schemas import DocumentChunk
from app.knowledge.retrieval import RetrievalEngine

# In a real environment, this would use a database fixture with rollback
# Since Prisma raw queries are complex to mock purely in unit tests without a real DB, 
# we structure the test logically.

@pytest.mark.asyncio
async def test_tenant_isolation_retrieval(monkeypatch):
    """
    Ensures that querying the RAG system strictly filters by organization_id.
    We mock the DB call to verify the SQL string and parameters.
    """
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    
    # Mock the database `query_raw`
    queries_executed = []
    
    class MockDB:
        async def query_raw(self, query: str, *args):
            queries_executed.append({"query": query, "args": args})
            return []
            
    # Mock the context manager injection
    async def mock_get_db():
        yield MockDB()
        
    monkeypatch.setattr("app.knowledge.retrieval.get_db", mock_get_db)
    
    # Execute retrieval for Org B
    await RetrievalEngine.retrieve_context("Test query", org_b)
    
    # Verify BOTH the keyword and vector SQL queries contained Org B's ID as a parameter
    assert len(queries_executed) == 2
    
    keyword_query = queries_executed[0]
    vector_query = queries_executed[1]
    
    # Org B's UUID MUST be in the arguments passed to the raw SQL
    assert org_b in keyword_query["args"], "Tenant Isolation Failure: Org ID not passed to FTS SQL"
    assert org_b in vector_query["args"], "Tenant Isolation Failure: Org ID not passed to Vector SQL"
    
    # Ensure Org A's ID never leaked into the query execution
    assert org_a not in keyword_query["args"]
    assert org_a not in vector_query["args"]
