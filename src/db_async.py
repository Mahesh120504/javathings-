import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

class DatabaseManager:
    def __init__(self):
        self.pool = None

    async def connect(self):
        try:
            self.pool = await asyncpg.create_pool(dsn=os.getenv("SC2_DATABASE_URL"))
            print("[+] Database connected successfully.")
            await self.create_tables()
        except Exception as e:
            print(f"[-] Database connection failed: {e}")

    async def create_tables(self):
        """Create the violations table in safe scema if it doesn't exist."""
        query = """
        CREATE SCHEMA IF NOT EXISTS safe;
        CREATE TABLE IF NOT EXISTS safe.violations (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            camera_id TEXT,
            person_id INTEGER,
            violation_type TEXT,
            image_path TEXT,
            flags JSONB
        );
        """
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(query)
                print("[+] Table safe.violations ready.")
        except Exception as e:
            print(f"[-] Table creation failed: {e}")

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            print("[-] Database disconnected.")

    # UPDATED: Accepts person_id (int) and flags (json string)
    async def log_violation(self, camera_id, person_id, violation_types, image_path, flags):
        query = """
        INSERT INTO safe.violations (camera_id, person_id, violation_type, image_path, flags)
        VALUES ($1, $2, $3, $4, $5)
        """
        try:
            async with self.pool.acquire() as connection:
                await connection.execute(query, camera_id, person_id, violation_types, image_path, flags)
                print(f"[DB] Logged violation for Person {person_id} on {camera_id}")
        except Exception as e:
            print(f"[-] Failed to log violation: {e}")

db = DatabaseManager()
