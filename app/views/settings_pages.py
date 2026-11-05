"""Settings module — everything under /settings:

  * My Profile        -> view/edit the logged-in user's own display name,
                        email and phone
  * Change Password   -> link out to /change-password
  * Database Backup    -> download a consistent point-in-time copy of the
                        live SQLite database (admin only)
  * Export All Data    -> download every business table as a single JSON
                        file, independent of the database file itself
                        (admin only)
  * User Management    -> list every account, add new ones, and
                        activate/deactivate or delete existing ones
                        (admin only)

Kept as its own mixin/module, same pattern as every other page group in
this app (AuthPagesMixin, DashboardPagesMixin, ...).
"""
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime

from utils.http_utils import (
    esc,
    html_response,
    redirect,
    new_csrf_token,
    consume_csrf_token,
    invalidate_user_sessions,
    clear_all_sessions,
    cross_process_lock,
    StoreLockTimeout,
    update_session,
    file_response,
    UploadedFile,
)
from database import get_db, DB_PATH, init_db
from database import wait_for_connections_to_close
from utils import backup_crypto

# Upstream sanity limit for an uploaded restore file — comfortably above
# any real database this app will produce, just to stop an obviously
# wrong/huge upload from being read fully into memory.
MAX_RESTORE_BYTES = 500 * 1024 * 1024

# Where "Save on Server" backups are kept. Defaults to a server_backups/
# folder next to the database; override with the QUOTATION_BACKUP_DIR
# environment variable (e.g. to point at a different disk/mount).
BACKUP_DIR = os.environ.get(
    'QUOTATION_BACKUP_DIR',
    os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'server_backups'))

# Only the newest N server-side backups are kept; older ones are removed
# automatically each time a new one is saved, so the folder can't grow
# without bound. (pre_restore_backup_* safety copies are never touched.)
MAX_SERVER_BACKUPS = 30

# The only filenames the server-backup download/delete routes accept —
# rules out any path traversal via the URL.
_BACKUP_NAME_RE = re.compile(r'^aypee_backup_\d{8}_\d{6}(?:_\d+)?\.db(?:\.enc)?$')


def _fmt_bytes(n):
    """Human-readable file size, e.g. 812 B / 96.4 KB / 3.1 MB."""
    n = float(n)
    for unit in ('B', 'KB', 'MB'):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class SettingsPagesMixin:
    # ── FORM HANDLER: PROFILE ────────────────────────────────
    def handle_profile_update(self, form, user_id):
        """Self-service profile edit (display name, email, phone),
        available to any logged-in user. Username/role are intentionally
        left read-only here — an admin can grant a fresh account via
        /register instead of this page silently renaming/re-privileging
        an existing one."""
        full_name = form.get('full_name', '').strip()
        email = form.get('email', '').strip()
        phone = form.get('phone', '').strip()

        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg='Your session expired — please try again.'),
                status=400))

        if not full_name:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True, msg='Full name cannot be empty.')))
        if len(full_name) > 100:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True, msg='Full name is too long.')))
        if len(email) > 150:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True, msg='Email is too long.')))
        if len(phone) > 30:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True, msg='Phone number is too long.')))

        db = get_db()
        db.execute("UPDATE users SET full_name=?, email=?, phone=? WHERE id=?",
                   (full_name, email or None, phone or None, user_id))
        db.commit()
        db.close()

        # Keep the sidebar/topbar in sync immediately, without requiring
        # the user to log out and back in.
        update_session(self.headers.get('Cookie'), full_name=full_name)

        return self.send(*html_response(
            self.pg_settings(user_id, success=True, msg='Profile updated.')))

    # ── DOWNLOAD: DATABASE BACKUP ────────────────────────────
    def _settings_backup(self, user_id):
        if not self._is_admin(user_id):
            return redirect('/settings')
        data = self._backup_db_bytes()
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return file_response(data, f'aypee_backup_{stamp}.db', 'application/x-sqlite3')

    # ── shared: optional password on an export ───────────────
    @staticmethod
    def _read_backup_password(form):
        """(password, error). password is '' when the person left both
        boxes empty (= plain, unencrypted backup)."""
        pw = form.get('backup_password', '') or ''
        pw2 = form.get('backup_password_confirm', '') or ''
        if not pw and not pw2:
            return '', None
        if pw != pw2:
            return '', 'The two passwords do not match — nothing was exported.'
        if len(pw) < backup_crypto.MIN_PASSWORD_LEN:
            return '', (f'Use a backup password of at least '
                        f'{backup_crypto.MIN_PASSWORD_LEN} characters — nothing was exported.')
        return pw, None

    # ── DOWNLOAD (+ optionally keep a copy on the server) ────
    def handle_settings_backup_download(self, form, user_id):
        """POST version of the download button. If 'Also keep a copy on
        the server' is ticked, the very same snapshot that is sent to the
        browser is also written to BACKUP_DIR, so the two files match. If a
        password is entered, both are encrypted with it."""
        if not self._is_admin(user_id):
            return self.send(*redirect('/settings'))
        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg='Your session expired — please try again.'),
                status=400))
        password, pw_err = self._read_backup_password(form)
        if pw_err:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True, msg=pw_err), status=400))

        data = self._backup_db_bytes()
        encrypted = bool(password)
        if encrypted:
            data = backup_crypto.encrypt_bytes(data, password)
        if form.get('save_on_server'):
            try:
                name = self._save_backup_to_server(data, encrypted)
            except OSError as e:
                return self.send(*html_response(
                    self.pg_settings(user_id, err=True,
                                      msg=f'Could not save a copy on the server ({e.strerror or e}). '
                                          'The download was cancelled so you are not left thinking '
                                          'a server copy exists — untick the option to download only.'),
                    status=500))
        else:
            name = (f"aypee_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                    + ('.enc' if encrypted else ''))
        ctype = 'application/octet-stream' if encrypted else 'application/x-sqlite3'
        return self.send(*file_response(data, name, ctype))

    # ── SAVE ON SERVER ONLY (no download) ────────────────────
    def handle_settings_backup_server(self, form, user_id):
        if not self._is_admin(user_id):
            return self.send(*redirect('/settings'))
        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg='Your session expired — please try again.'),
                status=400))
        password, pw_err = self._read_backup_password(form)
        if pw_err:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True, msg=pw_err), status=400))
        data = self._backup_db_bytes()
        encrypted = bool(password)
        if encrypted:
            data = backup_crypto.encrypt_bytes(data, password)
        try:
            name = self._save_backup_to_server(data, encrypted)
        except OSError as e:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg=f'Could not save the backup on the server ({e.strerror or e}).'),
                status=500))
        lock = ' It is password-protected — you will need that password to restore it.' if encrypted else ''
        return self.send(*html_response(self.pg_settings(
            user_id, success=True,
            msg=f'Backup saved on the server as {name}.{lock} '
                f'The latest {MAX_SERVER_BACKUPS} server backups are kept.')))

    # ── DOWNLOAD A BACKUP THAT LIVES ON THE SERVER ───────────
    def _settings_server_backup_download(self, user_id, name):
        if not self._is_admin(user_id) or not _BACKUP_NAME_RE.match(name):
            return redirect('/settings')
        path = os.path.join(BACKUP_DIR, name)
        if not os.path.isfile(path):
            return redirect('/settings')
        with open(path, 'rb') as f:
            return file_response(
                f.read(), name,
                'application/octet-stream' if name.endswith('.enc') else 'application/x-sqlite3')

    # ── DELETE A BACKUP THAT LIVES ON THE SERVER ─────────────
    def handle_settings_server_backup_delete(self, form, user_id, name):
        if not self._is_admin(user_id):
            return self.send(*redirect('/settings'))
        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg='Your session expired — please try again.'),
                status=400))
        if not _BACKUP_NAME_RE.match(name):
            return self.send(*redirect('/settings'))
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
        except FileNotFoundError:
            pass
        except OSError as e:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg=f'Could not delete {name} ({e.strerror or e}).'),
                status=500))
        return self.send(*html_response(
            self.pg_settings(user_id, success=True, msg=f'Deleted server backup {name}.')))

    # ── DOWNLOAD: EXPORT ALL DATA ────────────────────────────
    def _settings_export(self, user_id):
        if not self._is_admin(user_id):
            return redirect('/settings')
        payload = json.dumps(self._export_all_data(), indent=2, default=str).encode('utf-8')
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return file_response(payload, f'aypee_data_export_{stamp}.json', 'application/json')

    # ── UPLOAD: RESTORE DATABASE FROM BACKUP ─────────────────
    def handle_settings_restore(self, form, user_id):
        """Replace the live database with an uploaded .db backup — the
        counterpart to _settings_backup's download. Admin-only, since it
        overwrites every table for every user."""
        if not self._is_admin(user_id):
            return self.send(*redirect('/settings'))

        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg='Your session expired — please try again.'),
                status=400))

        upload = form.get('db_file')
        if not isinstance(upload, UploadedFile) or not upload.data:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg='Please choose a .db backup file to upload.')))

        if len(upload.data) > MAX_RESTORE_BYTES:
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg='That file is too large to be a valid backup.')))

        data = upload.data
        if backup_crypto.is_encrypted(data):
            password = form.get('db_password', '') or ''
            if not password:
                return self.send(*html_response(
                    self.pg_settings(user_id, err=True,
                                      msg='This backup is password-protected — enter the '
                                          'password that was set when it was exported.')))
            try:
                data = backup_crypto.decrypt_bytes(data, password)
            except ValueError as e:
                return self.send(*html_response(
                    self.pg_settings(user_id, err=True, msg=str(e))))

        if not data.startswith(b'SQLite format 3\x00'):
            return self.send(*html_response(
                self.pg_settings(user_id, err=True,
                                  msg="That file doesn't look like a valid database "
                                      "backup (.db) — please choose the file you "
                                      "originally downloaded from Download Backup.")))

        ok, msg = self._restore_db_from_bytes(data)
        return self.send(*html_response(
            self.pg_settings(user_id, err=not ok, success=ok, msg=msg)))

    @staticmethod
    def _restore_db_from_bytes(data):
        """Validate an uploaded backup, then atomically swap it in as the
        live database. A safety copy of the outgoing database is kept on
        disk first, so a mistaken or unwanted restore can still be undone
        by an admin with file access to the server, even though there's
        no in-app undo. Returns (ok, message)."""
        fd, tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        try:
            with open(tmp_path, 'wb') as f:
                f.write(data)

            try:
                check = sqlite3.connect(tmp_path)
                try:
                    tables = {r[0] for r in check.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                finally:
                    check.close()
            except sqlite3.DatabaseError:
                return False, 'That file is not a valid SQLite database.'

            required = {'users', 'customers', 'items', 'quotations'}
            if not required.issubset(tables):
                return False, ("That backup doesn't look like an APQMS database — "
                                "it's missing expected tables.")

            # Everything below replaces the live file, so it happens under a
            # lock other workers and other requests can't be inside at the
            # same time, and only once nothing in this process still has the
            # database open.
            #
            # That matters because an open connection is not disturbed by
            # os.replace(): it keeps writing to the inode it opened, which is
            # now unlinked. Those writes are not an error and not a rollback —
            # they land on a file nothing will ever read again, so a quotation
            # saved during a restore simply disappears while the save reports
            # success.
            try:
                swap = cross_process_lock(DB_PATH, timeout=10)
            except (OSError, StoreLockTimeout):
                return False, ('Another restore is in progress — try again '
                               'in a moment.')
            with swap:
                # Fold the WAL back into the main file first, so both the
                # safety copy below and the file being replaced are complete
                # on their own rather than "main file + sidecar".
                try:
                    src = get_db()
                    try:
                        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        src.close()
                except sqlite3.Error:
                    pass  # best-effort; the drain check below is the real guard

                if not wait_for_connections_to_close(timeout=5.0):
                    return False, ('Other requests are still using the '
                                   'database right now — nothing was '
                                   'restored. Please try again in a second.')

                # Then prove no *other process* is mid-write, using SQLite's
                # own write lock: the drain above can only see this process,
                # and on Windows a held handle would make the rename below
                # fail outright, so the lock is taken purely as a probe and
                # released immediately before the swap.
                probe = None
                try:
                    probe = sqlite3.connect(DB_PATH, timeout=2.0)
                    probe.isolation_level = None
                    probe.execute('BEGIN IMMEDIATE')
                except sqlite3.Error:
                    return False, ('Something is still writing to the database '
                                   '— nothing was restored. Please try again.')
                finally:
                    if probe is not None:
                        try:
                            probe.rollback()
                        except sqlite3.Error:
                            pass
                        try:
                            probe.close()
                        except sqlite3.Error:
                            pass

                # Safety copy of the current live database before it's
                # overwritten, kept alongside it on the server.
                if os.path.exists(DB_PATH):
                    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    pre_restore_path = os.path.join(
                        os.path.dirname(os.path.abspath(DB_PATH)),
                        f'pre_restore_backup_{stamp}.db')
                    try:
                        shutil.copy2(DB_PATH, pre_restore_path)
                    except OSError:
                        pass  # best-effort — never block the restore on this

                # Drop stale WAL/SHM sidecar files from the outgoing
                # database so they can't get paired with the replacement
                # file.
                for suffix in ('-wal', '-shm'):
                    sidecar = DB_PATH + suffix
                    if os.path.exists(sidecar):
                        try:
                            os.remove(sidecar)
                        except OSError:
                            pass

                try:
                    os.replace(tmp_path, DB_PATH)
                    tmp_path = None  # already moved into place
                except OSError as e:
                    return False, (f'Could not replace the database file '
                                   f'({e.strerror or e}) — nothing was restored.')

            # Apply schema migrations in case the uploaded backup predates
            # a column/table the running app now expects (a no-op if the
            # backup is already current).
            try:
                init_db()
            except Exception:
                pass

            # Sessions live in a file, users live in rows: after swapping the
            # database every cookie already handed out still names an id, and
            # that id can now belong to somebody else — including an admin.
            # Sign everyone out rather than letting stale sessions resolve
            # against the restored accounts.
            signed_out = clear_all_sessions()
            return True, ('Database restored from backup. All data now '
                          'reflects the uploaded file'
                          + (f'; {signed_out} active session(s) were signed out.'
                             if signed_out else '.'))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    # ── USER MANAGEMENT ACTIONS (admin only) ─────────────────
    def _toggle_user_status(self, user_id, target_id):
        """Flip a user's active/inactive flag. Admins can't deactivate
        their own account — that would lock them out with no one left
        to flip it back."""
        if not self._is_admin(user_id):
            return redirect('/settings')
        if target_id == user_id:
            return redirect('/settings')
        db = get_db()
        try:
            db.execute("UPDATE users SET is_active = 1 - COALESCE(is_active, 1) WHERE id=?",
                       (target_id,))
            db.commit()
            now_active = db.execute("SELECT COALESCE(is_active, 1) a FROM users WHERE id=?",
                                    (target_id,)).fetchone()
        finally:
            db.close()
        # Deactivating has to also end the sessions already issued to that
        # account. They live in the session store, not the users table, and
        # nothing compared the two — so the flag changed, the login form
        # started refusing them, and every tab already open carried on
        # working for the rest of that session's 7-day life.
        if now_active is not None and not now_active['a']:
            invalidate_user_sessions(target_id)
        return redirect('/settings')

    def _delete_user(self, user_id, target_id):
        """Remove an account entirely. Blocked for: deleting yourself,
        and deleting the last remaining admin (the system would have no
        one left who can manage users)."""
        if not self._is_admin(user_id):
            return redirect('/settings')
        if target_id == user_id:
            return redirect('/settings')
        db = get_db()
        try:
            target = db.execute("SELECT role FROM users WHERE id=?", (target_id,)).fetchone()
            if target and target['role'] == 'admin':
                admin_count = db.execute(
                    "SELECT COUNT(*) c FROM users WHERE role='admin'").fetchone()['c']
                if admin_count <= 1:
                    return redirect('/settings')
            db.execute("DELETE FROM users WHERE id=?", (target_id,))
            db.commit()
        finally:
            db.close()
        # Same reason as above, and worse here: the account is gone from
        # the database but its session survives, so a deleted user keeps
        # signed-in access as a row that no longer exists — the pages just
        # keep trusting the stored user_id/role.
        invalidate_user_sessions(target_id)
        return redirect('/settings')

    # ── HELPERS ──────────────────────────────────────────────
    def _is_admin(self, user_id):
        db = get_db()
        try:
            row = db.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        finally:
            db.close()
        return bool(row) and row['role'] == 'admin'

    @staticmethod
    def _backup_db_bytes():
        """A consistent point-in-time copy of the live database, taken via
        SQLite's own backup API rather than a raw file copy. That matters
        because the app runs in WAL mode (see database.get_db) — a plain
        `cp`/read of quotation.db while a write is mid-flight can capture
        an inconsistent snapshot; conn.backup() does not have that problem."""
        fd, tmp_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        src = dst = None
        try:
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(tmp_path)
            with dst:
                src.backup(dst)
            dst.close()
            dst = None
            src.close()
            src = None
            with open(tmp_path, 'rb') as f:
                return f.read()
        finally:
            # Both handles closed even when backup() itself raises — a leak
            # here is not harmless, because a lingering connection is exactly
            # what makes a later restore refuse to run.
            for conn in (dst, src):
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
            os.remove(tmp_path)

    @staticmethod
    def _save_backup_to_server(data, encrypted=False):
        """Write a backup snapshot into BACKUP_DIR (atomically, owner-only
        permissions, since it contains password hashes) and prune the
        oldest ones beyond MAX_SERVER_BACKUPS. Returns the filename."""
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        ext = '.db.enc' if encrypted else '.db'
        fd, tmp_path = tempfile.mkstemp(dir=BACKUP_DIR, suffix='.tmp')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        # The name is *reserved* rather than tested for. A
        # `while os.path.exists(name): name = ...` loop only reads the
        # directory and then writes separately, so two backups saved in the
        # same second could both choose the same name and the second rename
        # would silently destroy the first — losing a backup the admin
        # believes they have. O_EXCL makes "nobody has this name" and "this
        # name is mine" a single atomic statement.
        name = None
        for n in range(1, 1000):
            candidate = (f'aypee_backup_{stamp}{ext}' if n == 1
                         else f'aypee_backup_{stamp}_{n}{ext}')
            final_path = os.path.join(BACKUP_DIR, candidate)
            try:
                reserve = os.open(final_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            except OSError:
                break
            os.close(reserve)
            try:
                os.replace(tmp_path, final_path)
            except OSError:
                try:
                    os.remove(final_path)
                except OSError:
                    pass
                raise
            tmp_path = None
            name = candidate
            break
        if name is None:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise OSError('Could not reserve a filename for the server backup')

        for old in SettingsPagesMixin._list_server_backups()[MAX_SERVER_BACKUPS:]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old['name']))
            except OSError:
                pass
        return name

    @staticmethod
    def _list_server_backups():
        """Backups stored in BACKUP_DIR, newest first, as
        [{'name', 'size', 'mtime'}]. Empty if the folder doesn't exist."""
        out = []
        try:
            entries = os.listdir(BACKUP_DIR)
        except OSError:
            return out
        for name in entries:
            if not _BACKUP_NAME_RE.match(name):
                continue
            try:
                st = os.stat(os.path.join(BACKUP_DIR, name))
            except OSError:
                continue
            out.append({'name': name, 'size': st.st_size, 'mtime': st.st_mtime})
        out.sort(key=lambda b: (b['mtime'], b['name']), reverse=True)
        return out

    @staticmethod
    def _export_all_data():
        """Every business table's rows as plain dicts, for a full JSON
        export of the system's data — independent of the raw .db file, so
        it's readable/diffable/importable without SQLite tooling. Password
        hashes are deliberately dropped from the users table so the export
        is safe to hand off or store without also leaking credentials."""
        db = get_db()
        try:
            tables = [
                'customers', 'items', 'categories', 'item_categories',
                'customer_items', 'quotations', 'quotation_items',
                'proforma_invoices', 'proforma_invoice_items',
                'packing_lists', 'packing_list_items', 'packing_list_bags',
                'employees', 'attendance',
            ]
            out = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}").fetchall()]
                   for t in tables}
            out['users'] = [
                {k: v for k, v in dict(r).items() if k != 'password'}
                for r in db.execute("SELECT * FROM users").fetchall()
            ]
            out['_exported_at'] = datetime.now().isoformat()
            return out
        finally:
            db.close()

    # ── PAGE: SETTINGS ───────────────────────────────────────
    def pg_settings(self, user_id, err=False, success=False, msg=''):
        db = get_db()
        user = db.execute(
            "SELECT username, full_name, role, email, phone FROM users WHERE id=?",
            (user_id,)).fetchone()
        counts = {}
        for label, table in (
            ('Customers', 'customers'), ('Items', 'items'),
            ('Quotations', 'quotations'), ('Proforma Invoices', 'proforma_invoices'),
            ('Packing Lists', 'packing_lists'), ('Employees', 'employees'),
        ):
            counts[label] = db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()['c']

        full_name = user['full_name'] if user else ''
        username = user['username'] if user else ''
        role = user['role'] if user else 'user'
        email = user['email'] if user else ''
        phone = user['phone'] if user else ''
        is_admin = role == 'admin'

        all_users = []
        if is_admin:
            all_users = db.execute(
                "SELECT id, username, full_name, role, COALESCE(is_active, 1) AS is_active "
                "FROM users ORDER BY full_name COLLATE NOCASE").fetchall()
            admin_count = sum(1 for u in all_users if u['role'] == 'admin')
        db.close()

        db_size = _fmt_bytes(os.path.getsize(DB_PATH)) if os.path.exists(DB_PATH) else 'n/a'

        if success:
            alert = ('<div class="alert alert-success py-2 mb-3"><i class="bi '
                     'bi-check-circle"></i> ' + esc(msg or 'Saved.') + '</div>')
        elif err:
            alert = ('<div class="alert alert-danger py-2 mb-3"><i class="bi '
                     'bi-exclamation-circle"></i> ' + esc(msg or 'Something went wrong.')
                     + '</div>')
        else:
            alert = ''

        # One token per form, not one for the whole page: these tokens are
        # single-use and consumed on submit, so a shared token meant the
        # first form submitted worked and every other form on the same page
        # load came back "Your session expired" — including reloading the
        # page with the other form still filled in.
        profile_csrf_token = new_csrf_token()
        backup_csrf_token = new_csrf_token()
        restore_csrf_token = new_csrf_token()

        # ── Stats row (icon + number + label per item) ──
        stat_icons = {
            'Customers': 'bi-people', 'Items': 'bi-box-seam',
            'Quotations': 'bi-file-earmark-text', 'Proforma Invoices': 'bi-receipt',
            'Packing Lists': 'bi-box-seam', 'Employees': 'bi-people-fill',
        }
        stats_html = ''.join(
            f'<div class="set-stat">'
            f'<i class="bi {stat_icons.get(label, "bi-dot")}"></i>'
            f'<div><strong>{count}</strong><span>{esc(label)}</span></div>'
            f'</div>'
            for label, count in counts.items()
        )

        # ── Server-side backups (admin only) ──
        server_backups = self._list_server_backups() if is_admin else []
        if server_backups:
            last_server_copy = datetime.fromtimestamp(
                server_backups[0]['mtime']).strftime('%d %b %Y, %I:%M %p')
        else:
            last_server_copy = 'Never'

        server_rows = ''
        for b in server_backups[:10]:
            when = datetime.fromtimestamp(b['mtime']).strftime('%d %b %Y, %I:%M %p')
            server_rows += (
                f'<tr><td><i class="bi {"bi-file-earmark-lock2" if b["name"].endswith(".enc") else "bi-file-earmark-binary"} text-muted"></i> '
                f'<code>{esc(b["name"])}</code></td>'
                f'<td class="text-muted">{when}</td>'
                f'<td class="text-muted">{_fmt_bytes(b["size"])}</td>'
                f'<td class="text-end text-nowrap">'
                f'<a class="btn btn-outline-secondary btn-sm" href="/settings/backup/server/{esc(b["name"])}" '
                f'title="Download this backup"><i class="bi bi-download"></i></a> '
                f'<form method="POST" action="/settings/backup/server/{esc(b["name"])}/delete" class="d-inline" '
                f'onsubmit="return confirm(\'Delete this server backup permanently?\')">'
                f'<input type="hidden" name="csrf_token" value="{esc(new_csrf_token())}">'
                f'<button type="submit" class="btn btn-outline-danger btn-sm" title="Delete this backup">'
                f'<i class="bi bi-trash"></i></button></form></td></tr>')
        if server_rows:
            more = (f'<div class="text-muted small mt-1">Showing the latest 10 of {len(server_backups)}.</div>'
                    if len(server_backups) > 10 else '')
            server_list_html = f'''
  <hr class="set-divider">
  <div class="set-row-title mb-1">Backups stored on this server</div>
  <div class="set-row-sub mb-2">Kept in <code>{esc(BACKUP_DIR)}</code> — the newest {MAX_SERVER_BACKUPS} are retained automatically.</div>
  <div class="table-responsive-wrapper">
  <table class="table table-sm mb-0">
    <thead><tr><th>File</th><th>Saved</th><th>Size</th><th class="text-end">Actions</th></tr></thead>
    <tbody>{server_rows}</tbody>
  </table>
  </div>{more}'''
        else:
            server_list_html = ''

        # ── User Management table (admin only) ──
        if is_admin:
            rows_html = ''
            for u in all_users:
                u_initial = esc((u['full_name'] or u['username'] or 'U').strip()[:1].upper() or 'U')
                role_badge = (f'<span class="badge badge-role-admin">Admin</span>' if u['role'] == 'admin'
                              else f'<span class="badge badge-role-user">User</span>')
                active = bool(u['is_active'])
                status_badge = (f'<span class="badge badge-status-active">Active</span>' if active
                                else f'<span class="badge badge-status-inactive">Inactive</span>')
                is_self = u['id'] == user_id
                is_only_admin = u['role'] == 'admin' and admin_count <= 1
                if is_self:
                    actions = '<span class="text-muted small">You</span>'
                else:
                    toggle_label = 'Deactivate' if active else 'Activate'
                    toggle_icon = 'bi-pause-circle' if active else 'bi-play-circle'
                    delete_item = ''
                    if not is_only_admin:
                        delete_item = (
                            f'<li><form method="POST" action="/settings/users/{u["id"]}/delete" '
                            f'onsubmit="return confirm(\'Delete user {esc(u["username"])} permanently?\')">'
                            f'<button type="submit" class="dropdown-item text-danger">'
                            f'<i class="bi bi-trash"></i> Delete</button></form></li>')
                    actions = (
                        f'<div class="dropdown">'
                        f'<button class="row-action-btn" type="button" data-bs-toggle="dropdown" '
                        f'aria-expanded="false" title="Actions"><i class="bi bi-three-dots"></i></button>'
                        f'<ul class="dropdown-menu dropdown-menu-end">'
                        f'<li><form method="POST" action="/settings/users/{u["id"]}/toggle-status">'
                        f'<button type="submit" class="dropdown-item">'
                        f'<i class="bi {toggle_icon}"></i> {toggle_label}</button></form></li>'
                        f'{delete_item}'
                        f'</ul></div>')
                rows_html += (
                    f'<tr>'
                    f'<td><div class="d-flex align-items-center gap-2">'
                    f'<span class="avatar-circle" style="width:32px;height:32px;font-size:13px">{u_initial}</span>'
                    f'<span class="fw-semibold">{esc(u["full_name"])}</span></div></td>'
                    f'<td class="text-muted">{esc(u["username"])}</td>'
                    f'<td>{role_badge}</td>'
                    f'<td>{status_badge}</td>'
                    f'<td class="text-end">{actions}</td>'
                    f'</tr>')

            user_mgmt_html = f'''
<div class="form-section set-card">
  <div class="set-card-head-row">
    <div class="set-card-head">
      <div class="set-card-icon"><i class="bi bi-people"></i></div>
      <div>
        <h5>User Management</h5>
        <p>Manage who can sign in to this system.</p>
      </div>
    </div>
    <a href="/register" class="btn btn-outline-secondary btn-sm"><i class="bi bi-plus-lg"></i> Add User</a>
  </div>
  <div class="table-responsive-wrapper">
  <table class="table mb-0">
    <thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Status</th><th class="text-end">Actions</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  </div>
</div>'''

            backup_section = f'''
<div class="form-section set-card">
  <div class="set-card-head-row">
    <div class="set-card-head">
      <div class="set-card-icon"><i class="bi bi-hdd"></i></div>
      <div>
        <h5>Database Backup</h5>
        <p>Download a full copy of the live database, or keep one on the server.</p>
      </div>
    </div>
    <div class="set-size-badge"><strong>{db_size}</strong><span>Current size</span></div>
  </div>
  <div class="info-box">
    <i class="bi bi-info-circle"></i>
    <p class="mb-0">Download a full, point-in-time copy of the live database
    (<code>{esc(os.path.basename(DB_PATH))}</code>). You can also keep a copy on this server, which is
    handy for quick roll-backs — but it sits on the same disk as the live database, so still keep a
    downloaded copy somewhere off this server.</p>
  </div>
  <div class="set-stats-row">{stats_html}</div>
  <form method="POST" action="/settings/backup" id="backupForm">
    <input type="hidden" name="csrf_token" value="{esc(backup_csrf_token)}">
    <div class="d-flex align-items-center justify-content-between flex-wrap gap-2">
      <div class="d-flex flex-wrap gap-2">
        <button type="submit" class="btn btn-primary" id="backupDownloadBtn">
          <i class="bi bi-download"></i> Download Backup (.db)</button>
        <button type="submit" formaction="/settings/backup/server" class="btn btn-outline-primary">
          <i class="bi bi-hdd-network"></i> Save on Server</button>
      </div>
      <span class="text-muted small">Last server copy: {last_server_copy}</span>
    </div>
    <div class="form-check mt-2">
      <input class="form-check-input" type="checkbox" name="save_on_server" value="1"
             id="saveOnServer" checked>
      <label class="form-check-label small" for="saveOnServer">
        Also keep a copy on the server when I download</label>
    </div>
    <div class="row g-2 mt-1" style="max-width:600px;">
      <div class="col-sm-6">
        <input type="password" name="backup_password" class="form-control form-control-sm"
               placeholder="Backup password (optional)" autocomplete="new-password"
               minlength="{backup_crypto.MIN_PASSWORD_LEN}">
      </div>
      <div class="col-sm-6">
        <input type="password" name="backup_password_confirm" class="form-control form-control-sm"
               placeholder="Confirm password" autocomplete="new-password">
      </div>
    </div>
    <div class="form-text"><i class="bi bi-lock"></i> Enter a password to encrypt the backup
      (saved as <code>.db.enc</code>) — this applies to the download <em>and</em> the server copy.
      The same password is needed to restore it, and it <strong>cannot be recovered</strong> if lost.
      Leave blank for a normal unencrypted <code>.db</code>.</div>
  </form>
  <script>
  // A file download doesn't navigate away, so this page would keep a used-up
  // form token and a stale server-backup list. Reload /settings shortly after
  // a download so both are fresh. ("Save on Server" re-renders the page itself.)
  document.getElementById('backupForm').addEventListener('submit', function (e) {{
    if (e.submitter && e.submitter.id === 'backupDownloadBtn') {{
      setTimeout(function () {{ window.location.href = '/settings'; }}, 1800);
    }}
  }});
  </script>
{server_list_html}

  <hr class="set-divider">

  <div class="set-row" style="align-items:flex-start;">
    <div class="set-row-icon"><i class="bi bi-upload"></i></div>
    <div class="set-row-text" style="flex:1 1 100%;">
      <div class="set-row-title">Restore from Backup</div>
      <div class="set-row-sub">Upload a previously downloaded <code>.db</code> or password-protected <code>.db.enc</code> file to bring the
      system back to that point in time — same customers, items, quotations and everything else it had.</div>
      <form method="POST" action="/settings/restore" enctype="multipart/form-data" class="mt-2"
            onsubmit="return confirm('This will REPLACE all current data with the uploaded backup. A safety copy of what\'s on the server now will be kept, but there is no in-app undo. Continue?');">
        <input type="hidden" name="csrf_token" value="{esc(restore_csrf_token)}">
        <div class="d-flex flex-wrap gap-2 align-items-center">
          <input type="file" name="db_file" accept=".db,.enc" required
                 class="form-control form-control-sm" style="max-width:280px;">
          <input type="password" name="db_password" autocomplete="new-password"
                 placeholder="Backup password (if encrypted)"
                 class="form-control form-control-sm" style="max-width:240px;">
          <button type="submit" class="btn btn-outline-danger btn-sm">
            <i class="bi bi-upload"></i> Restore Backup</button>
        </div>
      </form>
    </div>
  </div>
</div>

<div class="form-section set-card">
  <div class="set-card-head">
    <div class="set-card-icon"><i class="bi bi-file-earmark-arrow-down"></i></div>
    <div>
      <h5>Export All Data</h5>
      <p>Export all records as a single JSON file.</p>
    </div>
  </div>
  <div class="info-box">
    <i class="bi bi-info-circle"></i>
    <p class="mb-0">Export every customer, item, quotation, proforma invoice, packing list and
    employee record as a single readable JSON file — handy for migrating to a new server or
    archiving a snapshot.</p>
  </div>
  <a href="/settings/export" class="btn btn-outline-primary"><i class="bi bi-download"></i> Export All Data (.json)</a>
</div>

{user_mgmt_html}'''
        else:
            backup_section = '''
<div class="form-section set-card">
  <div class="set-card-head">
    <div class="set-card-icon"><i class="bi bi-hdd"></i></div>
    <div>
      <h5>Database Backup &amp; Export</h5>
      <p class="mb-0">Only administrators can download database backups or export
      system-wide data. Contact an admin if you need a copy.</p>
    </div>
  </div>
</div>'''

        content = f'''
<style>
.set-page-head {{ display:flex; gap:14px; align-items:flex-start; margin-bottom:20px; }}
.set-page-head-icon {{
  width:46px; height:46px; border-radius:12px; background:var(--primary-bg); color:var(--primary);
  display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0;
}}
.set-page-head h1 {{ font-size:22px; font-weight:700; color:#0F172A; margin:0; }}
.set-page-head p {{ font-size:13px; color:#64748B; margin:2px 0 0; }}

.set-card {{ margin-bottom:24px; }}
.set-card:last-child {{ margin-bottom:0; }}
.set-card-head {{ display:flex; align-items:flex-start; gap:14px; margin-bottom:20px; }}
.set-card-head-row {{
  display:flex; align-items:flex-start; justify-content:space-between; gap:14px;
  flex-wrap:wrap; margin-bottom:20px;
}}
.set-card-icon {{
  width:44px; height:44px; border-radius:12px; background:var(--primary-bg); color:var(--primary);
  display:flex; align-items:center; justify-content:center; font-size:19px; flex-shrink:0;
}}
.set-card-icon.blue {{ background:#EFF6FF; color:#2563EB; }}
.set-card-head h5 {{ font-size:16px; font-weight:700; color:#0F172A; margin:0 0 2px; }}
.set-card-head p {{ font-size:12.5px; color:#64748B; margin:0; }}

.set-size-badge {{
  background:#ECFDF5; color:#047857; border:1px solid #A7F3D0; border-radius:10px;
  padding:6px 16px; text-align:right; flex-shrink:0;
}}
.set-size-badge strong {{ display:block; font-size:15px; font-weight:700; line-height:1.3; }}
.set-size-badge span {{ font-size:10.5px; opacity:.85; }}

.set-stats-row {{ display:flex; flex-wrap:wrap; gap:22px; margin:18px 0; }}
.set-stat {{ display:flex; align-items:center; gap:10px; }}
.set-stat i {{ font-size:18px; color:#CBD5E1; }}
.set-stat strong {{ display:block; font-size:18px; font-weight:700; color:#0F172A; line-height:1.2; }}
.set-stat span {{ font-size:11.5px; color:#64748B; }}

.set-divider {{ margin:24px 0 18px; border-color:#E2E8F0; }}
.set-row {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; }}
.set-row-icon {{
  width:40px; height:40px; border-radius:10px; background:#EFF6FF; color:#2563EB;
  display:flex; align-items:center; justify-content:center; font-size:16px; flex-shrink:0;
}}
.set-row-text {{ flex:1 1 auto; min-width:0; }}
.set-row-title {{ font-weight:700; font-size:14px; color:#0F172A; }}
.set-row-sub {{ font-size:12px; color:#64748B; }}

.set-field .input-group-text {{ background:#F8FAFC; color:#94A3B8; }}

.set-tip {{
  background:#EFF6FF; border:1px solid #DBEAFE; border-radius:var(--radius);
  padding:16px 20px; margin-top:24px; display:flex; gap:14px; align-items:flex-start;
}}
.set-tip-icon {{
  width:32px; height:32px; border-radius:50%; background:#3B82F6; color:#fff; flex-shrink:0;
  display:flex; align-items:center; justify-content:center; font-size:14px;
}}
.set-tip strong {{ font-weight:700; color:#0F172A; }}
.set-tip span {{ font-size:13px; color:#334155; }}
</style>

<div class="set-page-head">
  <div class="set-page-head-icon"><i class="bi bi-gear"></i></div>
  <div>
    <h1>Settings</h1>
    <p>Manage your profile, data, and system preferences.</p>
  </div>
</div>

{alert}
<div class="row g-4">
  <div class="col-md-5">
    <div class="form-section set-card">
      <div class="set-card-head">
        <div class="set-card-icon"><i class="bi bi-person"></i></div>
        <div>
          <h5>My Profile</h5>
          <p>Update your personal information</p>
        </div>
      </div>
      <form method="POST" action="/settings/profile">
        <input type="hidden" name="csrf_token" value="{esc(profile_csrf_token)}">
        <div class="mb-3">
          <label class="form-label">Full Name</label>
          <div class="input-group set-field">
            <span class="input-group-text"><i class="bi bi-person"></i></span>
            <input name="full_name" class="form-control" required maxlength="100" value="{esc(full_name)}">
          </div>
        </div>
        <div class="mb-3">
          <label class="form-label">Username</label>
          <div class="input-group set-field">
            <span class="input-group-text"><i class="bi bi-at"></i></span>
            <input class="form-control" value="{esc(username)}" disabled>
          </div>
          <div class="form-text">Contact an admin to change your username.</div>
        </div>
        <div class="mb-3">
          <label class="form-label">Email (Optional)</label>
          <div class="input-group set-field">
            <span class="input-group-text"><i class="bi bi-envelope"></i></span>
            <input type="email" name="email" class="form-control" maxlength="150"
                   placeholder="Enter your email address" value="{esc(email or "")}">
          </div>
        </div>
        <div class="mb-3">
          <label class="form-label">Phone (Optional)</label>
          <div class="input-group set-field">
            <span class="input-group-text"><i class="bi bi-telephone"></i></span>
            <input type="tel" name="phone" class="form-control" maxlength="30"
                   placeholder="Enter your contact number" value="{esc(phone or "")}">
          </div>
        </div>
        <div class="d-flex gap-2">
          <button class="btn btn-primary" title="Save (Ctrl+S)"><i class="bi bi-check-circle"></i> Save Changes</button>
          <button type="reset" class="btn btn-outline-secondary"><i class="bi bi-arrow-counterclockwise"></i> Reset</button>
        </div>
      </form>

      <hr class="set-divider">

      <div class="set-row">
        <div class="set-row-icon"><i class="bi bi-lock"></i></div>
        <div class="set-row-text">
          <div class="set-row-title">Change Password</div>
          <div class="set-row-sub">Keep your account secure with a strong password.</div>
        </div>
        <a href="/change-password" class="btn btn-outline-secondary btn-sm"><i class="bi bi-key"></i> Change Password</a>
      </div>
    </div>
  </div>
  <div class="col-md-7">
    {backup_section}
  </div>
</div>

<div class="set-tip">
  <div class="set-tip-icon"><i class="bi bi-info-lg"></i></div>
  <div><strong>Tip:</strong> <span>Keep your data backed up regularly and restrict admin access to trusted users only.</span></div>
</div>'''
        return self._layout('Settings', 'Settings', content, 'settings')
