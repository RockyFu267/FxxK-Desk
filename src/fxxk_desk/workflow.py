"""Persist visible observations and an outbox; never blindly retry a send."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .targets import open_target
from .interference import UserInterference, interrupted_send_state


def fingerprint(message):
    fields = {key: message.get(key) for key in ("text", "sender", "direction", "complete")}
    for key in ("text", "sender"):
        if isinstance(fields[key], str):
            fields[key] = "".join(fields[key].split())
    return json.dumps(fields, sort_keys=True, ensure_ascii=False)


def overlap(previous, current):
    """Match ordered suffix/prefix, preserving repeated identical bubbles."""
    for count in range(min(len(previous), len(current)), 0, -1):
        if previous[-count:] == current[:count]:
            return count
    return 0


class Workflow:
    def __init__(self, target_name, target, backend, gateway, database="artifacts/chat.sqlite3"):
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.name = target_name
        self.target, self.backend, self.gateway = target, backend, gateway
        # Scope includes executable and group, preventing reuse across config changes.
        self.scope = hashlib.sha256(json.dumps([target_name, target.executable, target.chat], ensure_ascii=False).encode()).hexdigest()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS snapshots(scope TEXT PRIMARY KEY, image_hash TEXT, messages TEXT);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, scope TEXT, observed_at TEXT,
              payload TEXT, screenshot TEXT, eligible INTEGER, processed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE,
              text TEXT, state TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS capture_pages(id INTEGER PRIMARY KEY,scope TEXT,
              observed_at TEXT,image_hash TEXT,screenshot TEXT,messages TEXT);
            CREATE TABLE IF NOT EXISTS collection_gaps(id INTEGER PRIMARY KEY,scope TEXT,
              first_seen TEXT,last_seen TEXT,reason TEXT,attempts INTEGER DEFAULT 1,resolved_at TEXT);
            CREATE TABLE IF NOT EXISTS interaction_pauses(id INTEGER PRIMARY KEY,scope TEXT,
              observed_at TEXT DEFAULT CURRENT_TIMESTAMP,phase TEXT,outcome TEXT);
        ''')
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(messages)")}
        if "continuity_verified" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE messages ADD COLUMN continuity_verified INTEGER NOT NULL DEFAULT 0")
                self.db.execute("UPDATE messages SET continuity_verified=eligible")

    def close(self):
        self.db.close()

    def record_interference(self, phase, outcome='paused'):
        with self.db:
            self.db.execute('INSERT INTO interaction_pauses(scope,phase,outcome) VALUES(?,?,?)', (self.scope, phase, outcome))

    def _check_context(self):
        if getattr(type(self.backend), 'supports_input_arbitration', False):
            self.backend.guard()

    def history(self):
        return [{"id": row["id"], "observed_at": row["observed_at"],
                 "message": json.loads(row["payload"]), "screenshot": row["screenshot"],
                 "source": json.loads(row["payload"]).get("source", "vision_model"), "verification_status": "unverified"}
                for row in self.db.execute("SELECT * FROM messages WHERE scope=? ORDER BY id", (self.scope,))]

    def collect(self):
        if self.target.backfill_enabled and getattr(type(self.backend), "supports_backfill", False):
            return self._collect_backfill()
        return self._collect_visible()

    def _collect_backfill(self):
        from .backfill import Backfill, Page
        open_target(self.target, self.backend)
        previous = self.db.execute("SELECT * FROM snapshots WHERE scope=?", (self.scope,)).fetchone()
        version = getattr(type(self.gateway), "extractor_version", "vision-v1")

        def read_page():
            path = self.backend.message_snapshot(self.target.chat)
            digest = hashlib.sha256(Path(path).read_bytes() + version.encode()).hexdigest()
            return Page(self.gateway.extract_messages(path), str(path), digest)

        # Avoid OCR on an unchanged committed latest screenshot.
        path = self.backend.message_snapshot(self.target.chat)
        digest = hashlib.sha256(Path(path).read_bytes() + version.encode()).hexdigest()
        if previous and previous["image_hash"] == digest:
            return {"status": "unchanged", "new_messages": 0, "coverage": "visible_with_backfill", "caught_up": True}
        initial = Page(self.gateway.extract_messages(path), str(path), digest)
        old_keys = ([fingerprint(json.loads(key)) for key in json.loads(previous["messages"])
                     if json.loads(key).get("complete")] if previous else [])
        engine = Backfill(read_page, self.backend.scroll_chat, fingerprint,
                          self.target.backfill_max_pages, self.target.backfill_max_seconds,
                          self.target.anchor_messages)
        recovered = engine.recover(old_keys, initial, had_checkpoint=previous is not None)
        self._check_context()
        now = datetime.now(timezone.utc).isoformat()
        continuous = recovered.status in ("overlap", "recovered")
        with self.db:
            for page in recovered.pages:
                self.db.execute("INSERT INTO capture_pages(scope,observed_at,image_hash,screenshot,messages) VALUES(?,?,?,?,?)",
                                (self.scope, now, page.image_hash, page.screenshot, json.dumps(page.messages, ensure_ascii=False)))
            if recovered.checkpoint or recovered.status == "baseline":
                observations = recovered.checkpoint if recovered.status == "baseline" else recovered.messages
                for message in observations:
                    payload = {key: value for key, value in message.items() if not key.startswith("_")}
                    self.db.execute("INSERT INTO messages(scope,observed_at,payload,screenshot,eligible,continuity_verified) VALUES(?,?,?,?,?,?)",
                                    (self.scope, now, json.dumps(payload, ensure_ascii=False), message["_screenshot"],
                                     int(continuous and message["direction"] == "incoming" and message["complete"]), int(continuous)))
                keys = [fingerprint(m) for m in recovered.checkpoint][-32:]
                self.db.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?)", (self.scope, recovered.image_hash, json.dumps(keys)))
            gap = self.db.execute("SELECT id FROM collection_gaps WHERE scope=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1", (self.scope,)).fetchone()
            if recovered.caught_up:
                self.db.execute("UPDATE collection_gaps SET resolved_at=? WHERE scope=? AND resolved_at IS NULL", (now, self.scope))
            elif gap:
                self.db.execute("UPDATE collection_gaps SET last_seen=?,reason=?,attempts=attempts+1 WHERE id=?", (now, recovered.reason, gap["id"]))
            else:
                self.db.execute("INSERT INTO collection_gaps(scope,first_seen,last_seen,reason) VALUES(?,?,?,?)", (self.scope, now, now, recovered.reason))
        return {"status": recovered.status, "new_messages": len(recovered.messages),
                "continuity": "recovered" if continuous else "baseline_or_gap",
                "coverage": "visible_with_backfill", "caught_up": recovered.caught_up,
                "pages": len(recovered.pages), "scrolls": recovered.scrolls, "gap_reason": recovered.reason}

    def _collect_visible(self):
        open_target(self.target, self.backend)
        path = self.backend.message_snapshot(self.target.chat)
        extractor_version = getattr(type(self.gateway), "extractor_version", "vision-v1")
        digest = hashlib.sha256(Path(path).read_bytes() + extractor_version.encode()).hexdigest()
        previous = self.db.execute("SELECT * FROM snapshots WHERE scope=?", (self.scope,)).fetchone()
        if previous and previous["image_hash"] == digest:
            return {"status": "unchanged", "new_messages": 0, "coverage": "visible_only"}
        current = self.gateway.extract_messages(path)
        keys = [fingerprint(m) for m in current]
        old_keys = [fingerprint(json.loads(key)) for key in json.loads(previous["messages"])] if previous else []
        offset = 0
        while offset < len(current) and not current[offset]["complete"]:
            offset += 1
        matched = overlap(old_keys, keys[offset:])
        count = offset + matched if matched else 0
        # First observation is a baseline, not a queue of old messages to reply to.
        # No overlap could mean scrolling, missed history or changed OCR: record,
        # but do not auto-reply to this uncertain transition.
        continuous = previous is not None and matched > 0
        self._check_context()
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            for message in current[count:]:
                eligible = continuous and message["direction"] == "incoming" and message["complete"]
                self.db.execute("INSERT INTO messages(scope,observed_at,payload,screenshot,eligible,continuity_verified) VALUES(?,?,?,?,?,?)",
                                (self.scope, now, json.dumps(message, ensure_ascii=False), str(path), int(eligible), int(continuous)))
            self.db.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?)", (self.scope, digest, json.dumps(keys)))
        return {"status": "observed", "new_messages": len(current) - count,
                "coverage": "visible_only", "continuity": "overlap" if continuous else "baseline_or_gap"}

    def draft(self, instruction):
        # One draft at a time; no background queue drains or historical bulk replies.
        row = self.db.execute("SELECT * FROM messages WHERE scope=? AND eligible=1 AND processed=0 ORDER BY id DESC LIMIT 1", (self.scope,)).fetchone()
        if row is None:
            return {"status": "no_new_incoming_message"}
        pending = self.db.execute("SELECT * FROM outbox WHERE message_id=?", (row["id"],)).fetchone()
        if pending:
            return dict(pending)
        text = self.gateway.reply(json.loads(row["payload"]), instruction)
        with self.db:
            cursor = self.db.execute("INSERT INTO outbox(message_id,text,state) VALUES(?,?,'draft')", (row["id"], text))
            # Older messages are context, not separate reply jobs.
            self.db.execute("UPDATE messages SET processed=1 WHERE scope=? AND id<=?", (self.scope, row["id"]))
        return {"id": cursor.lastrowid, "text": text, "state": "draft"}

    def send(self, reply_id):
        row = self.db.execute("SELECT o.* FROM outbox o JOIN messages m ON m.id=o.message_id WHERE o.id=? AND m.scope=?", (reply_id, self.scope)).fetchone()
        if not row or row["state"] != "draft":
            raise ValueError("Only an unsent draft for this target can be sent")
        # Persist before input; a crash or exception leaves an uncertain send blocked.
        with self.db:
            claimed = self.db.execute("UPDATE outbox SET state='sending' WHERE id=? AND state='draft'", (reply_id,))
            if claimed.rowcount != 1:
                raise RuntimeError("Draft already claimed by another sender")
        try:
            self.backend.send_phase = 'preparing'
            open_target(self.target, self.backend)
            self.backend.send_reply(self.target.chat, row["text"])
        except UserInterference:
            state = interrupted_send_state(self.backend)
            with self.db:
                self.db.execute("UPDATE outbox SET state=?,error='Interrupted by user input' WHERE id=?", (state, reply_id))
            self.record_interference(getattr(self.backend, 'send_phase', 'preparing'), state)
            raise
        except Exception:
            with self.db:
                self.db.execute("UPDATE outbox SET state='unknown',error='Send outcome requires manual inspection' WHERE id=?", (reply_id,))
            raise
        with self.db:
            self.db.execute("UPDATE outbox SET state='sent' WHERE id=?", (reply_id,))
        return {"id": reply_id, "state": "sent"}
