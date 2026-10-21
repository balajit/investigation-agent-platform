"""Synthetic Python fixture: environment-backed configuration declarations."""

import os

TIMEOUT = os.getenv("TIMEOUT", "30")

RETRIES = int(os.getenv("RETRIES", "3"))

API_KEY = os.environ["API_KEY"]

DEBUG = os.getenv("DEBUG", False)
