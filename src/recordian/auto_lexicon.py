from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{1,31}")
_CJK_BLOCK_RE = re.compile(r"[\u4e00-\u9fff]+")

_STOPWORDS = {
    "这个",
    "那个",
    "我们",
    "你们",
    "他们",
    "就是",
    "然后",
    "可以",
    "一下",
    "没有",
    "还是",
    "因为",
    "所以",
    "如果",
    "但是",
    "的话",
    "已经",
    "现在",
    "时候",
    "什么",
    "怎么",
    "不是",
    "还有",
    "自己",
    "东西",
    "进行",
    "以及",
    "the",
    "and",
    "that",
    "with",
    "this",
    "from",
    "have",
    "your",
    "you",
}


def _normalize_term(term: str) -> str | None:
    token = term.strip()
    if not token:
        return None
    if token.isascii():
        token = token.lower()
    if len(token) < 2 or len(token) > 32:
        return None
    if token.isdigit():
        return None
    if token in _STOPWORDS:
        return None
    return token


def extract_terms(text: str) -> list[str]:
    if not text.strip():
        return []

    found: set[str] = set()

    for raw in _ASCII_TOKEN_RE.findall(text):
        token = _normalize_term(raw)
        if token:
            found.add(token)

    for block in _CJK_BLOCK_RE.findall(text):
        block = block.strip()
        if len(block) < 2:
            continue

        if len(block) <= 8:
            token = _normalize_term(block)
            if token:
                found.add(token)
            continue

        # Long CJK blocks are usually whole sentences. Extract a bounded amount
        # of short ngrams so repeated domain terms can still be learned.
        emitted = 0
        for n in (4, 3, 2):
            if len(block) < n:
                continue
            for i in range(0, len(block) - n + 1):
                token = _normalize_term(block[i : i + n])
                if token:
                    found.add(token)
                emitted += 1
                if emitted >= 24:
                    break
            if emitted >= 24:
                break

    return sorted(found)


class AutoLexicon:
    def __init__(
        self,
        *,
        db_path: Path | str,
        max_hotwords: int = 40,
        min_accepts: int = 2,
        max_terms: int = 5000,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.max_hotwords = max(0, int(max_hotwords))
        self.min_accepts = max(1, int(min_accepts))
        self.max_terms = max(100, int(max_terms))
        self._lock = threading.RLock()
        self._updates_since_prune = 0
        self._closed = False

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lexicon_terms (
                    term TEXT PRIMARY KEY,
                    seen_count INTEGER NOT NULL DEFAULT 0,
                    accept_count INTEGER NOT NULL DEFAULT 0,
                    last_seen INTEGER NOT NULL DEFAULT 0,
                    last_accept INTEGER NOT NULL DEFAULT 0,
                    blocked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lexicon_terms_rank
                ON lexicon_terms(blocked, accept_count DESC, last_accept DESC)
                """
            )

    def compose_hotwords(self, base_hotwords: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for raw in base_hotwords:
            token = str(raw).strip()
            if not token:
                continue
            if token in seen:
                continue
            seen.add(token)
            merged.append(token)

        if self.max_hotwords <= 0:
            return merged
        if len(merged) >= self.max_hotwords:
            return merged[: self.max_hotwords]

        candidate_limit = max(self.max_hotwords * 4, 64)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT term
                FROM lexicon_terms
                WHERE blocked = 0
                  AND accept_count >= ?
                ORDER BY accept_count DESC, last_accept DESC
                LIMIT ?
                """,
                (self.min_accepts, candidate_limit),
            ).fetchall()

        for (term,) in rows:
            token = str(term).strip()
            if not token or token in seen:
                continue
            seen.add(token)
            merged.append(token)
            if len(merged) >= self.max_hotwords:
                break
        return merged

    def observe_accepted(self, text: str) -> int:
        terms = extract_terms(text)
        if not terms:
            return 0

        now_ts = int(time.time())
        with self._lock:
            with self._conn:
                for term in terms:
                    self._conn.execute(
                        """
                        INSERT INTO lexicon_terms (
                            term, seen_count, accept_count, last_seen, last_accept, blocked
                        ) VALUES (?, 1, 1, ?, ?, 0)
                        ON CONFLICT(term) DO UPDATE SET
                            seen_count = seen_count + 1,
                            accept_count = accept_count + 1,
                            last_seen = excluded.last_seen,
                            last_accept = excluded.last_accept
                        """,
                        (term, now_ts, now_ts),
                    )
            self._updates_since_prune += len(terms)
            if self._updates_since_prune >= 128:
                self._prune_to_limit_locked()
                self._updates_since_prune = 0
        return len(terms)

    def _prune_to_limit_locked(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                DELETE FROM lexicon_terms
                WHERE term IN (
                    SELECT term
                    FROM lexicon_terms
                    ORDER BY accept_count DESC, last_accept DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.max_terms,),
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._conn.close()
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
