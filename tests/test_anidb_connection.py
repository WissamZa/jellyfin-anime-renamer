#!/usr/bin/env python3
"""
Test script to verify the live connection, authentication, and communication with AniDB.
This script reads the credentials from your `.env` file.
"""

import contextlib
import sys

from renamer.config import Config
from renamer.providers.anidb import AniDBClient


def main():
    print("Loading configuration from environment/dotenv...")
    cfg = Config.from_env()

    # Basic validations
    if not cfg.anidb_username or not cfg.anidb_password:
        print("ERROR: ANIDB_USERNAME and ANIDB_PASSWORD must be configured in your .env file!")
        sys.exit(1)

    print(f"Testing AniDB connection for user: {cfg.anidb_username}")
    print(f"Configured Client Name: {cfg.anidb_client} (version {cfg.anidb_client_ver})")

    # Initialize the real client
    client = AniDBClient(
        username=cfg.anidb_username,
        password=cfg.anidb_password,
        client_name=cfg.anidb_client,
        client_ver=cfg.anidb_client_ver,
        api_key=cfg.anidb_api_key or None,
    )

    print("Attempting to connect and authenticate with AniDB...")
    success = client.connect()

    if success:
        print("\nSUCCESS! Successfully connected and authenticated with AniDB UDP API!")
        if client._session:
            print(f"Session established: {client._session[:6]}...")

        # Gracefully logout
        print("Closing the session (logging out)...")
        with contextlib.suppress(AttributeError):
            client.disconnect()
    else:
        print("\nFAILED: Could not authenticate with AniDB.")
        print(
            "Check the details above and verify that your username, password, and registered client name are correct."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
