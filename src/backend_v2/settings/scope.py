"""Settings domain mapping; every stored domain is a plain global domain."""

from typing import Any


def storage_domain(domain: str) -> str:
    return domain


def public_domain(domain: str) -> str:
    return domain
