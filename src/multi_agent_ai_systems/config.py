"""settings read from environment variables"""

import os


class ConfigError(RuntimeError):
    pass

def sec_user_agent() -> str:
    """Return the User-Agent SEC requires, e.g. 'Jane Doe jane@example.com'."""
    value = os.environ.get("SEC_USER_AGENT", "").strip()
    if not value:
        raise ConfigError("SEC_USER_AGENT is not set. SEC requires a User-Agent with contact info, "
            "e.g. 'Jane Doe jane@example.com'. Copy .env.example to .env and fill it in.")
    return value