"""Main entry point for the web dashboard."""

import sys

from .server import main

if __name__ == "__main__":
    main(sys.argv[1:])