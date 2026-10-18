"""PyInstaller entry point; source users continue using python -m ups_monitor."""
from ups_monitor.entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
