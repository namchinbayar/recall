"""The tests run the same on every machine: recall's settings come from an empty env file, not
from this machine's ~/.config/recall/env or a .env in the folder they are started from."""

import os
import tempfile

_env = tempfile.NamedTemporaryFile(prefix="recall-test-env-", suffix=".env", delete=False)
_env.close()
os.environ["RECALL_ENV"] = _env.name        # config stops at the first env file it finds
os.environ.pop("RECALL_OWNER", None)
