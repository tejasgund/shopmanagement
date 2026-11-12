"""
scheduler/_bootstrap.py - the two lines every entry script runs before it can
import anything from this package.

    1. Put the folder CONTAINING `scheduler/` on sys.path, so `python
       scheduler/run_rent_generation.py` works as well as `python -m
       scheduler.run_rent_generation`. This is the package's own parent
       directory, not the application: the scheduler folder can be copied
       anywhere and still finds itself.

    2. Load a .env if there is one, WITHOUT overriding anything already in the
       environment. Cron starts with almost no environment, so a deployment
       that keeps its credentials in .env would otherwise find none of them.
       scheduler/.env wins over one beside the folder, so a scheduler deployed
       on its own can carry its own credentials.

Imported for its side effects, before any `from scheduler...` line - which is
why the entry scripts carry a `# noqa: E402` on the imports that follow.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)

if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

try:
    from dotenv import load_dotenv

    for candidate in (os.path.join(_HERE, ".env"), os.path.join(_PARENT, ".env")):
        if os.path.isfile(candidate):
            load_dotenv(candidate)
            break
except Exception:      # python-dotenv absent, or no .env - both fine
    pass
