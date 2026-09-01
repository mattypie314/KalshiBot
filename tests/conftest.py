import os

# Serve starts the 15m / hourly / maker scheduler. Tests must not fire live loops.
os.environ["KALSHI_AUTO"] = "0"
