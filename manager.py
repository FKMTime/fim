#!/usr/bin/env python3
"""FKMTime Instance Manager - thin entry point."""
from fim.docker import sanitize_wifi_value  # noqa: F401 — backward compat for tests
from fim.main import main

if __name__ == "__main__":
    main()
