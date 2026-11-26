import hashlib
import json
import secrets
import sqlite3

STUDENT, LEADER, ADMIN, TEACHER = "Учащийся", "Староста", "Администратор", "Учитель"
ROLES = (STUDENT, LEADER, ADMIN, TEACHER)


class Roles:
    def __init__(self, path, admins):
        self.admins = admins
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY, name TEXT, role TEXT, subjects TEXT, blocked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS invites (
                digest TEXT PRIMARY KEY, role TEXT, subjects TEXT);
        """)

    def issue(self, role, subjects):
        key = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute("INSERT INTO invites VALUES (?,?,?)",
                            (hashlib.sha256(key.encode()).hexdigest(), role, json.dumps(subjects)))
        return key

    def redeem(self, key, uid, name):
        with self.db:
            grant = self.db.execute("DELETE FROM invites WHERE digest=? RETURNING role,subjects",
                                    (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()
            if grant:
                self.db.execute("""INSERT INTO members(id,name,role,subjects) VALUES (?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,role=excluded.role,
                    subjects=excluded.subjects""", (uid, name, *grant))
        return bool(grant)

    def access(self, uid, subjects):
        if uid in self.admins:
            return ADMIN, subjects
        row = self.db.execute("SELECT role,subjects,blocked FROM members WHERE id=?", (uid,)).fetchone()
        if not row:
            return None, []
        role, allowed, blocked = row
        if role == LEADER and not blocked:
            return role, [s for s in subjects if s in json.loads(allowed)]
        return role, subjects if role in (ADMIN, TEACHER) else []

    def leaders(self):
        return self.db.execute("SELECT id,name,blocked FROM members WHERE role=? ORDER BY name,id",
                               (LEADER,)).fetchall()

    def block(self, uid, blocked):
        with self.db:
            self.db.execute("UPDATE members SET blocked=? WHERE id=? AND role=?", (blocked, uid, LEADER))
