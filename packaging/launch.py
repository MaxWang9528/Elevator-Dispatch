"""Entry point for the packaged app (PyInstaller needs a script, not `-m`)."""

from elevator_sim.app import main

if __name__ == "__main__":
    main()
