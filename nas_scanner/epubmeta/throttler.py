"""Adaptive throttling and batch rate limiter based on active limiting factors."""

from __future__ import annotations
import logging
import time
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class Throttler:
    """
    Intelligent rate limiter that adapts delays based on the actual limiting factors:
    - AI vision/LLM used: enforces slower pacing (e.g. 4.0s) to respect 15 RPM free tier limits.
    - Catalog API queried: enforces polite pacing (e.g. 1.0s) for Open Library / Google Books.
    - Local only (embedded/cached/quarantined): instantaneous (0.0s), no useless waiting.
    - Rate-limit encountered (429): triggers dynamic backoff.
    - Batch pause: only counts items that actually made network requests.
    """

    def __init__(
        self,
        delay_seconds: float = 1.0,
        ai_delay_seconds: float = 4.0,
        batch_size: int = 5,
        batch_pause_seconds: float = 30.0,
        rate_limit_backoff_seconds: float = 10.0,
        callback: Optional[Callable[[str, float], None]] = None,
    ):
        self.delay_seconds = max(0.0, delay_seconds)
        self.ai_delay_seconds = max(0.0, ai_delay_seconds)
        self.batch_size = max(1, batch_size)
        self.batch_pause_seconds = max(0.0, batch_pause_seconds)
        self.rate_limit_backoff_seconds = max(0.0, rate_limit_backoff_seconds)
        self.callback = callback

        self.processed_count = 0
        self.network_request_count = 0
        self.last_action_time: Optional[float] = None

    def wait_for_next(
        self,
        is_last: bool = False,
        ai_used: bool = False,
        catalog_called: bool = True,
        rate_limited: bool = False,
    ) -> float:
        """
        Wait before starting the next item, scaling dynamically based on limiting factors.
        Returns the duration waited in seconds.
        """
        if is_last:
            return 0.0

        self.processed_count += 1
        made_network_call = ai_used or catalog_called

        if made_network_call:
            self.network_request_count += 1

        # 1. Check if batch pause threshold for network requests is reached
        if (
            self.network_request_count > 0
            and self.network_request_count % self.batch_size == 0
            and self.batch_pause_seconds > 0
            and made_network_call
        ):
            msg = (
                f"Completed batch of {self.batch_size} network requests. "
                f"Pausing for {self.batch_pause_seconds:.1f}s to respect catalog/API limits..."
            )
            logger.info(msg)
            if self.callback:
                self.callback(msg, self.batch_pause_seconds)
            time.sleep(self.batch_pause_seconds)
            self.last_action_time = time.time()
            return self.batch_pause_seconds

        # 2. Rate limit 429 backoff
        if rate_limited and self.rate_limit_backoff_seconds > 0:
            msg = f"Rate limit detected. Backing off for {self.rate_limit_backoff_seconds:.1f}s..."
            logger.warning(msg)
            if self.callback:
                self.callback(msg, self.rate_limit_backoff_seconds)
            time.sleep(self.rate_limit_backoff_seconds)
            self.last_action_time = time.time()
            return self.rate_limit_backoff_seconds

        # 3. Dynamic delay based on operation type
        wait_time = 0.0
        if ai_used:
            wait_time = self.ai_delay_seconds
            logger.debug("AI used: throttling %.1fs to protect LLM rate limits...", wait_time)
        elif catalog_called:
            wait_time = self.delay_seconds
            logger.debug("Catalog queried: throttling %.1fs for polite API etiquette...", wait_time)
        else:
            # Local-only operation (embedded, quarantined, or cached)
            wait_time = 0.0
            logger.debug("Local-only operation: no throttling delay required.")

        if wait_time > 0:
            time.sleep(wait_time)

        self.last_action_time = time.time()
        return wait_time
