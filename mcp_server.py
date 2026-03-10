"""MCP server for Search CAIE past-paper search.

This server is standalone and proxies to the deployed API.

Default backend: https://api.searchcaie.com/api
Override with: MCP_API_BASE

Default transport is stdio. For remote deployment, set:
  MCP_TRANSPORT=streamable-http
  MCP_HOST=0.0.0.0
  MCP_PORT=8000
  MCP_PATH=/mcp
"""

import logging
import os
import re
import sys
import time
from typing import Any, Optional

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import ToolResult


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp-searchcaie")


API_BASE = os.getenv("MCP_API_BASE", "https://api.searchcaie.com/api").rstrip("/")
IMAGE_BASE_URL = os.getenv("MCP_IMAGE_BASE_URL", "https://api.searchcaie.com/api/images")
REQUEST_TIMEOUT = float(os.getenv("MCP_REQUEST_TIMEOUT", "30"))
_default_subject = os.getenv("MCP_DEFAULT_SUBJECT", "").strip()
DEFAULT_SUBJECT = _default_subject or None
QUESTION_URL_BASE = "https://www.searchcaie.com/question"
MAX_SEARCH_LIMIT = 50
MAX_BATCH_IDS = 50
MAX_TOPICS = 12
DEFAULT_PREVIEW_LIMIT = 8
MAX_RECOMMENDED_IDS = 20
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


mcp = FastMCP(
    "searchcaie-search",
    instructions=(
        "You are connected to Search CAIE — a search engine for Cambridge International "
        "A-Level past papers, mark schemes, and examiner reports.\n\n"
        "RECOMMENDED WORKFLOW:\n"
        "1. search_questions(query) → find relevant past paper questions\n"
        "2. get_questions(question_ids_list=[...]) → get full question + mark scheme + images\n"
        "3. search_examiner_reports(query) → examiner insights on common mistakes\n"
        "4. search_web_context(query) → external explanations for deeper understanding\n"
        "5. search_topic_images(query) → supplementary diagrams if needed\n\n"
        "CRITICAL RULES:\n"
        "- ALWAYS cite: Paper, Year, Session, Variant, Question number, ID\n"
        "- Supported subject codes include 9618, 9700, 9702, 9708, and 9709 when indexed\n"
        "- If the user clearly specifies a subject, pass the correct subject code in tool calls\n"
        "- If the user does not specify a subject, do not assume a subject filter by default\n"
        "- For image-based questions, reference question_image_url so student can see the diagram\n"
        "- Use mark scheme key_points as the authoritative answer source\n"
        "- topic_signal reveals exam frequency — highlight overdue/high-frequency topics\n"
        "- Examiner reports reveal WHAT GETS MARKS — prioritize over generic explanations\n"
        "- question_url links to the full question page for students to view\n"
    ),
)


_client: Optional[httpx.Client] = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            base_url=API_BASE,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "MCP-SearchCAIE/3.0"},
            follow_redirects=True,
        )
    return _client


def _tool_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        },
    }


def _error_from_exception(exc: Exception, endpoint: str) -> dict[str, Any]:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status in RETRYABLE_STATUS_CODES
        return _tool_error(
            "UPSTREAM_HTTP_ERROR",
            f"Upstream API returned HTTP {status} for {endpoint}.",
            retryable=retryable,
            details={"status_code": status, "endpoint": endpoint, "api_base": API_BASE},
        )
    if isinstance(exc, httpx.TimeoutException):
        return _tool_error(
            "UPSTREAM_TIMEOUT",
            f"Upstream API timed out for {endpoint}.",
            retryable=True,
            details={"endpoint": endpoint, "api_base": API_BASE, "timeout_s": REQUEST_TIMEOUT},
        )
    if isinstance(exc, httpx.HTTPError):
        return _tool_error(
            "UPSTREAM_NETWORK_ERROR",
            f"Network error while calling {endpoint}: {str(exc)}",
            retryable=True,
            details={"endpoint": endpoint, "api_base": API_BASE},
        )
    return _tool_error(
        "INTERNAL_ERROR",
        f"Internal MCP error while calling {endpoint}: {str(exc)}",
        retryable=False,
        details={"endpoint": endpoint},
    )


def _api_get(endpoint: str, params: Optional[dict[str, Any]] = None) -> dict | list:
    client = _get_client()
    retries = 2
    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            response = client.get(endpoint, params=params)

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < retries:
                wait_s = 0.35 * (2**attempt)
                logger.warning(
                    "Transient upstream status %s on %s, retrying in %.2fs",
                    response.status_code,
                    endpoint,
                    wait_s,
                )
                time.sleep(wait_s)
                continue

            response.raise_for_status()
            return response.json()

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            last_error = exc
            if attempt < retries:
                wait_s = 0.35 * (2**attempt)
                logger.warning("Transient network error on %s, retrying in %.2fs: %s", endpoint, wait_s, exc)
                time.sleep(wait_s)
                continue
            raise
        except Exception as exc:
            last_error = exc
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Unexpected error while calling endpoint {endpoint}")


def _clean_text(value: Any, max_len: int = 220) -> str:
    text = str(value or "")
    text = " ".join(text.replace("\n", " ").split())
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "..."
    return text


def _to_image_url(path: Any) -> Optional[str]:
    """Convert a local filesystem image path to a public URL."""
    if not path:
        return None
    path_str = str(path)
    # Extract just the filename from any path format
    filename = path_str.replace("\\", "/").rsplit("/", 1)[-1]
    if not filename:
        return None
    return f"{IMAGE_BASE_URL}/{filename}"


def _short_session(session_name: Optional[str]) -> str:
    s = (session_name or "").strip().lower()
    if "may" in s:
        return "MJ"
    if "oct" in s:
        return "ON"
    if "feb" in s:
        return "FM"
    return session_name or ""


def _normalize_session_filter(session: Optional[str]) -> Optional[str]:
    if not session:
        return None
    s = session.strip().lower()
    mapping = {
        "mj": "May/June",
        "may/june": "May/June",
        "may june": "May/June",
        "on": "Oct/Nov",
        "oct/nov": "Oct/Nov",
        "oct nov": "Oct/Nov",
        "fm": "Feb/March",
        "feb/march": "Feb/March",
        "feb march": "Feb/March",
    }
    return mapping.get(s, session)


def _spell_correct(query: str) -> tuple[str, bool]:
    try:
        data = _api_get("/spellcheck", {"q": query})
        if isinstance(data, dict) and data.get("was_corrected") and data.get("corrected"):
            return str(data["corrected"]), True
    except Exception as exc:
        logger.debug("Spellcheck unavailable, continuing without correction: %s", exc)
    return query, False


def _result_item(raw: dict[str, Any], index: int, matched_topics: Optional[list[str]] = None) -> dict[str, Any]:
    paper = raw.get("paper") or {}
    session_short = _short_session(paper.get("session_name"))
    paper_number = paper.get("paper_number")
    year = paper.get("year")
    variant = paper.get("variant")

    paper_label_parts: list[str] = []
    if paper_number is not None:
        paper_label_parts.append(f"P{paper_number}")
    if session_short and year is not None:
        paper_label_parts.append(f"{session_short}{year}")
    elif year is not None:
        paper_label_parts.append(str(year))
    if variant is not None:
        paper_label_parts.append(f"v{variant}")

    is_image_based = bool(raw.get("is_image_based"))

    item: dict[str, Any] = {
        "rank": index,
        "id": raw.get("id"),
        "paper": paper_number,
        "year": year,
        "session": session_short,
        "variant": variant,
        "paper_label": " ".join(paper_label_parts),
        "question_number": raw.get("question_number"),
        "marks": raw.get("marks"),
        "relevance_score": raw.get("relevance_score"),
        "snippet": _clean_text(raw.get("question_text"), max_len=200),
        "is_image_based": is_image_based,
        "question_url": f"{QUESTION_URL_BASE}/{raw.get('id')}" if raw.get("id") else None,
    }

    # Include image URLs for image-based questions
    if is_image_based:
        item["question_image_url"] = _to_image_url(raw.get("image_path"))
        item["ms_image_url"] = _to_image_url(raw.get("ms_image_path"))

    # Curated topic intelligence: just the one-line signal
    ti = raw.get("topic_intelligence")
    if isinstance(ti, dict) and ti.get("signal"):
        item["topic_signal"] = ti["signal"]

    # Variant info: count + IDs only, not full copies
    variants = raw.get("duplicate_variants", [])
    if isinstance(variants, list) and len(variants) > 0:
        item["variants_count"] = raw.get("duplicate_group_size", 1)
        item["variant_ids"] = [v.get("id") for v in variants if isinstance(v, dict) and v.get("id")]

    if matched_topics:
        item["matched_topics"] = matched_topics
    return item


def _validate_mode(mode: str) -> bool:
    return mode in {"hybrid", "keyword", "semantic"}


def _normalize_topic_key(topic: str) -> str:
    return re.sub(r"\s+", " ", (topic or "").strip().lower())


def _to_ascii_text(value: Any, max_len: int = 500) -> str:
    cleaned = _clean_text(value, max_len=max_len)
    return cleaned.encode("ascii", errors="ignore").decode("ascii")


def _parse_topics_input(topics: str, topics_list: Optional[list[str]]) -> list[str]:
    merged: list[str] = []
    if isinstance(topics, str) and topics.strip():
        merged.extend(part.strip() for part in topics.split(",") if part.strip())
    if topics_list:
        merged.extend(str(part).strip() for part in topics_list if str(part).strip())

    unique_topics: list[str] = []
    seen: set[str] = set()
    for topic in merged:
        key = _normalize_topic_key(topic)
        if not key or key in seen:
            continue
        seen.add(key)
        unique_topics.append(topic)
    return unique_topics


def _parse_question_ids_input(question_ids: str, question_ids_list: Optional[list[int]]) -> list[int]:
    parsed_ids: list[int] = []

    if isinstance(question_ids, str) and question_ids.strip():
        for part in question_ids.split(","):
            value = part.strip()
            if value:
                parsed_ids.append(int(value))

    if question_ids_list:
        for value in question_ids_list:
            parsed_ids.append(int(value))

    unique_ids: list[int] = []
    for qid in parsed_ids:
        if qid not in unique_ids:
            unique_ids.append(qid)
    return unique_ids


def _result_line(card: dict[str, Any]) -> str:
    score_value = card.get("relevance_score")
    score_text = f"{float(score_value):.3f}" if isinstance(score_value, (int, float)) else "n/a"
    marks = card.get("marks")
    marks_text = f"{marks}m" if marks is not None else "?m"
    question_no = card.get("question_number") or "?"
    img_tag = " [IMG]" if card.get("is_image_based") else ""
    return (
        f"[{card.get('rank')}] ID:{card.get('id')} | {card.get('paper_label', '').strip()} | "
        f"Q{question_no} | {marks_text} | score {score_text}{img_tag} | {card.get('snippet', '')}"
    )


def _build_search_summary(
    *,
    title: str,
    query_note: Optional[str],
    total: int,
    returned: int,
    cards: list[dict[str, Any]],
    topic_lines: Optional[list[str]] = None,
    recommended_ids: Optional[list[int]] = None,
) -> str:
    lines = [f"{title}: {total} found, showing {returned}."]
    if query_note:
        lines.append(query_note)

    if topic_lines:
        lines.append("Topic breakdown:")
        lines.extend(topic_lines)

    preview = cards[:DEFAULT_PREVIEW_LIMIT]
    for card in preview:
        lines.append(_result_line(card))

    if len(cards) > len(preview):
        lines.append(f"... {len(cards) - len(preview)} more results not shown in preview.")

    if recommended_ids:
        lines.append(f"Recommended IDs for next step: {', '.join(str(i) for i in recommended_ids)}")
        lines.append(
            "Next call: get_questions(question_ids_list=[...]) — choose detail='compact' for typical use or 'full' if you need the complete answer text."
        )

    return "\n".join(lines)


def _extract_key_points(answer_text: Any, bullet_points: Any, max_points: int = 8) -> list[str]:
    points: list[str] = []
    seen_keys: set[str] = set()

    if isinstance(bullet_points, list):
        for bullet in bullet_points:
            text = _to_ascii_text(bullet, max_len=220)
            text = re.sub(r"\s+", " ", text).strip("-:;,. ")
            key = text.lower()
            if text and key not in seen_keys:
                points.append(text)
                seen_keys.add(key)
            if len(points) >= max_points:
                return points

    text_blob = str(answer_text or "")
    for raw_line in text_blob.splitlines():
        line = _to_ascii_text(raw_line, max_len=220)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue

        lowered = line.lower()
        if lowered.startswith("one mark per"):
            continue
        if lowered.startswith("one mark"):
            continue
        if lowered.startswith("two marks"):
            continue
        if lowered.startswith("three marks"):
            continue
        if lowered.startswith("max "):
            continue
        if re.match(r"^mp\d+\b", lowered):
            continue
        if lowered.startswith("mp") and len(line) <= 6:
            continue

        line = line.strip("-:;,. ")
        if len(line) < 4:
            continue
        if line.startswith("/"):
            continue

        key = line.lower()
        if key not in seen_keys:
            points.append(line)
            seen_keys.add(key)
        if len(points) >= max_points:
            break

    return points


def _select_recommended_ids(cards: list[dict[str, Any]], limit: int) -> list[int]:
    selected: list[int] = []
    covered_topics: set[str] = set()

    for card in cards:
        card_id = card.get("id")
        if not isinstance(card_id, int):
            continue

        card_topics = [str(t) for t in (card.get("matched_topics") or [])]
        unseen = [t for t in card_topics if t not in covered_topics]
        if unseen and card_id not in selected:
            selected.append(card_id)
            covered_topics.update(unseen)
            if len(selected) >= limit:
                return selected[:limit]

    for card in cards:
        card_id = card.get("id")
        if isinstance(card_id, int) and card_id not in selected:
            selected.append(card_id)
            if len(selected) >= limit:
                break

    return selected[:limit]


def _build_questions_summary(
    questions: list[dict[str, Any]],
    missing_ids: list[int],
    detail: str,
) -> str:
    lines = [f"Fetched {len(questions)} questions (detail={detail})."]
    if missing_ids:
        lines.append(f"Missing IDs: {', '.join(str(i) for i in missing_ids)}")

    preview = questions[:10]
    for q in preview:
        paper = q.get("paper") or {}
        session_short = _short_session(paper.get("session_name"))
        label_parts: list[str] = []
        if paper.get("paper_number") is not None:
            label_parts.append(f"P{paper.get('paper_number')}")
        if session_short and paper.get("year") is not None:
            label_parts.append(f"{session_short}{paper.get('year')}")
        elif paper.get("year") is not None:
            label_parts.append(str(paper.get("year")))
        if paper.get("variant") is not None:
            label_parts.append(f"v{paper.get('variant')}")
        paper_label = " ".join(label_parts) if label_parts else "Unknown paper"
        lines.append(
            f"ID:{q.get('id')} | {paper_label} | Q{q.get('question_number')} | {q.get('marks', '?')}m"
        )
        lines.append(f"Q: {_to_ascii_text(q.get('question_text'), max_len=180)}")

        key_points = q.get("key_points") or []
        if key_points:
            lines.append("Key points: " + "; ".join(str(p) for p in key_points[:5]))

    if len(questions) > len(preview):
        lines.append(f"... {len(questions) - len(preview)} more questions available in structured output.")

    return "\n".join(lines)


@mcp.tool(
    title="Search Questions",
    tags={"search", "core"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def search_questions(
    query: str,
    subject: Optional[str] = DEFAULT_SUBJECT,
    paper: Optional[int] = None,
    year: Optional[int] = None,
    session: Optional[str] = None,
    chapter: Optional[int] = None,
    mode: str = "hybrid",
    limit: int = 20,
    offset: int = 0,
    expand: bool = True,
) -> ToolResult:
    """Search past-paper questions with optional filters.

    Returns ranked questions with compact metadata. Each result includes:
    - Paper identification (paper, year, session, variant, paper_label)
    - Question number, marks, relevance score
    - Whether it's image-based (if so, question_image_url and ms_image_url are provided)
    - topic_signal: exam frequency and importance summary
    - question_url: link to full question page

    NEXT STEP: Call get_questions(question_ids_list=[...]) with the recommended_ids
    to get full question text, mark scheme key points, and images.
    """
    if not _validate_mode(mode):
        raise ToolError(
            "INVALID_MODE: mode must be one of 'hybrid', 'keyword', or 'semantic'."
        )

    capped_limit = max(1, min(limit, MAX_SEARCH_LIMIT))
    safe_offset = max(0, offset)
    normalized_session = _normalize_session_filter(session)

    original_query = query
    effective_query, was_corrected = _spell_correct(query)

    params: dict[str, Any] = {
        "q": effective_query,
        "mode": mode,
        "limit": capped_limit,
        "offset": safe_offset,
        "expand": expand,
        "has_answer": True,
    }
    if subject:
        params["subject"] = subject
    if paper is not None:
        params["paper"] = paper
    if year is not None:
        params["year"] = year
    if normalized_session:
        params["session"] = normalized_session
    if chapter is not None:
        params["chapter"] = chapter

    try:
        data = _api_get("/search", params)
    except Exception as exc:
        logger.error("search_questions failed: %s", exc, exc_info=True)
        error_payload = _error_from_exception(exc, "/search")
        message = error_payload.get("error", {}).get("message", "Search failed.")
        raise ToolError(message)

    raw_results = data.get("results", []) if isinstance(data, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []

    visible_results = raw_results[:capped_limit]
    cards = [_result_item(r, i + 1) for i, r in enumerate(visible_results) if isinstance(r, dict)]
    all_result_ids = [r["id"] for r in cards if isinstance(r.get("id"), int)]
    recommended_ids = all_result_ids[: min(len(all_result_ids), MAX_RECOMMENDED_IDS)]

    payload = {
        "ok": True,
        "query": original_query,
        "effective_query": effective_query,
        "was_corrected": was_corrected,
        "filters": {
            "subject": subject,
            "paper": paper,
            "year": year,
            "session": normalized_session,
            "chapter": chapter,
            "mode": mode,
            "expand": expand,
            "limit": capped_limit,
            "offset": safe_offset,
        },
        "meta": {
            "total": data.get("total", len(raw_results)) if isinstance(data, dict) else len(raw_results),
            "returned": len(cards),
            "query_time_ms": data.get("query_time_ms") if isinstance(data, dict) else None,
        },
        "results": cards,
        "recommended_ids": recommended_ids,
        "next_step": {
            "tool": "get_questions",
            "question_ids_list": recommended_ids[:15],
            "example": "get_questions(question_ids_list=[1615,1684])",
        },
    }

    query_note = None
    if was_corrected and effective_query != original_query:
        query_note = f"Query corrected from '{original_query}' to '{effective_query}'."

    summary_text = _build_search_summary(
        title=f"Search results for '{original_query}'",
        query_note=query_note,
        total=payload["meta"]["total"],
        returned=payload["meta"]["returned"],
        cards=cards,
        recommended_ids=recommended_ids[:15],
    )

    return ToolResult(content=summary_text, structured_content=payload)


@mcp.tool(
    title="Search Multiple Topics",
    tags={"search", "core"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def search_multi(
    topics: str = "",
    topics_list: Optional[list[str]] = None,
    subject: Optional[str] = DEFAULT_SUBJECT,
    paper: Optional[int] = None,
    year: Optional[int] = None,
    session: Optional[str] = None,
    chapter: Optional[int] = None,
    mode: str = "hybrid",
    limit_per_topic: int = 10,
    max_results: int = 40,
    expand: bool = True,
) -> ToolResult:
    """Search multiple topics and deduplicate by question ID.

    Accepts either `topics` (comma-separated string) or `topics_list`.
    """
    if not _validate_mode(mode):
        raise ToolError(
            "INVALID_MODE: mode must be one of 'hybrid', 'keyword', or 'semantic'."
        )

    topic_list = _parse_topics_input(topics, topics_list)

    if not topic_list:
        raise ToolError("NO_TOPICS: Provide one or more topics via topics or topics_list.")
    if len(topic_list) > MAX_TOPICS:
        raise ToolError(f"TOO_MANY_TOPICS: Maximum {MAX_TOPICS} topics per request.")

    capped_limit_per_topic = max(1, min(limit_per_topic, 30))
    capped_max_results = max(1, min(max_results, 100))
    normalized_session = _normalize_session_filter(session)

    all_results: dict[int, dict[str, Any]] = {}
    topic_breakdown: list[dict[str, Any]] = []
    effective_topics: list[str] = []
    effective_seen: set[str] = set()

    for topic in topic_list:
        corrected_topic, was_corrected = _spell_correct(topic)
        normalized_effective = _normalize_topic_key(corrected_topic)

        if normalized_effective in effective_seen:
            topic_breakdown.append(
                {
                    "topic": topic,
                    "effective_topic": corrected_topic,
                    "was_corrected": was_corrected,
                    "api_returned": 0,
                    "new_unique_results": 0,
                    "skipped": "duplicate_effective_topic",
                }
            )
            continue

        effective_seen.add(normalized_effective)
        effective_topics.append(corrected_topic)

        params: dict[str, Any] = {
            "q": corrected_topic,
            "mode": mode,
            "limit": capped_limit_per_topic,
            "expand": expand,
            "has_answer": True,
        }
        if subject:
            params["subject"] = subject
        if paper is not None:
            params["paper"] = paper
        if year is not None:
            params["year"] = year
        if normalized_session:
            params["session"] = normalized_session
        if chapter is not None:
            params["chapter"] = chapter

        try:
            data = _api_get("/search", params)
            result_rows = data.get("results", []) if isinstance(data, dict) else []
            if not isinstance(result_rows, list):
                result_rows = []

            unique_added = 0
            for row in result_rows:
                if not isinstance(row, dict):
                    continue
                row_id = row.get("id")
                if not isinstance(row_id, int):
                    continue
                if row_id not in all_results:
                    all_results[row_id] = dict(row)
                    all_results[row_id]["_matched_topics"] = {corrected_topic}
                    unique_added += 1
                else:
                    all_results[row_id].setdefault("_matched_topics", set()).add(corrected_topic)

            topic_breakdown.append(
                {
                    "topic": topic,
                    "effective_topic": corrected_topic,
                    "was_corrected": was_corrected,
                    "api_returned": len(result_rows),
                    "new_unique_results": unique_added,
                }
            )
        except Exception as exc:
            logger.warning("Topic search failed for '%s': %s", topic, exc)
            topic_breakdown.append(
                {
                    "topic": topic,
                    "effective_topic": corrected_topic,
                    "was_corrected": was_corrected,
                    "error": str(exc),
                }
            )

    merged = list(all_results.values())
    merged.sort(key=lambda x: float(x.get("relevance_score") or 0.0), reverse=True)
    visible = merged[:capped_max_results]

    cards: list[dict[str, Any]] = []
    for i, row in enumerate(visible, 1):
        matched_topics = sorted(list(row.get("_matched_topics", [])))
        cards.append(_result_item(row, i, matched_topics=matched_topics))

    all_result_ids = [r["id"] for r in cards if isinstance(r.get("id"), int)]
    recommended_ids = _select_recommended_ids(
        cards,
        limit=min(MAX_RECOMMENDED_IDS, len(all_result_ids)),
    )

    payload = {
        "ok": True,
        "topics": topic_list,
        "effective_topics": effective_topics,
        "filters": {
            "subject": subject,
            "paper": paper,
            "year": year,
            "session": normalized_session,
            "chapter": chapter,
            "mode": mode,
            "expand": expand,
            "limit_per_topic": capped_limit_per_topic,
            "max_results": capped_max_results,
        },
        "meta": {
            "topics_searched": len(topic_list),
            "unique_results_total": len(merged),
            "returned": len(cards),
        },
        "topic_breakdown": topic_breakdown,
        "results": cards,
        "recommended_ids": recommended_ids,
        "next_step": {
            "tool": "get_questions",
            "question_ids_list": recommended_ids,
            "example": "get_questions(question_ids_list=[1615,1684])",
        },
    }

    topic_lines: list[str] = []
    for row in topic_breakdown:
        if row.get("skipped"):
            topic_lines.append(
                f"- '{row.get('topic')}': skipped (duplicate of '{row.get('effective_topic')}')."
            )
            continue
        if row.get("error"):
            topic_lines.append(f"- '{row.get('topic')}': error ({row.get('error')}).")
            continue
        topic_lines.append(
            f"- '{row.get('topic')}': {row.get('api_returned', 0)} found, "
            f"{row.get('new_unique_results', 0)} unique"
        )

    summary_text = _build_search_summary(
        title=f"Multi-topic search across {len(topic_list)} topics",
        query_note=None,
        total=payload["meta"]["unique_results_total"],
        returned=payload["meta"]["returned"],
        cards=cards,
        topic_lines=topic_lines,
        recommended_ids=recommended_ids,
    )

    return ToolResult(content=summary_text, structured_content=payload)


@mcp.tool(
    title="Get Questions By IDs",
    tags={"search", "core"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def get_questions(
    question_ids: str = "",
    question_ids_list: Optional[list[int]] = None,
    detail: str = "compact",
    max_key_points: int = 8,
    include_images: bool = False,
    include_ocr: bool = False,
) -> ToolResult:
    """Fetch full question details and mark schemes for selected IDs.

    Accepts either `question_ids` (comma-separated) or `question_ids_list`.
    Default detail is `compact` for LLM-friendly responses.

    Returns for each question:
    - Full question text and context
    - Key mark scheme points (authoritative answers)
    - Paper identification
    - question_url: link to full question page
    - Image URLs (question_image_url, ms_image_url) for image-based questions

    Use include_images=True to get image URLs for ALL questions (not just image-based).
    Use include_ocr=True to get OCR text extracted from question images.
    """
    if detail not in {"compact", "full"}:
        raise ToolError("INVALID_DETAIL: detail must be 'compact' or 'full'.")

    capped_points = max(1, min(max_key_points, 20))

    try:
        unique_ids = _parse_question_ids_input(question_ids, question_ids_list)
    except (TypeError, ValueError):
        raise ToolError(
            "INVALID_IDS: Use integers only, e.g. question_ids='552,799' or question_ids_list=[552,799]."
        )

    if not unique_ids:
        raise ToolError("NO_IDS: No question IDs were provided.")
    if len(unique_ids) > MAX_BATCH_IDS:
        raise ToolError(
            f"TOO_MANY_IDS: Maximum {MAX_BATCH_IDS} IDs per request (received {len(unique_ids)})."
        )

    try:
        rows = _api_get("/questions/batch", {"ids": ",".join(str(x) for x in unique_ids)})
    except Exception as exc:
        logger.error("get_questions failed: %s", exc, exc_info=True)
        error_payload = _error_from_exception(exc, "/questions/batch")
        message = error_payload.get("error", {}).get("message", "Question fetch failed.")
        raise ToolError(message)

    if not isinstance(rows, list):
        rows = []

    rows_by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
    ordered_rows = [rows_by_id[i] for i in unique_ids if i in rows_by_id]
    missing_ids = [i for i in unique_ids if i not in rows_by_id]

    questions: list[dict[str, Any]] = []
    for row in ordered_rows:
        paper = row.get("paper") or {}
        bullets = row.get("answer_bullet_points")
        if not isinstance(bullets, list):
            bullets = []

        key_points = _extract_key_points(
            answer_text=row.get("answer_text"),
            bullet_points=bullets,
            max_points=capped_points,
        )

        base_payload = {
            "id": row.get("id"),
            "question_number": row.get("question_number"),
            "question_text": row.get("question_text"),
            "question_context": row.get("question_context"),
            "marks": row.get("marks"),
            "topic": row.get("topic"),
            "chapter_name": row.get("chapter_name"),
            "subtopic": row.get("subtopic"),
            "paper": {
                "paper_number": paper.get("paper_number"),
                "year": paper.get("year"),
                "session": _short_session(paper.get("session_name")),
                "variant": paper.get("variant"),
            },
            "is_image_based": bool(row.get("is_image_based")),
            "question_url": f"{QUESTION_URL_BASE}/{row.get('id')}" if row.get("id") else None,
            "key_points": key_points,
        }

        # Always include image URLs for image-based questions
        if bool(row.get("is_image_based")):
            base_payload["question_image_url"] = _to_image_url(row.get("image_path"))
            base_payload["ms_image_url"] = _to_image_url(row.get("ms_image_path"))

        if detail == "compact":
            compact_payload = dict(base_payload)
            compact_payload["question_text"] = _to_ascii_text(row.get("question_text"), max_len=550)
            compact_payload["question_context"] = _to_ascii_text(
                row.get("question_context"),
                max_len=280,
            )
            compact_payload["answer_preview"] = _to_ascii_text(
                row.get("answer_text"),
                max_len=320,
            )

            if include_images and not bool(row.get("is_image_based")):
                compact_payload["question_image_url"] = _to_image_url(row.get("image_path"))
                compact_payload["ms_image_url"] = _to_image_url(row.get("ms_image_path"))

            if include_ocr:
                compact_payload["ocr_text"] = _to_ascii_text(row.get("ocr_text"), max_len=500)

            questions.append(compact_payload)
            continue

        full_payload = {
            **base_payload,
            "answer_text": row.get("answer_text") or "",
            "answer_bullet_points": bullets,
            "ocr_text": row.get("ocr_text") if include_ocr else None,
        }
        if include_images and not bool(row.get("is_image_based")):
            full_payload["question_image_url"] = _to_image_url(row.get("image_path"))
            full_payload["ms_image_url"] = _to_image_url(row.get("ms_image_path"))

        questions.append(full_payload)

    payload = {
        "ok": True,
        "requested_ids": unique_ids,
        "meta": {
            "requested": len(unique_ids),
            "found": len(questions),
            "missing": len(missing_ids),
            "detail": detail,
        },
        "missing_ids": missing_ids,
        "questions": questions,
    }

    summary_text = _build_questions_summary(
        questions=questions,
        missing_ids=missing_ids,
        detail=detail,
    )

    return ToolResult(content=summary_text, structured_content=payload)


@mcp.tool(title="Get Database Stats", tags={"search", "core"})
def get_stats() -> dict[str, Any]:
    """Get overall API statistics and service health."""
    try:
        stats = _api_get("/stats")
    except Exception as exc:
        logger.error("get_stats failed: %s", exc, exc_info=True)
        return _error_from_exception(exc, "/stats")

    health_payload: Optional[dict[str, Any]] = None
    try:
        health_data = _api_get("/health")
        if isinstance(health_data, dict):
            health_payload = health_data
    except Exception:
        health_payload = None

    years = stats.get("years", []) if isinstance(stats, dict) else []
    if not isinstance(years, list):
        years = []

    return {
        "ok": True,
        "api_base": API_BASE,
        "totals": {
            "total_questions": stats.get("total_questions", 0) if isinstance(stats, dict) else 0,
            "papers_indexed": stats.get("papers_indexed", 0) if isinstance(stats, dict) else 0,
            "total_marks": stats.get("total_marks", 0) if isinstance(stats, dict) else 0,
        },
        "years": years,
        "health": health_payload,
    }


@mcp.resource("cie9618://stats", title="Search Stats")
def resource_stats() -> str:
    """Current database statistics including total questions, papers, and marks."""
    result = get_stats()
    if not result.get("ok"):
        error = result.get("error") or {}
        return f"Error: {error.get('message', 'Unknown stats error')}"

    totals = result.get("totals") or {}
    years = result.get("years") or []
    health = result.get("health") or {}

    lines = [
        "=== Search CAIE Database Statistics ===",
        "",
        f"API Base: {result.get('api_base')}",
        f"Total Questions: {totals.get('total_questions', 0)}",
        f"Papers Indexed: {totals.get('papers_indexed', 0)}",
        f"Total Marks: {totals.get('total_marks', 0)}",
        f"Years: {', '.join(str(y) for y in years)}",
    ]

    if health:
        lines.append(f"Health: {health.get('status', 'unknown')}")
        lines.append(f"Database OK: {health.get('database_ok')}")
        lines.append(f"Index OK: {health.get('index_ok')}")
        lines.append(f"AI Enabled: {health.get('ai_enabled')}")

    return "\n".join(lines)


@mcp.resource("cie9618://papers", title="Paper Catalog")
def resource_papers() -> str:
    """Full catalog of indexed papers with metadata."""
    try:
        papers = _api_get("/papers")
        if not isinstance(papers, list):
            papers = []

        lines = [f"=== Paper Catalog ({len(papers)} papers) ===", ""]
        for row in papers:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"S{row.get('subject_code', '?')} P{row.get('paper_number', '?')} "
                f"v{row.get('variant', '?')} | {_short_session(row.get('session_name'))} "
                f"{row.get('year', '?')} | {row.get('question_count', 0)} questions"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("resource_papers failed: %s", exc, exc_info=True)
        return f"Error loading papers: {str(exc)}"


@mcp.prompt(title="Exam Study Helper")
def exam_study_helper(topic: str, subject: str = DEFAULT_SUBJECT) -> str:
    """Create a structured study plan for a specific topic using MCP tools."""
    return (
        f"Help me study '{topic}' for CAIE subject {subject}.\n\n"
        "Workflow:\n"
        f"1) Call search_questions(query='{topic}', subject='{subject}', expand=True).\n"
        "2) Read recommended_ids and call get_questions(question_ids_list=[...], detail='compact').\n"
        "3) Explain recurring exam patterns and command words.\n"
        "4) Build a revision checklist and suggest practice order.\n\n"
        "When citing evidence, include subject, paper, year, session, variant, question number, and ID."
    )


@mcp.prompt(title="Topic Deep Dive")
def topic_deep_dive(topics: str, subject: str = DEFAULT_SUBJECT) -> str:
    """Analyze one or more topics across exam sessions."""
    return (
        f"Analyze how these topics are examined for CAIE subject {subject}: {topics}.\n\n"
        "Workflow:\n"
        f"1) Call search_multi(topics='{topics}', subject='{subject}', expand=True).\n"
        "2) Use recommended_ids and call get_questions(question_ids_list=[...], detail='compact').\n"
        "3) Summarize paper distribution, mark patterns, and recurring wording.\n"
        "4) Give exam-focused advice based on the evidence."
    )




# ── Enhanced Search Tools (API-proxied) ──────────────────────────────────────


def _clean_examiner_chunk(text: str) -> str:
    """Remove generic ER boilerplate preamble, keep only topic-specific commentary."""
    if not text:
        return ""

    # Common boilerplate sections to skip past
    skip_markers = [
        "Comments on specific questions",
        "Comment on specific questions",
    ]
    for marker in skip_markers:
        idx = text.find(marker)
        if idx >= 0:
            # Start from after the marker line
            after = text[idx + len(marker):]
            stripped = after.lstrip("\n\r ")
            if stripped:
                text = stripped
                break

    # Strip remaining generic preamble phrases that add no value
    boilerplate_starts = [
        "Key messages\n",
        "General comments\n",
        "Candidates need to demonstrate a detailed study",
        "Candidates are advised to answer each question",
        "Candidates must always make sure",
        "Candidates are further advised to make use",
    ]
    for bp in boilerplate_starts:
        if text.startswith(bp):
            # Find next paragraph
            next_para = text.find("\n\n", len(bp))
            if next_para > 0:
                text = text[next_para:].lstrip("\n\r ")

    # Clean up copyright lines
    text = re.sub(r"©\s*\d{4}\s*$", "", text, flags=re.MULTILINE).strip()
    return text


def _extract_educational_content(content: str, max_chars: int = 800) -> str:
    """Extract educational content from a web page scrape, stripping nav/ads/chrome."""
    if not content:
        return ""

    # Remove common navigation/menu patterns
    nav_patterns = [
        r"(?i)(?:^|\n)\s*\*\s*(?:Courses|Tutorials|Interview Prep|Sign In|DSA Python|"
        r"Interview Corner|Puzzles|Aptitude|System Design|Must Do|Quizzes|"
        r"Interview Questions|DSA Tutorial|Data Types|Examples|Practice|"
        r"Data Science|NumPy|Pandas|Django|Flask|Projects|Advanced DSA)\s*(?:\n|$)",
        r"(?i)Open In App\s*\n",
        r"(?i)Jump to content\s*\n",
        r"\*\*\s*\n",  # Standalone bold markers
        r"(?i)^\s*\d+\s+languages?\s*$",  # Language count lines
        r"(?i)^\s*\*\s*(?:Español|فارسی|한국어|Italiano|עברית)\s*$",  # Other language links
        r"(?i)Article Tags:.*$",
        r"(?i)Comment\s*$",
        r"(?i)Improve\s*$",
        r"(?i)\d+\s*Likes?\s*$",
        r"(?i)Like\s*$",
        r"(?i)Report\s*$",
        r"(?i)Suggest changes\s*$",
        r"(?i)Last Updated\s*:\s*\d+\s+\w+,?\s*\d{4}",
        r"(?i)geeksforgeeks\s*\n",
    ]
    cleaned = content
    for pattern in nav_patterns:
        cleaned = re.sub(pattern, "\n", cleaned, flags=re.MULTILINE)

    # Collapse multiple blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # Truncate to max_chars at a sentence boundary
    if len(cleaned) > max_chars:
        # Try to cut at sentence boundary
        truncated = cleaned[:max_chars]
        last_period = truncated.rfind(".")
        last_newline = truncated.rfind("\n")
        cut_at = max(last_period, last_newline)
        if cut_at > max_chars * 0.5:
            cleaned = truncated[:cut_at + 1].rstrip()
        else:
            cleaned = truncated.rstrip() + "..."

    return cleaned


def _clean_image_title(title: str) -> str:
    """Clean image title by removing common prefixes/suffixes."""
    if not title:
        return ""
    # Remove "File:" prefix common in Wikipedia
    title = re.sub(r"^File:\s*", "", title)
    # Remove file extensions
    title = re.sub(r"\.(svg|png|jpg|jpeg|gif|webp)\s*", " ", title, flags=re.IGNORECASE)
    # Remove " - Wikipedia" suffix
    title = re.sub(r"\s*-\s*Wikipedia\s*$", "", title)
    # Remove " - GeeksforGeeks" suffix
    title = re.sub(r"\s*-\s*GeeksforGeeks\s*$", "", title)
    return title.strip()


@mcp.tool(
    title="Search Examiner Reports",
    tags={"search", "enhanced"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def search_examiner_reports(
    query: str,
    subject: Optional[str] = DEFAULT_SUBJECT,
    paper: Optional[int] = None,
    year: Optional[int] = None,
    limit: int = 5,
) -> ToolResult:
    """Search Cambridge examiner report commentary for insights on how a topic is examined.

    Returns examiner observations including:
    - What examiners expect in answers on this topic
    - Common mistakes candidates make
    - Tips for how to structure answers to gain full marks

    Use AFTER searching questions to understand examiner expectations on the same topic.
    Examiner reports are the most authoritative source for HOW to answer, not WHAT to answer.
    """
    params: dict[str, Any] = {"q": query, "limit": max(1, min(limit, 20))}
    if subject:
        params["subject"] = subject
    if paper is not None:
        params["paper"] = paper
    if year is not None:
        params["year"] = year

    try:
        data = _api_get("/search/examiner-reports", params)
    except Exception as exc:
        logger.error("search_examiner_reports failed: %s", exc)
        error_payload = _error_from_exception(exc, "/search/examiner-reports")
        raise ToolError(error_payload.get("error", {}).get("message", "ER search failed."))

    raw_chunks = data.get("results", []) if isinstance(data, dict) else []
    total = data.get("total", 0) if isinstance(data, dict) else 0

    # De-duplicate near-identical chunks and strip boilerplate
    seen_hashes: set[int] = set()
    curated_chunks: list[dict[str, Any]] = []
    for chunk in raw_chunks:
        if not isinstance(chunk, dict):
            continue
        cleaned_text = _clean_examiner_chunk(chunk.get("chunk_text", ""))
        if not cleaned_text or len(cleaned_text) < 20:
            continue
        text_hash = hash(cleaned_text[:200])
        if text_hash in seen_hashes:
            continue
        seen_hashes.add(text_hash)
        curated_chunks.append({
            "year": chunk.get("year"),
            "session": chunk.get("session_name"),
            "paper": chunk.get("paper_number"),
            "commentary": cleaned_text,
            "relevance_score": chunk.get("relevance_score"),
        })

    lines = [f"Examiner Report Search: {total} total for '{query}', showing {len(curated_chunks)} unique."]
    for i, chunk in enumerate(curated_chunks, 1):
        year_str = chunk.get("year", "?")
        session = chunk.get("session", "?")
        paper_num = chunk.get("paper")
        paper_label = f"Paper {paper_num}" if paper_num else "General"
        text = _clean_text(chunk.get("commentary", ""), max_len=350)
        lines.append(f"[{i}] {year_str} {session} | {paper_label}")
        lines.append(f"    {text}")

    if not curated_chunks:
        lines.append("No examiner report data found for this query.")

    payload = {
        "ok": True, "query": query, "total": total,
        "returned": len(curated_chunks),
        "results": curated_chunks,
    }
    return ToolResult(content="\n".join(lines), structured_content=payload)


@mcp.tool(
    title="Search Web Context",
    tags={"search", "enhanced"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def search_web_context(
    query: str,
    subject: Optional[str] = DEFAULT_SUBJECT,
    num_results: int = 5,
) -> ToolResult:
    """Get educational web content to supplement CAIE exam explanations.

    Returns summarized content from trusted CS education sites.
    Use when the student needs conceptual explanations beyond what mark schemes provide.

    Web content is supplementary — always prioritize official CAIE mark scheme points first.
    Returns: source title, URL, domain, and key educational content (max 800 chars per source).
    """
    params: dict[str, Any] = {
        "q": query,
        "num_results": max(1, min(num_results, 10)),
    }
    if subject:
        params["subject"] = subject

    try:
        data = _api_get("/search/web-context", params)
    except Exception as exc:
        logger.error("search_web_context failed: %s", exc)
        error_payload = _error_from_exception(exc, "/search/web-context")
        raise ToolError(error_payload.get("error", {}).get("message", "Web search failed."))

    results = data.get("results", []) if isinstance(data, dict) else []

    # De-duplicate by domain — keep only the most relevant per domain
    seen_domains: set[str] = set()
    curated_results: list[dict[str, Any]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        domain = r.get("domain", "")
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        key_content = _extract_educational_content(r.get("content", ""), max_chars=800)
        curated_results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "domain": domain,
            "key_content": key_content,
        })

    # Build concise text summary
    content_lines = [f"Web context for '{query}' from {len(curated_results)} sources:"]
    for i, r in enumerate(curated_results, 1):
        content_lines.append(f"\n[{i}] {r['title']} ({r['domain']})")
        content_lines.append(r["key_content"])

    if not curated_results:
        content_lines.append("No web content found for this query.")

    payload = {
        "ok": True, "query": query,
        "returned": len(curated_results),
        "results": curated_results,
    }
    return ToolResult(content="\n".join(content_lines), structured_content=payload)


@mcp.tool(
    title="Search Topic Images",
    tags={"search", "enhanced"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def search_topic_images(
    query: str,
    subject: Optional[str] = DEFAULT_SUBJECT,
    num_images: int = 3,
) -> ToolResult:
    """Find educational diagrams and illustrations for a CAIE topic.

    Returns external web images (Wikipedia, GFG diagrams) — NOT past paper images.
    For actual question/mark scheme images, use get_questions with include_images=True.

    Use this when:
    - Student needs a visual explanation of a concept (e.g., "show me a binary tree diagram")
    - Adding supplementary illustrations beyond what exam papers show

    Returns: image URL, title, source domain.
    """
    params: dict[str, Any] = {
        "q": query,
        "num_images": max(1, min(num_images, 10)),
    }
    if subject:
        params["subject"] = subject

    try:
        data = _api_get("/search/images", params)
    except Exception as exc:
        logger.error("search_topic_images failed: %s", exc)
        error_payload = _error_from_exception(exc, "/search/images")
        raise ToolError(error_payload.get("error", {}).get("message", "Image search failed."))

    raw_images = data.get("images", []) if isinstance(data, dict) else []

    # Clean up image data and filter problematic URLs
    curated_images: list[dict[str, Any]] = []
    for img in raw_images:
        if not isinstance(img, dict):
            continue
            
        url = img.get("url", "")
        # Skip Wikipedia thumbnails as they often throw 429 or have CORS issues
        if "wikimedia.org/wikipedia/commons/thumb" in url or "wikipedia.org" in url:
            continue
        # Skip SVGs as they don't render well in some markdown/chat clients
        if url.lower().endswith(".svg"):
            continue
            
        curated_images.append({
            "url": url,
            "title": _clean_image_title(img.get("title", "")),
            "source": img.get("source_domain", ""),
        })

        if len(curated_images) >= num_images:
            break

    # Build concise text summary
    content_lines = [f"Found {len(curated_images)} images for '{query}':"]
    for i, img in enumerate(curated_images, 1):
        content_lines.append(f"[{i}] {img['title']} — {img['source']}")
        content_lines.append(f"    URL: {img['url']}")

    if not curated_images:
        content_lines.append("No images found for this query.")

    payload = {
        "ok": True, "query": query,
        "returned": len(curated_images),
        "images": curated_images,
    }
    return ToolResult(content="\n".join(content_lines), structured_content=payload)


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport in {"http", "streamable-http", "sse"}:
        host = os.getenv("MCP_HOST", "127.0.0.1")
        port = int(os.getenv("MCP_PORT", "8000"))
        path = os.getenv("MCP_PATH", "/mcp")
        if transport in {"http", "streamable-http"}:
            mcp.run(transport=transport, host=host, port=port, path=path)
        else:
            mcp.run(transport=transport, host=host, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
