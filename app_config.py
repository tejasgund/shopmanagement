"""
app_config.py - small, dependency-free constants shared across app.py and
router modules. Kept separate so routers never need to import app.py itself
(which would be circular, since app.py is what wires the routers together).
"""

APP_TIMEZONE = "Asia/Kolkata"
