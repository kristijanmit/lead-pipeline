"""Suggested outreach channel — re-exported from the shared `pipeline.channels`
(market rules: order, mobile prefixes, first-touch exclusions, labels)."""

from pipeline.channels import (
    best_channel,
    is_mobile,
    is_placeholder_email,
    is_placeholder_phone,
    normalize_phone,
    usable_channels,
)

suggest_channel = best_channel

__all__ = [
    "best_channel", "is_mobile", "is_placeholder_email", "is_placeholder_phone",
    "normalize_phone", "suggest_channel", "usable_channels",
]
