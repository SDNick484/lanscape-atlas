"""Capped exponential backoff that is defined for every failure count."""

from __future__ import annotations

import sys

# 2.0**1023 is the largest power of two a float holds; 2**1024 cannot be converted to float
# (OverflowError). Any positive base reaches any sane cap long before this exponent, so
# clamping it here never changes a result below the cap.
MAX_EXPONENT = sys.float_info.max_exp - 1


def capped_backoff(base_s: float, failures: int, cap_s: float) -> float:
    """``min(base_s * 2**failures, cap_s)`` for any ``failures >= 0``, without overflow."""
    # base_s * 2.0**MAX_EXPONENT may be inf (float multiplication does not raise); min() caps it.
    return float(min(base_s * 2.0 ** min(failures, MAX_EXPONENT), cap_s))
