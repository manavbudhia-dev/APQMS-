from utils.http_utils import (
    redirect,
    html_response,
    create_session,
    destroy_session,
    session_cookie_header,
    logout_cookie_header,
    new_csrf_token,
    consume_csrf_token,
    is_locked_out,
    record_failed_login,
    clear_failed_logins,
    invalidate_user_sessions,
    sid_from_cookie,
    esc,
)
from database import get_db
from database import hash_password
from database import verify_password_or_dummy


class AuthPagesMixin:
    # ── FORM HANDLERS ────────────────────────────────────────
    def handle_login(self, form):
        username = form.get('username', '').strip()
        password = form.get('password', '')
        ip = self.client_address[0] if getattr(self, 'client_address', None) else ''

        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_login(err=True, msg='Your session expired — please try again.'),
                status=400))

        remaining = is_locked_out(username, ip)
        if remaining:
            minutes = max(1, remaining // 60)
            return self.send(*html_response(
                self.pg_login(err=True,
                               msg=f'Too many failed attempts. Try again in about {minutes} minute(s).'),
                status=429))

        db = get_db()
        try:
            user = db.execute("SELECT * FROM users WHERE username=?",
                              (username,)).fetchone()
        finally:
            db.close()
        stored_hash = user['password'] if user else None
        # A PBKDF2 check is always run, even for a username that doesn't
        # exist (see verify_password_or_dummy), so response timing can't be
        # used to enumerate accounts. A deactivated account is verified and
        # *then* refused for the same reason — refusing on the password
        # check alone used to let "Deactivate" be a purely cosmetic badge on
        # the Settings table while the account carried on signing in.
        password_ok = bool(user) and verify_password_or_dummy(stored_hash, password)
        is_active = user is None or user['is_active'] is None or bool(user['is_active'])
        if password_ok and not is_active:
            password_ok = False
        if password_ok:
            clear_failed_logins(username, ip)
            sid = create_session({
                'user_id': user['id'],
                'username': user['username'],
                'full_name': user['full_name'],
                'role': user['role'],
            })
            self.send_response(302)
            self.send_header('Location', '/dashboard')
            self.send_header('Set-Cookie', session_cookie_header(sid))
            self.end_headers()
        else:
            record_failed_login(username, ip)
            self.send(*html_response(self.pg_login(err=True)))


    def handle_logout(self):
        """Log the current user out. Wire this up to e.g. POST /logout."""
        destroy_session(self.headers.get('Cookie'))
        self.send_response(302)
        self.send_header('Location', '/login')
        self.send_header('Set-Cookie', logout_cookie_header())
        self.end_headers()


    def handle_register(self, form):
        """Create a new user account. This is an admin-only, in-app
        action (see routing: /register requires an admin session) — there
        is no public sign-up page, so accounts only ever get made by
        someone who already has access handing it out deliberately."""
        username = form.get('username', '').strip()
        password = form.get('password', '')
        full_name = form.get('full_name', '').strip()
        role = form.get('role', 'user').strip()
        if role not in ('user', 'admin'):
            role = 'user'

        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_register(err=True, msg='Your session expired — please try again.'),
                status=400))

        if not username or not password or not full_name:
            return self.send(*html_response(
                self.pg_register(err=True, msg='Please fill all fields')))

        if not (3 <= len(username) <= 50):
            return self.send(*html_response(
                self.pg_register(err=True, msg='Username must be 3-50 characters')))

        pw_error = self._password_policy_error(password)
        if pw_error:
            return self.send(*html_response(self.pg_register(err=True, msg=pw_error)))

        db = get_db()
        try:
            db.execute("INSERT INTO users (username, password, full_name, role) "
                       "VALUES (?,?,?,?)",
                       (username, hash_password(password), full_name, role))
            db.commit()
        except Exception:
            db.close()
            return self.send(*html_response(
                self.pg_register(err=True, msg='Username already exists')))
        db.close()
        return self.send(*html_response(
            self.pg_register(success=True, msg=f'Account "{username}" created.')))


    def handle_change_password(self, form, user_id):
        """Self-service password change, available to any logged-in user
        (including admin) via /change-password."""
        current = form.get('current_password', '')
        new = form.get('new_password', '')
        confirm = form.get('confirm_password', '')

        if not consume_csrf_token(form.get('csrf_token', '')):
            return self.send(*html_response(
                self.pg_change_password(err=True, msg='Your session expired — please try again.'),
                status=400))

        db = get_db()
        try:
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            stored_hash = user['password'] if user else None
            if not user or not verify_password_or_dummy(stored_hash, current):
                return self.send(*html_response(
                    self.pg_change_password(err=True, msg='Current password is incorrect')))

            if new != confirm:
                return self.send(*html_response(
                    self.pg_change_password(err=True, msg='New passwords do not match')))

            pw_error = self._password_policy_error(new)
            if pw_error:
                return self.send(*html_response(self.pg_change_password(err=True, msg=pw_error)))

            db.execute("UPDATE users SET password=? WHERE id=?", (hash_password(new), user_id))
            db.commit()
        finally:
            db.close()
        # Every *other* session for this account is ended. Changing a
        # password because you think someone else knows it only helps if it
        # actually signs them out, and sessions live in the session store,
        # which the password change otherwise never touched. The session
        # that just submitted the change is kept so the user isn't bounced.
        invalidate_user_sessions(user_id, keep_sid=sid_from_cookie(self.headers.get('Cookie')))
        return self.send(*html_response(
            self.pg_change_password(success=True, msg='Password updated.')))


    @staticmethod
    def _password_policy_error(password):
        """Minimal, no-nag password policy: long enough and not just
        letters (blocks the "aaaaaaaa" case) without being annoying."""
        if len(password) < 8:
            return 'Password must be at least 8 characters'
        if len(password) > 128:
            return 'Password is too long'
        if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
            return 'Password must contain both letters and numbers'
        return None


    # ── PAGE: LOGIN ──────────────────────────────────────────
    def pg_login(self, err=False, msg=None):
        alert = ('<div class="alert alert-danger py-2 mb-3"><i class="bi '
                 'bi-exclamation-circle"></i> ' + esc(msg or 'Invalid username or password')
                 + '</div>') if err else ''
        csrf_token = new_csrf_token()
        return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login - Aypee Quotation System</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
<link href="/static/css/style.css" rel="stylesheet">
</head>
<body>
<div class="login-page">
  <div class="login-card">
    <div class="brand">
      <div class="brand-icon">A</div>
      <h2>AYPEE INDUSTRIES</h2>
      <p>Quotation Management System</p>
    </div>
    {alert}
    <form method="POST" action="/login">
      <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
      <div class="mb-3">
        <label class="form-label">Username</label>
        <input name="username" class="form-control" required autofocus placeholder="Enter your username" maxlength="50" autocomplete="username">
      </div>
      <div class="mb-3">
        <label class="form-label">Password</label>
        <input name="password" type="password" class="form-control" required placeholder="Enter your password" maxlength="128" autocomplete="current-password">
      </div>
      <button class="btn btn-primary w-100 mb-3" title="Sign in (Ctrl+S / Enter)"><i class="bi bi-box-arrow-in-right"></i> Sign In</button>
    </form>
  </div>
</div>
</body>
</html>'''


    # ── PAGE: REGISTER (admin-only "Add User" panel, not public) ──
    def pg_register(self, err=False, msg='', success=False):
        """Renders at /register, but routing only lets an already-logged-in
        admin reach it — this is how you grant someone else access, not a
        public sign-up form."""
        if success:
            alert = ('<div class="alert alert-success py-2 mb-3"><i class="bi '
                     'bi-check-circle"></i> ' + esc(msg or 'Account created.')
                     + '</div>')
        elif err:
            alert = ('<div class="alert alert-danger py-2 mb-3"><i class="bi '
                     'bi-exclamation-circle"></i> ' + esc(msg or 'Please fill all fields')
                     + '</div>')
        else:
            alert = ''
        csrf_token = new_csrf_token()
        return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Add User - Aypee Quotation System</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
<link href="/static/css/style.css" rel="stylesheet">
</head>
<body>
<div class="login-page">
  <div class="login-card">
    <div class="brand">
      <div class="brand-icon">A</div>
      <h2>AYPEE INDUSTRIES</h2>
      <p>Give Someone Access</p>
    </div>
    {alert}
    <form method="POST" action="/register">
      <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
      <div class="mb-3">
        <label class="form-label">Full Name</label>
        <input name="full_name" class="form-control" required placeholder="Their full name" maxlength="100" autocomplete="off">
      </div>
      <div class="mb-3">
        <label class="form-label">Username</label>
        <input name="username" class="form-control" required placeholder="Choose a username for them" maxlength="50" autocomplete="off">
      </div>
      <div class="mb-3">
        <label class="form-label">Password</label>
        <div class="input-group">
          <input name="password" id="genPassword" type="text" class="form-control" required placeholder="Click Generate, or type your own" maxlength="128" autocomplete="off">
          <button type="button" class="btn btn-outline-secondary" onclick="generatePassword()"><i class="bi bi-shuffle"></i> Generate</button>
        </div>
        <div class="form-text">Shown in plain text so you can copy it and share it with them — at least 8 characters, letters + numbers.</div>
      </div>
      <div class="mb-3">
        <label class="form-label">Role</label>
        <select name="role" class="form-select">
          <option value="user" selected>User</option>
          <option value="admin">Admin (can also create accounts)</option>
        </select>
      </div>
      <button class="btn btn-primary w-100 mb-3" title="Save (Ctrl+S)"><i class="bi bi-person-plus"></i> Create Account</button>
    </form>
    <div class="text-center">
      <a href="/dashboard" style="color:#DC2626;text-decoration:none;font-weight:500">&larr; Back to Dashboard</a>
    </div>
  </div>
</div>
<script>
function generatePassword() {{
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%';
  const arr = new Uint32Array(14);
  window.crypto.getRandomValues(arr);
  let pwd = '';
  for (let i = 0; i < arr.length; i++) {{ pwd += chars[arr[i] % chars.length]; }}
  document.getElementById('genPassword').value = pwd;
}}
</script>
</body>
</html>'''


    # ── PAGE: CHANGE PASSWORD (self-service, any logged-in user) ──
    def pg_change_password(self, err=False, msg='', success=False):
        if success:
            alert = ('<div class="alert alert-success py-2 mb-3"><i class="bi '
                     'bi-check-circle"></i> ' + esc(msg or 'Password updated.')
                     + '</div>')
        elif err:
            alert = ('<div class="alert alert-danger py-2 mb-3"><i class="bi '
                     'bi-exclamation-circle"></i> ' + esc(msg or 'Please fill all fields')
                     + '</div>')
        else:
            alert = ''
        csrf_token = new_csrf_token()
        return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Change Password - Aypee Quotation System</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
<link href="/static/css/style.css" rel="stylesheet">
</head>
<body>
<div class="login-page">
  <div class="login-card">
    <div class="brand">
      <div class="brand-icon">A</div>
      <h2>AYPEE INDUSTRIES</h2>
      <p>Change Password</p>
    </div>
    {alert}
    <form method="POST" action="/change-password">
      <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
      <div class="mb-3">
        <label class="form-label">Current Password</label>
        <input name="current_password" type="password" class="form-control" required maxlength="128" autocomplete="current-password">
      </div>
      <div class="mb-3">
        <label class="form-label">New Password</label>
        <input name="new_password" type="password" class="form-control" required placeholder="At least 8 characters, letters + numbers" maxlength="128" autocomplete="new-password">
      </div>
      <div class="mb-3">
        <label class="form-label">Confirm New Password</label>
        <input name="confirm_password" type="password" class="form-control" required maxlength="128" autocomplete="new-password">
      </div>
      <button class="btn btn-primary w-100 mb-3" title="Save (Ctrl+S)"><i class="bi bi-key"></i> Update Password</button>
    </form>
    <div class="text-center">
      <a href="/dashboard" style="color:#DC2626;text-decoration:none;font-weight:500">&larr; Back to Dashboard</a>
    </div>
  </div>
</div>
</body>
</html>'''
