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

    PROMPT_INSTRUCTIONS = (
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
        "- Always provide a concise 1-3 sentence 'description' summarizing what this book, manual, rulebook, or document is about based on its title, subject, and front matter (do not leave 'description' null when the work is identifiable).\n"
        "- If you cannot determine another field, set it to null (or empty list for authors/subjects).\n"
    )

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-3.5-flash-lite",
        raise_on_quota: bool = False,
        state_tracker: Optional[Any] = None,
        max_daily_cost_usd: Optional[float] = None,
        input_price_per_1m: Optional[float] = None,
        output_price_per_1m: Optional[float] = None,
        batch_mode: bool = False,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = model or "gemini-3.5-flash-lite"
        self.raise_on_quota = raise_on_quota
        self.state_tracker = state_tracker
        self.max_daily_cost_usd = (
            float(max_daily_cost_usd) if max_daily_cost_usd is not None else None
        )
        m_low = self.model.lower()
        if input_price_per_1m is None or output_price_per_1m is None:
            if "lite" in m_low:
                default_in, default_out = 0.10, 0.40
            elif "pro" in m_low:
                default_in, default_out = 1.25, 5.00
            else:
                default_in, default_out = 0.30, 2.50
            self.input_price_per_1m = float(input_price_per_1m if input_price_per_1m is not None else default_in)
            self.output_price_per_1m = float(output_price_per_1m if output_price_per_1m is not None else default_out)
        else:
            self.input_price_per_1m = float(input_price_per_1m)
            self.output_price_per_1m = float(output_price_per_1m)
        self.batch_mode = bool(batch_mode)
        self._session_cost_usd = 0.0
        self._session_requests = 0
        self.quota_exhausted = False
        self._client = None

    @classmethod
    def build_prompt_for_text(cls, text: Optional[str]) -> Optional[str]:
        """Build a combined prompt string for front-matter text inspection (used by both sync and Batch API)."""
        if not text:
            return None
        snippet = text[:12000].strip()
        if not snippet:
            return None
        return f"{cls.PROMPT_INSTRUCTIONS}\n--- DOCUMENT FRONT-MATTER TEXT ---\n{snippet}\n--- END TEXT ---"

    @staticmethod
    def parse_ai_response_text(raw_text: str) -> Dict[str, Any]:
        """Parse and validate a JSON response string from Gemini into a standardized metadata dict."""
        if not raw_text:
            return {}
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except Exception:
            m_json = re.search(r"\{.*\}", cleaned, re.S)
            if not m_json:
                raise
            data = json.loads(m_json.group(0))
        if not isinstance(data, dict):
            return {}
        result: Dict[str, Any] = {
            "isbn13": None,
            "isbn10": None,
            "title": data.get("title") or None,
            "authors": [
                a.strip()
                for a in (data.get("authors") or [])
                if isinstance(a, str) and a.strip()
            ],
            "publisher": data.get("publisher") or None,
            "published_year": str(data.get("published_year")) if data.get("published_year") else None,
            "description": str(data.get("description")).strip() if data.get("description") else None,
            "subjects": [
                s.strip()
                for s in (data.get("subjects") or [])
                if isinstance(s, str) and s.strip()
            ],
        }
        for key in ("isbn13", "isbn10", "isbn"):
            val = data.get(key)
            if val:
                i13, i10 = normalize_isbn(str(val))
                if i13:
                    result["isbn13"] = i13
                    result["isbn10"] = i10
                    break
        return result

    def _check_daily_budget(self) -> None:
        """Verify that today's cumulative AI spend has not reached max_daily_cost_usd."""
        if self.max_daily_cost_usd is None:
            return
        if self.max_daily_cost_usd <= 0:
            self.quota_exhausted = True
            return

        if self.state_tracker and hasattr(self.state_tracker, "get_daily_ai_usage"):
            usage = self.state_tracker.get_daily_ai_usage()
            spent = float(usage.get("estimated_cost_usd") or 0.0)
            reqs = int(usage.get("requests_count") or 0)
        else:
            spent = self._session_cost_usd
            reqs = self._session_requests

        # Reserve a $0.002 safety margin for the upcoming request so we never overshoot the cap
        if spent + 0.002 > self.max_daily_cost_usd or spent >= self.max_daily_cost_usd:
            self.quota_exhausted = True
            msg = (
                f"Daily AI budget cap reached (${spent:.4f} / ${self.max_daily_cost_usd:.2f} USD "
                f"across {reqs} requests today)"
            )
            if self.raise_on_quota:
                from ..exceptions import RateLimitExceededError
                raise RateLimitExceededError("Gemini AI Daily Budget", msg)
            logger.warning("%s. Disabling Gemini AI until tomorrow UTC.", msg)

    def _record_call_usage(self, response: Any, fallback_chars: int) -> None:
        """Extract token counts from Gemini response and persist cost to SQLite state tracker."""
        usage = getattr(response, "usage_metadata", None)
        in_tokens = getattr(usage, "prompt_token_count", None) if usage else None
        out_tokens = getattr(usage, "candidates_token_count", None) if usage else None
        thought_tokens = getattr(usage, "thoughts_token_count", None) if usage else None

        if not in_tokens:
            in_tokens = max(fallback_chars // 3, 800)
        total_out_tokens = (int(out_tokens or 120)) + (int(thought_tokens or 0))

        cost_usd = (
            (int(in_tokens) / 1_000_000.0) * self.input_price_per_1m
            + (total_out_tokens / 1_000_000.0) * self.output_price_per_1m
        )
        self._session_cost_usd += cost_usd
        self._session_requests += 1

        if self.state_tracker and hasattr(self.state_tracker, "record_ai_usage"):
            daily = self.state_tracker.record_ai_usage(
                input_tokens=int(in_tokens),
                output_tokens=total_out_tokens,
                cost_usd=cost_usd,
            )
            spent = float(daily.get("estimated_cost_usd") or self._session_cost_usd)
            reqs = int(daily.get("requests_count") or self._session_requests)
        else:
            spent = self._session_cost_usd
            reqs = self._session_requests

        cap_str = f" / ${self.max_daily_cost_usd:.2f}" if self.max_daily_cost_usd is not None else ""
        logger.info(
            "Gemini AI usage: +%d in / +%d out tokens ($%.5f) | Today total: $%.4f%s (%d reqs)",
            int(in_tokens),
            total_out_tokens,
            cost_usd,
            spent,
            cap_str,
            reqs,
        )

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
        file_path_hint: Optional[str] = None,
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
            "description": Optional[str],
            "subjects": List[str],
        }
        """
        if not self.is_available:
            logger.debug("Gemini API key not configured; skipping AI extraction.")
            return {}

        contents: List[Any] = [self.PROMPT_INSTRUCTIONS]

        if file_path_hint:
            contents.append(f"--- FILE PATH CONTEXT ---\n{file_path_hint}\n--- END FILE PATH ---")

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

        if len(contents) == 1 or (len(contents) == 2 and file_path_hint and not text and not images):
            # Neither text nor images provided
            return {}

        # Enforce hard daily USD cost cap before making network call
        self._check_daily_budget()
        if not self.is_available:
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
            self._record_call_usage(
                response,
                fallback_chars=len(text[:12000]) if text else 3000,
            )

            raw_text = response.text or ""
            return self.parse_ai_response_text(raw_text)

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

    DESCRIPTION_PROMPT_INSTRUCTIONS = (
        "You are an expert literary and technical book cataloger. "
        "This book/document did not match external ISBN catalogs and currently needs catalog metadata. "
        "Carefully read the provided actual book content (front matter, table of contents, preface, "
        "introduction, opening chapters, and/or rendered cover/page images) along with any filename/path clues, "
        "and extract its clean human-readable title, author(s), publisher, and an accurate 2-4 sentence description (synopsis) grounded strictly in the book's content.\n\n"
        "Respond ONLY with a valid JSON object with these keys:\n"
        "{\n"
        '  "description": "2-4 sentence informative synopsis based strictly on the document content",\n'
        '  "title": "clean human-readable title (include concise subtitle with \': \' if prominent on cover/title page), else null",\n'
        '  "authors": ["First Last", ...],\n'
        '  "publisher": "publisher or issuing organization if identifiable, else null",\n'
        '  "published_year": "4-digit publication year if stated on copyright/title page, else null",\n'
        '  "isbn": "ISBN-13 or ISBN-10 digits if printed in front matter, else null",\n'
        '  "subjects": ["subject/topic 1", "subject/topic 2", ...]\n'
        "}\n"
        "Important rules:\n"
        "- Base the description directly on the actual topics, chapters, narrative, or technical material inside the document.\n"
        "- Do NOT invent plot points unsupported by the text, and do NOT write generic filler like 'This PDF document contains text.'\n"
        "- Keep the description clear, natural, and catalog-ready (roughly 40 to 110 words, 2 to 4 sentences).\n"
        "- NEVER return raw filenames, DTP production slugs (like '6289_biofuels_rd5.indd', 'Microsoft Word - ...', or '*.pdf'), or 'Unknown Author' as the title or author.\n"
        "- Format person authors in 'First Last' order (using full names from cover/title page when filename only has surnames). If corporate/institutional with no named person author, use the authoring organization.\n"
    )

    TITLE_AUTHOR_PROMPT_INSTRUCTIONS = (
        "You are an expert literary, academic, and technical book cataloger. "
        "The user is fixing a book/document whose stored Title or Author is wrong, missing, or a raw DTP/filename slug (such as '*.indd', 'Untitled', or 'Unknown Author'). "
        "Carefully inspect the rendered cover/title/copyright page images, extracted front-matter text, and file path/filename clues to determine the exact, clean human-readable Title, Author(s), Publisher, Published Year, and ISBN.\n\n"
        "Respond ONLY with a valid JSON object with these keys:\n"
        "{\n"
        '  "title": "clean human-readable title (include concise subtitle with \': \' when prominent on cover/title page)",\n'
        '  "authors": ["First Last", ...],\n'
        '  "publisher": "publisher or issuing organization if identifiable, else null",\n'
        '  "published_year": "4-digit publication year if stated on copyright/title page, else null",\n'
        '  "isbn": "ISBN-13 or ISBN-10 digits if printed in front matter, else null",\n'
        '  "description": "2-4 sentence informative synopsis based strictly on the document content, or null",\n'
        '  "subjects": ["subject/topic 1", "subject/topic 2", ...]\n'
        "}\n"
        "Important rules:\n"
        "- Do NOT parrot back broken stored metadata like '*.indd', '*.qxd', '*.pdf', 'Microsoft Word - ...', underscores, or 'Unknown Author'.\n"
        "- Read the cover image and title/copyright pages carefully, cross-checking with the filename/folder path (e.g., if the filename has author surnames or publisher in parentheses and the cover/front-matter gives their full 'First Last' names and subtitle, use the full proper names and clean title).\n"
        "- Format person authors in standard 'First Last' order. If no individual human author/editor is credited on an institutional report or rulebook, use the authoring organization/studio.\n"
    )

    def generate_description_from_content(
        self,
        text: Optional[str] = None,
        images: Optional[List[bytes]] = None,
        title: Optional[str] = None,
        authors: Optional[str] = None,
        publisher: Optional[str] = None,
        file_path_hint: Optional[str] = None,
        focus_title_author: bool = False,
    ) -> Dict[str, Any]:
        """Generate catalog-ready description and/or clean title & authors grounded in the book's actual content."""
        if not self.is_available:
            logger.debug("Gemini API key not configured; skipping AI content extraction.")
            return {}

        prompt_hdr = self.TITLE_AUTHOR_PROMPT_INSTRUCTIONS if focus_title_author else self.DESCRIPTION_PROMPT_INSTRUCTIONS
        contents: List[Any] = [prompt_hdr]
        meta_lines: List[str] = []
        if file_path_hint:
            meta_lines.append(f"File Path / Filename Clue: {file_path_hint}")
        if title:
            lbl = "Current Stored Title (may be wrong/junk — verify against cover & content)" if focus_title_author else "Current Title"
            meta_lines.append(f"{lbl}: {title}")
        if authors:
            lbl_a = "Current Stored Author(s) (may be wrong/missing — verify against cover & content)" if focus_title_author else "Current Author(s)"
            meta_lines.append(f"{lbl_a}: {authors}")
        if publisher:
            meta_lines.append(f"Current Publisher: {publisher}")
        if meta_lines:
            contents.append("--- EXISTING METADATA / FILE CLUES ---\n" + "\n".join(meta_lines) + "\n--- END CLUES ---")

        has_payload = False
        if text:
            snippet = text[:16000].strip()
            if snippet:
                contents.append(f"--- EXTRACTED BOOK CONTENT ---\n{snippet}\n--- END CONTENT ---")
                has_payload = True

        if images:
            from google.genai import types
            for img_bytes in images[:4]:
                contents.append(types.Part.from_bytes(data=img_bytes, mime_type="image/png"))
                has_payload = True

        if not has_payload:
            return {}

        self._check_daily_budget()
        if not self.is_available:
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
            self._record_call_usage(
                response,
                fallback_chars=len(text[:16000]) if text else 3500,
            )
            raw_text = response.text or ""
            return self.parse_ai_response_text(raw_text)
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "Quota exceeded" in err_str:
                if self.raise_on_quota:
                    from ..exceptions import RateLimitExceededError
                    retry_match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
                    retry_sec = float(retry_match.group(1)) if retry_match else None
                    raise RateLimitExceededError("Gemini AI", err_str, retry_after=retry_sec) from exc
                self.quota_exhausted = True
                logger.warning("Gemini AI daily quota reached (429 RESOURCE_EXHAUSTED).")
            else:
                logger.warning("Gemini AI description generation failed: %s", exc)
            return {}

    def submit_queued_batch_jobs(self, chunk_size: int = 500) -> List[str]:
        """
        Submit any QUEUED items in `gemini_batch_items` via the Gemini Batch API (50% cost reduction).
        Enforces `max_daily_cost_usd` at half-price token rates and leaves any excess items queued for tomorrow.
        Returns list of created batch job names (e.g. `['batches/...']`).
        """
        if not self.api_key or not self.state_tracker or not hasattr(self.state_tracker, "get_queued_ai_batch_items"):
            return []

        queued = self.state_tracker.get_queued_ai_batch_items(limit=4000)
        if not queued:
            return []

        # Batch API pricing is 50% of standard synchronous pricing
        batch_in_price = self.input_price_per_1m * 0.5
        batch_out_price = self.output_price_per_1m * 0.5
        est_out_tokens_per_item = 120

        usage = self.state_tracker.get_daily_ai_usage()
        spent_today = float(usage.get("estimated_cost_usd") or 0.0)

        eligible_items: List[Tuple[Dict[str, Any], int, float]] = []
        running_cost = spent_today
        for item in queued:
            prompt_text = item.get("prompt_text") or ""
            if not prompt_text:
                continue
            est_in_tokens = max(len(prompt_text) // 3, 400)
            item_cost = (
                (est_in_tokens / 1_000_000.0) * batch_in_price
                + (est_out_tokens_per_item / 1_000_000.0) * batch_out_price
            )
            if self.max_daily_cost_usd is not None and (running_cost + item_cost > self.max_daily_cost_usd):
                logger.info(
                    "Gemini Batch API daily budget cap reached ($%.4f / $%.2f). Deferring remaining %d queued item(s) to tomorrow.",
                    running_cost,
                    self.max_daily_cost_usd,
                    len(queued) - len(eligible_items),
                )
                break
            running_cost += item_cost
            eligible_items.append((item, est_in_tokens, item_cost))

        if not eligible_items:
            return []

        from datetime import datetime, timezone
        from google.genai import types

        client = self._get_client()
        created_jobs: List[str] = []

        for i in range(0, len(eligible_items), chunk_size):
            chunk = eligible_items[i : i + chunk_size]
            inlined_reqs: List[types.InlinedRequest] = []
            file_paths: List[str] = []
            chunk_in_tokens = 0
            chunk_out_tokens = 0
            chunk_cost = 0.0

            for item, est_in, item_cost in chunk:
                fp = item["file_path"]
                inlined_reqs.append(
                    types.InlinedRequest(
                        contents=item["prompt_text"],
                        metadata={"file_path": fp},
                    )
                )
                file_paths.append(fp)
                chunk_in_tokens += est_in
                chunk_out_tokens += est_out_tokens_per_item
                chunk_cost += item_cost

            display_name = f"mylibrary-epubmeta-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{i // chunk_size + 1}"
            try:
                batch_job = client.batches.create(
                    model=self.model,
                    src=inlined_reqs,
                    config=types.CreateBatchJobConfig(display_name=display_name),
                )
                job_name = batch_job.name
                self.state_tracker.mark_ai_batch_submitted(
                    job_name=job_name,
                    model=self.model,
                    file_paths=file_paths,
                    estimated_input_tokens=chunk_in_tokens,
                    estimated_cost_usd=chunk_cost,
                )
                self.state_tracker.record_ai_usage(
                    input_tokens=chunk_in_tokens,
                    output_tokens=chunk_out_tokens,
                    cost_usd=chunk_cost,
                    requests_increment=len(file_paths),
                )
                created_jobs.append(job_name)
                logger.info(
                    "Submitted Gemini Batch API job %s (%d books, est. 50%%-off cost $%.4f)",
                    job_name,
                    len(file_paths),
                    chunk_cost,
                )
            except Exception as exc:
                logger.warning("Failed to submit Gemini Batch API job (%d items): %s", len(file_paths), exc)
                break

        return created_jobs

    def check_and_harvest_batch_jobs(self) -> List[Dict[str, Any]]:
        """
        Poll active Gemini Batch API jobs in `gemini_batch_jobs`.
        For any job that has succeeded, parse all responses, mark the job harvested,
        and return the resolved book items ready for catalog lookup & metadata application.
        """
        if not self.api_key or not self.state_tracker or not hasattr(self.state_tracker, "get_active_ai_batch_jobs"):
            return []

        active_jobs = self.state_tracker.get_active_ai_batch_jobs()
        if not active_jobs:
            return []

        client = self._get_client()
        harvested_items: List[Dict[str, Any]] = []

        for job_row in active_jobs:
            job_name = job_row["job_name"]
            try:
                batch_job = client.batches.get(name=job_name)
            except Exception as exc:
                logger.warning("Could not check status of Gemini Batch job %s: %s", job_name, exc)
                continue

            state_name = getattr(batch_job.state, "name", str(batch_job.state or ""))
            job_items = self.state_tracker.get_ai_batch_items_for_job(job_name)
            items_by_path = {row["file_path"]: row for row in job_items}

            if "SUCCEEDED" in state_name:
                inlined_resps = (
                    batch_job.dest.inlined_responses
                    if (batch_job.dest and batch_job.dest.inlined_responses)
                    else []
                )
                for idx, ir in enumerate(inlined_resps):
                    meta_dict = getattr(ir, "metadata", None) or {}
                    fp = meta_dict.get("file_path")
                    if not fp and idx < len(job_items):
                        fp = job_items[idx]["file_path"]
                    if not fp:
                        continue

                    item_row = items_by_path.get(fp, {})
                    err_obj = getattr(ir, "error", None)
                    resp_obj = getattr(ir, "response", None)
                    raw_text = getattr(resp_obj, "text", None) if resp_obj else None

                    if err_obj or not raw_text:
                        err_msg = str(err_obj) if err_obj else "Empty response in Gemini Batch job"
                        self.state_tracker.complete_ai_batch_item(
                            fp, ai_result=None, status="FAILED", error_message=err_msg
                        )
                        harvested_items.append(
                            {
                                "file_path": fp,
                                "ai_result": {},
                                "fallback_title": item_row.get("fallback_title"),
                                "fallback_author": item_row.get("fallback_author"),
                                "original_metadata_json": item_row.get("original_metadata_json"),
                                "error": err_msg,
                            }
                        )
                        continue

                    try:
                        parsed = self.parse_ai_response_text(raw_text)
                        self.state_tracker.complete_ai_batch_item(
                            fp, ai_result=parsed, status="COMPLETED", error_message=None
                        )
                        harvested_items.append(
                            {
                                "file_path": fp,
                                "ai_result": parsed,
                                "fallback_title": item_row.get("fallback_title"),
                                "fallback_author": item_row.get("fallback_author"),
                                "original_metadata_json": item_row.get("original_metadata_json"),
                                "error": None,
                            }
                        )
                    except Exception as parse_err:
                        self.state_tracker.complete_ai_batch_item(
                            fp, ai_result=None, status="FAILED", error_message=str(parse_err)
                        )
                        harvested_items.append(
                            {
                                "file_path": fp,
                                "ai_result": {},
                                "fallback_title": item_row.get("fallback_title"),
                                "fallback_author": item_row.get("fallback_author"),
                                "original_metadata_json": item_row.get("original_metadata_json"),
                                "error": str(parse_err),
                            }
                        )

                self.state_tracker.update_ai_batch_job_status(job_name, "SUCCEEDED")
                try:
                    client.batches.delete(name=job_name)
                except Exception:
                    pass
                logger.info(
                    "Harvested Gemini Batch job %s (%d responses)",
                    job_name,
                    len(inlined_resps),
                )

            elif any(s in state_name for s in ("FAILED", "CANCELLED", "EXPIRED")):
                self.state_tracker.update_ai_batch_job_status(job_name, state_name)
                for row in job_items:
                    fp = row["file_path"]
                    self.state_tracker.complete_ai_batch_item(
                        fp, ai_result=None, status="FAILED", error_message=f"Batch job {state_name}"
                    )
                logger.warning("Gemini Batch job %s ended with state %s", job_name, state_name)
            else:
                # Still PENDING or RUNNING
                self.state_tracker.update_ai_batch_job_status(job_name, "RUNNING")
                logger.info("Gemini Batch job %s is currently %s (%d items)", job_name, state_name, len(job_items))

        return harvested_items
