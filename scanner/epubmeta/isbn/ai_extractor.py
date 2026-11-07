"""AI-based book identifier and ISBN extractor using Google Gemini API."""

from __future__ import annotations
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .validator import normalize_isbn

logger = logging.getLogger(__name__)


class GeminiBookExtractor:
    """Uses Google Gemini to inspect front matter text or page images to determine book identity and ISBN."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-3.5-flash",
        raise_on_quota: bool = False,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = model
        self.raise_on_quota = raise_on_quota
        self.quota_exhausted = False
        self._client = None

    @property
    def is_available(self) -> bool:
        return bool(self.api_key) and not getattr(self, "quota_exhausted", False)

    def _get_client(self):
        if not self._client:
            if not self.api_key:
                raise ValueError(
                    "Gemini API key is required. Set GEMINI_API_KEY environment variable or pass --gemini-api-key."
                )
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def extract(
        self,
        text: Optional[str] = None,
        images: Optional[List[bytes]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze front-matter text and/or images to extract book metadata.
        Returns a dict:
        {
            "isbn13": Optional[str],
            "isbn10": Optional[str],
            "title": Optional[str],
            "authors": List[str],
            "publisher": Optional[str],
            "published_year": Optional[str],
        }
        """
        if not self.is_available:
            logger.debug("Gemini API key not configured; skipping AI extraction.")
            return {}

        prompt = (
            "You are an expert cataloger and book metadata assistant. "
            "Analyze the provided text and/or page images (which are the title page, copyright page, "
            "or cover of a book, RPG rulebook/supplement, manual, or magazine). Identify and extract the book's metadata.\n\n"
            "Respond ONLY with a valid JSON object with the following keys:\n"
            "{\n"
            '  "isbn13": "string or null",\n'
            '  "isbn10": "string or null",\n'
            '  "title": "string or null",\n'
            '  "authors": ["author 1", ...],\n'
            '  "publisher": "string or null",\n'
            '  "published_year": "string or null",\n'
            '  "description": "1-3 sentence synopsis of this book/rulebook/supplement or null",\n'
            '  "subjects": ["subject/genre 1", ...]\n'
            "}\n"
            "Important instructions:\n"
            "- If there are multiple ISBNs (e.g. print, e-book, cloth, paperback), prefer the e-book/ePub ISBN if available, otherwise print ISBN-13.\n"
            "- Clean hyphens and spaces from the ISBN numbers if possible.\n"
            "- Use any FILE PATH CONTEXT (such as series/game folder names like 'Classic Traveller') together with the cover/page image to determine the full accurate title, author/designer, and publisher.\n"
            "- If no explicit person author is printed on the cover, use the known designer/author of the work or the publisher/studio (e.g. 'Marc Miller' or 'Game Designers\\' Workshop').\n"
            "- If you cannot determine a field, set it to null (or empty list for authors/subjects).\n"
        )

        contents: List[Any] = [prompt]

        if text:
            # Send sample of front matter (limit to 12,000 characters to stay concise)
            snippet = text[:12000].strip()
            if snippet:
                contents.append(f"--- DOCUMENT FRONT-MATTER TEXT ---\n{snippet}\n--- END TEXT ---")

        if images:
            from google.genai import types
            for i, img_bytes in enumerate(images[:3]):  # Send up to 3 front pages
                contents.append(
                    types.Part.from_bytes(data=img_bytes, mime_type="image/png")
                )

        if len(contents) == 1:
            # Neither text nor images provided
            return {}

        try:
            client = self._get_client()
            from google.genai import types
            config = types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
            )
            response = client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )

            raw_text = response.text or ""
            # Strip markdown json fences if present
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()

            data = json.loads(cleaned)
            result = {
                "isbn13": None,
                "isbn10": None,
                "title": data.get("title") or None,
                "authors": [a.strip() for a in data.get("authors", []) if isinstance(a, str) and a.strip()],
                "publisher": data.get("publisher") or None,
                "published_year": str(data.get("published_year")) if data.get("published_year") else None,
                "description": str(data.get("description")).strip() if data.get("description") else None,
                "subjects": [
                    s.strip()
                    for s in (data.get("subjects") or [])
                    if isinstance(s, str) and s.strip()
                ],
            }

            # Validate any returned ISBN
            for key in ("isbn13", "isbn10"):
                val = data.get(key)
                if val:
                    i13, i10 = normalize_isbn(str(val))
                    if i13:
                        result["isbn13"] = i13
                        result["isbn10"] = i10
                        break

            return result

        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "Quota exceeded" in err_str:
                if self.raise_on_quota:
                    from ..exceptions import RateLimitExceededError
                    retry_match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
                    retry_sec = float(retry_match.group(1)) if retry_match else None
                    raise RateLimitExceededError("Gemini AI", err_str, retry_after=retry_sec) from exc
                self.quota_exhausted = True
                logger.warning("Gemini AI daily quota reached (429 RESOURCE_EXHAUSTED). Disabling AI for remaining files.")
            else:
                logger.warning("Gemini AI extraction failed: %s", exc)
            return {}
