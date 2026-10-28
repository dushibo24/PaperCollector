from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from paper_collector.models import Paper, Review


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalized_title(title: str) -> str:
    return re.sub(r"\W+", " ", title.casefold()).strip()


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS works (
                id TEXT PRIMARY KEY,
                doi TEXT,
                title TEXT NOT NULL,
                normalized_title TEXT,
                paper_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'discovered',
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                review_json TEXT,
                original_pdf TEXT,
                dual_pdf TEXT,
                brief_path TEXT,
                package_path TEXT,
                baidupan_remote_path TEXT,
                share_url TEXT,
                share_password TEXT,
                shared_at TEXT,
                rejected_reason TEXT,
                summary_delivered_at TEXT,
                file_delivered_at TEXT,
                last_error TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS works_doi_unique
                ON works(doi) WHERE doi IS NOT NULL AND doi != '';
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                selected_work_id TEXT,
                error TEXT,
                FOREIGN KEY(selected_work_id) REFERENCES works(id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                work_id TEXT,
                created_at TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT,
                FOREIGN KEY(run_id) REFERENCES runs(id),
                FOREIGN KEY(work_id) REFERENCES works(id)
            );
            CREATE TABLE IF NOT EXISTS api_daily_usage (
                provider TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                request_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(provider, usage_date)
            );
            """
        )
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(works)").fetchall()
        }
        migrations = {
            "normalized_title": "TEXT",
            "baidupan_remote_path": "TEXT",
            "share_url": "TEXT",
            "share_password": "TEXT",
            "shared_at": "TEXT",
            "package_path": "TEXT",
            "reading_note_path": "TEXT",
            "reading_note_url": "TEXT",
        }
        for name, column_type in migrations.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE works ADD COLUMN {name} {column_type}")
        rows = self.connection.execute(
            "SELECT id, title FROM works WHERE normalized_title IS NULL"
        ).fetchall()
        self.connection.executemany(
            "UPDATE works SET normalized_title=? WHERE id=?",
            [(normalized_title(row["title"]), row["id"]) for row in rows],
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS works_normalized_title ON works(normalized_title)"
        )
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def start_run(self) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(started_at, status) VALUES (?, 'running')", (utc_now(),)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        selected_work_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at=?, status=?, selected_work_id=?, error=? WHERE id=?",
            (utc_now(), status, selected_work_id, error, run_id),
        )
        self.connection.commit()

    def event(
        self, event: str, run_id: int | None = None, work_id: str | None = None, detail: str = ""
    ) -> None:
        self.connection.execute(
            "INSERT INTO events(run_id, work_id, created_at, event, detail) VALUES (?, ?, ?, ?, ?)",
            (run_id, work_id, utc_now(), event, detail),
        )
        self.connection.commit()

    def reserve_api_request(
        self, provider: str, daily_limit: int, usage_date: str | None = None
    ) -> bool:
        """Atomically reserve one request from a provider's local-calendar daily quota."""
        if daily_limit <= 0:
            return False
        usage_date = usage_date or datetime.now().astimezone().date().isoformat()
        now = utc_now()
        cursor = self.connection.execute(
            """INSERT INTO api_daily_usage(
                   provider, usage_date, request_count, updated_at
               ) VALUES (?, ?, 1, ?)
               ON CONFLICT(provider, usage_date) DO UPDATE SET
                   request_count=request_count + 1,
                   updated_at=excluded.updated_at
               WHERE request_count < ?""",
            (provider, usage_date, now, daily_limit),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def api_request_count(self, provider: str, usage_date: str | None = None) -> int:
        usage_date = usage_date or datetime.now().astimezone().date().isoformat()
        row = self.connection.execute(
            """SELECT request_count FROM api_daily_usage
               WHERE provider=? AND usage_date=?""",
            (provider, usage_date),
        ).fetchone()
        return int(row["request_count"]) if row else 0

    def upsert_paper(self, paper: Paper) -> bool:
        existing = self.connection.execute(
            """SELECT id, status FROM works
               WHERE id=? OR (doi IS NOT NULL AND doi=?) OR normalized_title=?""",
            (paper.id, paper.doi, normalized_title(paper.title)),
        ).fetchone()
        now = utc_now()
        payload = json.dumps(paper.to_dict(), ensure_ascii=False)
        if existing:
            # Preserve lifecycle state; refresh only source metadata and scores.
            paper.id = existing["id"]
            payload = json.dumps(paper.to_dict(), ensure_ascii=False)
            self.connection.execute(
                "UPDATE works SET paper_json=?, title=?, normalized_title=?, updated_at=? WHERE id=?",
                (payload, paper.title, normalized_title(paper.title), now, existing["id"]),
            )
            self.connection.commit()
            return False
        self.connection.execute(
            """INSERT INTO works(
                   id, doi, title, normalized_title, paper_json, status, first_seen_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, 'discovered', ?, ?)""",
            (paper.id, paper.doi, paper.title, normalized_title(paper.title), payload, now, now),
        )
        self.connection.commit()
        return True

    def get_paper(self, paper_id: str) -> Paper:
        row = self.connection.execute(
            "SELECT paper_json FROM works WHERE id=?", (paper_id,)
        ).fetchone()
        if not row:
            raise KeyError(paper_id)
        return Paper.from_dict(json.loads(row["paper_json"]))

    def eligible_papers(self, limit: int = 200) -> list[Paper]:
        rows = self.connection.execute(
            """SELECT paper_json FROM works
               WHERE file_delivered_at IS NULL
                 AND status IN ('discovered', 'ranked', 'failed')
                 AND review_json IS NULL
                 AND rejected_reason IS NULL
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [Paper.from_dict(json.loads(row["paper_json"])) for row in rows]

    def pending_delivery(self) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM works
               WHERE review_json IS NOT NULL AND rejected_reason IS NULL
                 AND file_delivered_at IS NULL
               ORDER BY updated_at ASC LIMIT 1"""
        ).fetchone()

    def inflight_delivery(self) -> sqlite3.Row | None:
        """Return work whose delivery has already started and therefore must be resumed first."""
        return self.connection.execute(
            """SELECT * FROM works
               WHERE review_json IS NOT NULL AND rejected_reason IS NULL
                 AND file_delivered_at IS NULL
                 AND (
                     status IN ('translated', 'shared', 'delivering')
                     OR package_path IS NOT NULL
                     OR share_url IS NOT NULL
                 )
               ORDER BY updated_at ASC LIMIT 1"""
        ).fetchone()

    def queued_deliveries(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT * FROM works
               WHERE review_json IS NOT NULL AND rejected_reason IS NULL
                 AND file_delivered_at IS NULL
                 AND status IN ('reviewed', 'failed')
               ORDER BY updated_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()

    def recent_deliveries(self, days: int = 7, limit: int = 50) -> list[sqlite3.Row]:
        if days <= 0:
            return []
        return self.connection.execute(
            """SELECT * FROM works
               WHERE file_delivered_at IS NOT NULL
                 AND datetime(file_delivered_at) >= datetime('now', ?)
               ORDER BY file_delivered_at DESC LIMIT ?""",
            (f"-{days} days", limit),
        ).fetchall()

    def work_row(self, paper_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM works WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise KeyError(paper_id)
        return row

    def set_status(self, paper_id: str, status: str, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE works SET status=?, last_error=?, updated_at=? WHERE id=?",
            (status, error, utc_now(), paper_id),
        )
        self.connection.commit()

    def save_review(self, paper_id: str, review: Review) -> None:
        self.connection.execute(
            "UPDATE works SET review_json=?, status='reviewed', updated_at=?, last_error=NULL WHERE id=?",
            (
                json.dumps(
                    review.__dict__
                    if hasattr(review, "__dict__")
                    else {name: getattr(review, name) for name in review.__dataclass_fields__},
                    ensure_ascii=False,
                ),
                utc_now(),
                paper_id,
            ),
        )
        self.connection.commit()

    def reject(self, paper_id: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE works SET status='rejected', rejected_reason=?, updated_at=? WHERE id=?",
            (reason, utc_now(), paper_id),
        )
        self.connection.commit()

    def save_artifacts(
        self,
        paper_id: str,
        original_pdf: Path | None = None,
        dual_pdf: Path | None = None,
        brief_path: Path | None = None,
        package_path: Path | None = None,
    ) -> None:
        current = self.connection.execute(
            "SELECT original_pdf, dual_pdf, brief_path, package_path FROM works WHERE id=?",
            (paper_id,),
        ).fetchone()
        self.connection.execute(
            """UPDATE works SET original_pdf=?, dual_pdf=?, brief_path=?, package_path=?,
                   updated_at=? WHERE id=?""",
            (
                str(original_pdf) if original_pdf else current["original_pdf"],
                str(dual_pdf) if dual_pdf else current["dual_pdf"],
                str(brief_path) if brief_path else current["brief_path"],
                str(package_path) if package_path else current["package_path"],
                utc_now(),
                paper_id,
            ),
        )
        self.connection.commit()

    def save_reading_note(self, paper_id: str, path: Path, url: str) -> None:
        self.connection.execute(
            "UPDATE works SET reading_note_path=?, reading_note_url=?, updated_at=? WHERE id=?",
            (str(path), url, utc_now(), paper_id),
        )
        self.connection.commit()

    def mark_summary_delivered(self, paper_id: str) -> None:
        self.connection.execute(
            "UPDATE works SET summary_delivered_at=?, status='delivering', updated_at=? WHERE id=?",
            (utc_now(), utc_now(), paper_id),
        )
        self.connection.commit()

    def mark_file_delivered(self, paper_id: str) -> None:
        self.connection.execute(
            "UPDATE works SET file_delivered_at=?, status='delivered', updated_at=?, last_error=NULL WHERE id=?",
            (utc_now(), utc_now(), paper_id),
        )
        self.connection.commit()

    def save_share(
        self, paper_id: str, remote_path: str, share_url: str, share_password: str
    ) -> None:
        self.connection.execute(
            """UPDATE works
               SET baidupan_remote_path=?, share_url=?, share_password=?, shared_at=?,
                   status='shared', updated_at=?, last_error=NULL
               WHERE id=?""",
            (remote_path, share_url, share_password, utc_now(), utc_now(), paper_id),
        )
        self.connection.commit()

    def mark_link_delivered(self, paper_id: str) -> None:
        """Atomically mark the single ServerChan message containing summary and file link."""
        now = utc_now()
        self.connection.execute(
            """UPDATE works
               SET summary_delivered_at=?, file_delivered_at=?, status='delivered',
                   updated_at=?, last_error=NULL WHERE id=?""",
            (now, now, now, paper_id),
        )
        self.connection.commit()

    def stats(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, count(*) AS count FROM works GROUP BY status ORDER BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}
