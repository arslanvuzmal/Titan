from prisma import Prisma
import logging

logger = logging.getLogger(__name__)

# Global Prisma client instance
db = Prisma()

async def get_db():
    """
    FastAPI dependency that yields a database transaction/session.
    We use the Prisma Python client. If the connection isn't established,
    we connect. Then we yield an interactive transaction so that 
    unhandled exceptions will automatically trigger a rollback.
    """
    if not db.is_connected():
        await db.connect()
        logger.info("Connected to Prisma database.")
        
    # Yield an interactive transaction. 
    # If the block executes successfully, it commits.
    # If an exception is raised, the transaction rolls back.
    async with db.tx() as transaction:
        yield transaction
