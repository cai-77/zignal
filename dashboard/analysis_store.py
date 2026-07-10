"""
Persistent cache for AI analysis results.

Saves each LLMAnalysis to a local SQLite database so the same analysis
is not re-run (and re-billed) within the cache window.

Cache key: symbol + analysis_date + analysis_type  (+ cost_basis for exit)
Default TTL: 24 hours — after that the entry is treated as stale and a
             fresh API call is made on the next request.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dashboard.llm_analyzer import LLMAnalysis

_DB_PATH = Path(__file__).parent.parent / "db" / "analyses.db"


def init_db() -> None:
    """Create the analyses table if it does not exist."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                analysis_date   TEXT    NOT NULL,
                analysis_type   TEXT    NOT NULL,
                cost_basis      REAL,
                layer1_verdict  TEXT,
                llm_verdict     TEXT,
                llm_confidence  TEXT,
                llm_summary     TEXT,
                llm_analysis    TEXT,
                llm_observations TEXT,
                llm_risks       TEXT,
                llm_watch_for   TEXT,
                llm_model       TEXT,
                ctx_meta        TEXT,
                created_at      TEXT    NOT NULL
            )
        """)
        conn.commit()


def save_analysis(
    symbol: str,
    analysis_date: str,
    analysis_type: str,
    layer1_verdict: str,
    llm_result: LLMAnalysis,
    ctx_meta: Optional[dict] = None,
    cost_basis: Optional[float] = None,
) -> None:
    """Insert or replace an analysis result in the cache."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(_DB_PATH) as conn:
        # Remove any existing entry for this cache key first
        conn.execute(
            "DELETE FROM analyses WHERE symbol=? AND analysis_date=? AND analysis_type=?",
            (symbol.upper(), analysis_date, analysis_type),
        )
        conn.execute(
            """INSERT INTO analyses
               (symbol, analysis_date, analysis_type, cost_basis, layer1_verdict,
                llm_verdict, llm_confidence, llm_summary, llm_analysis,
                llm_observations, llm_risks, llm_watch_for, llm_model,
                ctx_meta, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                symbol.upper(),
                analysis_date,
                analysis_type,
                cost_basis,
                layer1_verdict,
                llm_result.verdict,
                llm_result.confidence,
                llm_result.summary,
                llm_result.analysis,
                json.dumps(llm_result.key_observations),
                json.dumps(llm_result.risks),
                llm_result.watch_for,
                llm_result.model_used,
                json.dumps(ctx_meta) if ctx_meta else None,
                now,
            ),
        )
        conn.commit()


def load_analysis(
    symbol: str,
    analysis_date: str,
    analysis_type: str,
    max_age_hours: int = 24,
) -> tuple[Optional[LLMAnalysis], Optional[dict], Optional[str]]:
    """
    Return (LLMAnalysis, ctx_meta, cached_at) if a fresh cached result exists,
    otherwise (None, None, None).
    """
    if not _DB_PATH.exists():
        return None, None, None

    cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM analyses
               WHERE symbol=? AND analysis_date=? AND analysis_type=?
                 AND created_at >= ?
               ORDER BY created_at DESC LIMIT 1""",
            (symbol.upper(), analysis_date, analysis_type, cutoff),
        ).fetchone()

    if row is None:
        return None, None, None

    llm_result = LLMAnalysis(
        verdict=row["llm_verdict"] or "",
        confidence=row["llm_confidence"] or "",
        summary=row["llm_summary"] or "",
        analysis=row["llm_analysis"] or "",
        key_observations=json.loads(row["llm_observations"] or "[]"),
        risks=json.loads(row["llm_risks"] or "[]"),
        watch_for=row["llm_watch_for"] or "",
        model_used=row["llm_model"] or "",
    )
    ctx_meta = json.loads(row["ctx_meta"]) if row["ctx_meta"] else {}
    cached_at = row["created_at"]
    return llm_result, ctx_meta, cached_at


def list_analyses(limit: int = 50) -> list[dict]:
    """Return recent analyses for a history view (most recent first)."""
    if not _DB_PATH.exists():
        return []
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT symbol, analysis_date, analysis_type, cost_basis,
                      layer1_verdict, llm_verdict, llm_confidence, llm_model, created_at
               FROM analyses ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
