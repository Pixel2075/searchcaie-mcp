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
import sys
import time
from typing import Any, Optional

import httpx
from fastmcp import FastMCP


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
    item = {
        "rank": index,
        "id": raw.get("id"),
        "subject": paper.get("subject_code"),
        "paper": paper.get("paper_number"),
        "year": paper.get("year"),
        "session": paper.get("session_name"),
        "session_short": _short_session(paper.get("session_name")),
        "variant": paper.get("variant"),
        "question_number": raw.get("question_number"),
        "marks": raw.get("marks"),
        "relevance_score": raw.get("relevance_score"),
        "snippet": _clean_text(raw.get("question_text"), max_len=200),
    }
    if matched_topics:
        item["matched_topics"] = matched_topics
    return item


def _validate_mode(mode: str) -> bool:
    return mode in {"hybrid", "keyword", "semantic"}


@mcp.tool(title="Search Questions", tags={"search", "core"})
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
) -> dict[str, Any]:
    """Search past-paper questions with optional filters.

    Returns structured and compact result cards. Use get_questions(question_ids)
    to fetch full question text and mark schemes.
    """
    if not _validate_mode(mode):
        return _tool_error(
            "INVALID_MODE",
            "mode must be one of: hybrid, keyword, semantic",
            details={"mode": mode},
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
        return _error_from_exception(exc, "/search")

    raw_results = data.get("results", []) if isinstance(data, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []

    visible_results = raw_results[:capped_limit]
    cards = [_result_item(r, i + 1) for i, r in enumerate(visible_results) if isinstance(r, dict)]
    card_ids = [r["id"] for r in cards if r.get("id") is not None]

    return {
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
        "next_step": {
            "tool": "get_questions",
            "question_ids": card_ids[:15],
            "example": "get_questions(question_ids='id1,id2,id3')",
        },
    }


@mcp.tool(title="Search Multiple Topics", tags={"search", "core"})
def search_multi(
    topics: str,
    subject: Optional[str] = DEFAULT_SUBJECT,
    paper: Optional[int] = None,
    year: Optional[int] = None,
    session: Optional[str] = None,
    chapter: Optional[int] = None,
    mode: str = "hybrid",
    limit_per_topic: int = 10,
    max_results: int = 40,
    expand: bool = True,
) -> dict[str, Any]:
    """Search multiple comma-separated topics and deduplicate by question ID."""
    if not _validate_mode(mode):
        return _tool_error(
            "INVALID_MODE",
            "mode must be one of: hybrid, keyword, semantic",
            details={"mode": mode},
        )

    raw_topics = [t.strip() for t in topics.split(",") if t.strip()]
    topic_list: list[str] = []
    for t in raw_topics:
        if t not in topic_list:
            topic_list.append(t)

    if not topic_list:
        return _tool_error(
            "NO_TOPICS",
            "Provide one or more topics separated by commas.",
            details={"topics": topics},
        )

    capped_limit_per_topic = max(1, min(limit_per_topic, 30))
    capped_max_results = max(1, min(max_results, 100))
    normalized_session = _normalize_session_filter(session)

    all_results: dict[int, dict[str, Any]] = {}
    topic_breakdown: list[dict[str, Any]] = []

    for topic in topic_list:
        corrected_topic, was_corrected = _spell_correct(topic)
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

    cards = []
    for i, row in enumerate(visible, 1):
        matched_topics = sorted(list(row.get("_matched_topics", [])))
        cards.append(_result_item(row, i, matched_topics=matched_topics))

    card_ids = [r["id"] for r in cards if r.get("id") is not None]

    return {
        "ok": True,
        "topics": topic_list,
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
        "next_step": {
            "tool": "get_questions",
            "question_ids": card_ids[:20],
            "example": "get_questions(question_ids='id1,id2,id3')",
        },
    }


@mcp.tool(title="Get Questions By IDs", tags={"search", "core"})
def get_questions(question_ids: str) -> dict[str, Any]:
    """Fetch full question details and mark schemes for comma-separated IDs."""
    try:
        parsed_ids = [int(x.strip()) for x in question_ids.split(",") if x.strip()]
    except ValueError:
        return _tool_error(
            "INVALID_IDS",
            "question_ids must be comma-separated integers, e.g. '552,799,866'.",
            details={"question_ids": question_ids},
        )

    unique_ids: list[int] = []
    for qid in parsed_ids:
        if qid not in unique_ids:
            unique_ids.append(qid)

    if not unique_ids:
        return _tool_error("NO_IDS", "No question IDs provided.")
    if len(unique_ids) > MAX_BATCH_IDS:
        return _tool_error(
            "TOO_MANY_IDS",
            f"Too many IDs. Max {MAX_BATCH_IDS} per request.",
            details={"provided": len(unique_ids), "max": MAX_BATCH_IDS},
        )

    try:
        rows = _api_get("/questions/batch", {"ids": ",".join(str(x) for x in unique_ids)})
    except Exception as exc:
        logger.error("get_questions failed: %s", exc, exc_info=True)
        return _error_from_exception(exc, "/questions/batch")

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

        questions.append(
            {
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
                "answer_text": row.get("answer_text") or "",
                "answer_bullet_points": bullets,
                "is_image_based": bool(row.get("is_image_based")),
                "image_path": row.get("image_path"),
                "ms_image_path": row.get("ms_image_path"),
                "ocr_text": row.get("ocr_text"),
            }
        )

    return {
        "ok": True,
        "requested_ids": unique_ids,
        "meta": {
            "requested": len(unique_ids),
            "found": len(questions),
            "missing": len(missing_ids),
            "api_base": API_BASE,
        },
        "missing_ids": missing_ids,
        "questions": questions,
    }


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
        "2) Read IDs from results and call get_questions(question_ids='id1,id2,...').\n"
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
        "2) Call get_questions for top IDs.\n"
        "3) Summarize paper distribution, mark patterns, and recurring wording.\n"
        "4) Give exam-focused advice based on the evidence."
    )


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
