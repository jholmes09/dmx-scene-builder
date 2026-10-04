"""PyInstaller entry point (absolute imports; the packaged app has no parent package to import relatively)."""
import sys

from scenebuilder.desktop import main

if __name__ == "__main__":
    sys.exit(main())
