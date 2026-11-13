"""Omega's browser: a DevTools-driven tab in the operator's Edge."""

from .cdp_client import CdpClient, CdpError

__all__ = ["CdpClient", "CdpError"]
