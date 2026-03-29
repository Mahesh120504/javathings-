from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
import os
from dotenv import load_dotenv

load_dotenv()

# Use env var, fallback to empty string (which might fail but better than hardcoded password in repo)
# Ideally, user should provide DATABASE_URL.
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # Print warning or handle it. For now, assuming it will be provided.
    # We can keep the original as a comment if needed for reference, but best to remove secrets.
    pass

# If DATABASE_URL is None, create_engine will fail.
# For integration purposes, we assume the environment is set up.
# If I must provide a default for "functioning", I should NOT use the password 'Ayesha02'.
# I'll rely on os.getenv.

engine = create_engine(
    DATABASE_URL if DATABASE_URL else "postgresql://user:password@localhost/dbname",
    pool_pre_ping=True,
    echo=False
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
