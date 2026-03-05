"""MCP server for Search CAIE past-paper search.

This server is standalone and proxies to the deployed API.

Default backend: https://api.searchcaie.qzz.io/api
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


API_BASE = os.getenv("MCP_API_BASE", "https://api.searchcaie.qzz.io/api").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("MCP_REQUEST_TIMEOUT", "30"))
DEFAULT_SUBJECT = os.getenv("MCP_DEFAULT_SUBJECT", "9618")
MAX_SEARCH_LIMIT = 50
MAX_BATCH_IDS = 50
MAX_TOPICS = 12
DEFAULT_PREVIEW_LIMIT = 8
MAX_RECOMMENDED_IDS = 20
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


mcp = FastMCP(
    "searchcaie-search",
    instructions=(
        "You are connected to Search CAIE past-paper search tools.\n\n"
        "Recommended workflow:\n"
        "1) Use search_questions for one query with filters.\n"
        "2) Use search_multi for comma-separated multi-topic search.\n"
        "3) Use get_questions for full question/mark-scheme details by IDs.\n"
        "4) Cite subject, paper, year, session, variant, question number, and ID.\n\n"
        "Tool outputs are structured for reliability; prefer those fields over guessing."
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

    item = {
        "rank": index,
        "id": raw.get("id"),
        "subject": paper.get("subject_code"),
        "paper": paper_number,
        "year": year,
        "session": paper.get("session_name"),
        "session_short": session_short,
        "variant": variant,
        "paper_label": " ".join(paper_label_parts),
        "question_number": raw.get("question_number"),
        "marks": raw.get("marks"),
        "relevance_score": raw.get("relevance_score"),
        "relevance_tier": raw.get("relevance_tier"),
        "duplicate_group_size": raw.get("duplicate_group_size"),
        "snippet": _clean_text(raw.get("question_text"), max_len=200),
    }
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
    return (
        f"[{card.get('rank')}] ID:{card.get('id')} | {card.get('paper_label', '').strip()} | "
        f"Q{question_no} | {marks_text} | score {score_text} | {card.get('snippet', '')}"
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
            "Next call: get_questions(question_ids_list=[...], detail='compact')"
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

    preview = questions[:4]
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

    Returns compact ranked cards and structured IDs for follow-up retrieval.
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
            "has_answer": True,
            "limit": capped_limit,
            "offset": safe_offset,
        },
        "meta": {
            "api_total": data.get("total", len(raw_results)) if isinstance(data, dict) else len(raw_results),
            "api_returned": len(raw_results),
            "returned": len(cards),
            "query_time_ms": data.get("query_time_ms") if isinstance(data, dict) else None,
            "truncated": len(raw_results) > len(cards),
            "api_base": API_BASE,
        },
        "results": cards,
        "all_result_ids": all_result_ids,
        "recommended_ids": recommended_ids,
        "next_step": {
            "tool": "get_questions",
            "question_ids": recommended_ids[:15],
            "question_ids_list": recommended_ids[:15],
            "example": "get_questions(question_ids_list=[1615,1684], detail='compact')",
        },
    }

    query_note = None
    if was_corrected and effective_query != original_query:
        query_note = f"Query corrected from '{original_query}' to '{effective_query}'."

    summary_text = _build_search_summary(
        title=f"Search results for '{original_query}'",
        query_note=query_note,
        total=payload["meta"]["api_total"],
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
            "has_answer": True,
            "limit_per_topic": capped_limit_per_topic,
            "max_results": capped_max_results,
        },
        "meta": {
            "topics_searched": len(topic_list),
            "unique_results_total": len(merged),
            "returned": len(cards),
            "truncated": len(merged) > len(cards),
            "api_base": API_BASE,
        },
        "topic_breakdown": topic_breakdown,
        "results": cards,
        "all_result_ids": all_result_ids,
        "recommended_ids": recommended_ids,
        "next_step": {
            "tool": "get_questions",
            "question_ids": recommended_ids,
            "question_ids_list": recommended_ids,
            "example": "get_questions(question_ids_list=[1615,1684], detail='compact')",
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
    """Fetch question details and mark schemes for selected IDs.

    Accepts either `question_ids` (comma-separated) or `question_ids_list`.
    Default detail mode is `compact` for LLM-friendly responses.
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
            "chapter_id": row.get("chapter_id"),
            "chapter_name": row.get("chapter_name"),
            "subtopic": row.get("subtopic"),
            "paper": {
                "subject_code": paper.get("subject_code"),
                "paper_number": paper.get("paper_number"),
                "year": paper.get("year"),
                "session_name": paper.get("session_name"),
                "variant": paper.get("variant"),
            },
            "is_image_based": bool(row.get("is_image_based")),
            "key_points": key_points,
        }

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

            if include_images:
                compact_payload["image_path"] = row.get("image_path")
                compact_payload["ms_image_path"] = row.get("ms_image_path")

            if include_ocr:
                compact_payload["ocr_text"] = _to_ascii_text(row.get("ocr_text"), max_len=500)

            questions.append(compact_payload)
            continue

        questions.append(
            {
                **base_payload,
                "answer_text": row.get("answer_text") or "",
                "answer_bullet_points": bullets,
                "image_path": row.get("image_path") if include_images else None,
                "ms_image_path": row.get("ms_image_path") if include_images else None,
                "ocr_text": row.get("ocr_text") if include_ocr else None,
            }
        )

    payload = {
        "ok": True,
        "requested_ids": unique_ids,
        "meta": {
            "requested": len(unique_ids),
            "found": len(questions),
            "missing": len(missing_ids),
            "api_base": API_BASE,
            "detail": detail,
            "include_images": include_images,
            "include_ocr": include_ocr,
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
) -> str:
    """Search examiner report commentary for insights on a topic.

    Returns relevant text chunks from Cambridge examiner reports that reveal:
    - What examiners expect from students on a topic
    - Common mistakes candidates make
    - How to properly approach and answer questions on this topic
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

    chunks = data.get("results", []) if isinstance(data, dict) else []
    total = data.get("total", 0) if isinstance(data, dict) else 0

    lines = [f"Examiner Report Search: {total} chunks found for '{query}', showing {len(chunks)}."]
    for i, chunk in enumerate(chunks, 1):
        year_str = chunk.get("year", "?")
        session = chunk.get("session_name", "?")
        paper_num = chunk.get("paper_number")
        paper_label = f"Paper {paper_num}" if paper_num else "General"
        text = _clean_text(chunk.get("chunk_text", ""), max_len=300)
        lines.append(f"[{i}] {year_str} {session} | {paper_label}")
        lines.append(f"    {text}")

    if not chunks:
        lines.append("No examiner report data found for this query.")

    payload = {"ok": True, "query": query, "total": total, "returned": len(chunks), "results": chunks}
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
) -> str:
    """Get educational web content on a topic to supplement exam questions.

    Returns curated explanatory text from trusted education sites.
    Use this to understand concepts deeply before explaining to students.
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

    formatted = data.get("formatted", "") if isinstance(data, dict) else ""
    results = data.get("results", []) if isinstance(data, dict) else []

    payload = {
        "ok": True, "query": query, "returned": len(results),
        "results": [{"title": r.get("title"), "url": r.get("url"), "domain": r.get("domain")} for r in results],
    }

    return ToolResult(content=formatted or "No web content found.", structured_content=payload)


@mcp.tool(
    title="Search Topic Images",
    tags={"search", "enhanced"},
    annotations={"readOnlyHint": True, "idempotentHint": True},
)
def search_topic_images(
    query: str,
    subject: Optional[str] = DEFAULT_SUBJECT,
    num_images: int = 3,
) -> str:
    """Find educational diagrams and illustrations for a topic.

    Returns image URLs with descriptive labels from trusted educational sources.
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

    formatted = data.get("formatted", "") if isinstance(data, dict) else ""
    images = data.get("images", []) if isinstance(data, dict) else []

    payload = {"ok": True, "query": query, "returned": len(images), "images": images}
    return ToolResult(content=formatted or "No images found.", structured_content=payload)


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
