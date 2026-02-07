"""Allow running the CLI as python -m nordvpn (e.g. for testing without installing)."""

from .cli import main

if __name__ == "__main__":
    main()
