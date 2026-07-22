from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    FRONTEND_URL: str = "http://localhost:3000"
    DATABASE_URL: str = "postgresql://titan:titan_dev_password@localhost:5432/titan_db"
    CLERK_ISSUER_URL: str = ""
    CLERK_JWKS_URL: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
