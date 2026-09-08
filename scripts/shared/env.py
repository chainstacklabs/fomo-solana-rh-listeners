"""Where an endpoint comes from, and what happens when it is not there.

Every script reads its endpoints from `.env` and none of them substitute anything for a
missing one: a script started without its endpoint exits naming the variable to set. Eight
scripts were each carrying the same three lines to say so, which is three lines too many to
keep saying identically by hand. `.env.example` carries the names.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def require(*names):
    """The values, or exit naming every variable that is missing.

    The name is the message: a script that stops without saying which variable to set
    sends you looking through its imports for the answer. Every one that is missing is
    named at once, so filling them in is one trip to `.env` rather than one per run.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        sys.exit(f"set {' and '.join(missing)} in .env")
    return [os.environ[name] for name in names]


def endpoint(name):
    """One endpoint, or exit naming it."""
    return require(name)[0]


def optional(name):
    """The endpoint, or None. For the callers that decide for themselves."""
    return os.environ.get(name)
