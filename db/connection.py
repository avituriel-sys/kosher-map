"""Database connection helper.

Reads DATABASE_URL from the environment (or a local .env file, which is
gitignored - see .env.example for the shape). Nothing here ever hardcodes
a connection string.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_connection() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in "
            "your database's connection string."
        )
    return psycopg.connect(url, connect_timeout=15)
