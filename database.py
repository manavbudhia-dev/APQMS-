"""SQLite database models and initialization for Aypee Quotation System."""
import sqlite3, os, sys, hashlib, secrets
from datetime import datetime

DB_PATH = os.environ.get('QUOTATION_DB', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quotation.db'))

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

# ── PASSWORD HASHING ──
# PBKDF2-HMAC-SHA256 with a high iteration count. Much more resistant to
# offline brute-forcing than a single unsalted/lightly-salted SHA-256
# round would be, while still needing no extra dependencies.
PBKDF2_ITERATIONS = 260_000

def hash_password(password):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${dk.hex()}"

def verify_password(stored, password):
    try:
        if stored.startswith('pbkdf2$'):
            _, iterations, salt, hash_hex = stored.split('$')
            dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations))
            return secrets.compare_digest(dk.hex(), hash_hex)
        # Legacy format ("salt:sha256hex") from older installs — verified
        # so existing users aren't locked out; they'll get a PBKDF2 hash
        # next time they set/reset their password.
        salt, h = stored.split(':')
        legacy_hash = hashlib.sha256((salt + password).encode()).hexdigest()
        return secrets.compare_digest(legacy_hash, h)
    except (ValueError, AttributeError):
        return False

# A hash of a random, never-used password. Used to run a dummy PBKDF2
# comparison when a username doesn't exist, so a login attempt takes
# roughly the same time either way and timing can't be used to enumerate
# valid usernames.
_DUMMY_HASH = hash_password(secrets.token_hex(16))

def verify_password_or_dummy(stored, password):
    if not stored:
        verify_password(_DUMMY_HASH, password)
        return False
    return verify_password(stored, password)

def _deliver_generated_admin_password(password):
    """Hand a freshly-generated admin password to the operator.

    Printing to the console is the primary channel, but if stdout isn't
    being captured or watched at deploy time (e.g. the server is started
    as a background/systemd service with output redirected to /dev/null)
    that print is lost forever and the admin account becomes unreachable.
    As a backup, also write the password to a 0600 file next to the
    database, so it can be recovered by reading the filesystem even if the
    console output was missed. The account is also always recoverable
    later via `python3 database.py reset-admin`.
    """
    print("=" * 64)
    print(" First run: created the 'admin' account with a generated password")
    print(f"   username: admin")
    print(f"   password: {password}")
    print(" Log in and change this immediately. To set your own instead,")
    print(" set ADMIN_PASSWORD in the environment before starting the")
    print(" server (and rotate/delete the account if this one leaked).")

    cred_path = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'ADMIN_CREDENTIALS.txt')
    try:
        fd = os.open(cred_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(
                "Aypee Quotation System — generated admin credentials\n"
                f"username: admin\npassword: {password}\n\n"
                "Delete this file once you have logged in and changed the\n"
                "password (or set ADMIN_PASSWORD and run:\n"
                "  python3 database.py reset-admin\n"
                "to rotate it without touching the database directly).\n"
            )
        print(f" Also saved to: {cred_path} (permissions 600)")
    except OSError as e:
        print(f" Could not write backup credentials file ({e}); the")
        print(" console output above is the only copy — save it now.")
    print("=" * 64)

def reset_admin_password(new_password=None):
    """Recovery path for a lost/never-seen admin password.

    Creates the 'admin' user if it doesn't exist yet, or resets its
    password otherwise. Pass a password explicitly, or set ADMIN_PASSWORD
    in the environment, or leave both unset to get a fresh generated one
    (delivered the same way as on first run). This is what turns "console
    output was missed" from a permanent lockout into a one-command fix.
    """
    conn = get_db()
    c = conn.cursor()
    if new_password is None:
        new_password = os.environ.get('ADMIN_PASSWORD')
    generated = new_password is None
    if generated:
        new_password = secrets.token_urlsafe(12)

    exists = c.execute("SELECT 1 FROM users WHERE username=?", ('admin',)).fetchone()
    if exists:
        c.execute("UPDATE users SET password=? WHERE username=?",
                  (hash_password(new_password), 'admin'))
    else:
        c.execute("INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
                  ('admin', hash_password(new_password), 'Administrator', 'admin'))
    conn.commit()
    conn.close()

    if generated:
        _deliver_generated_admin_password(new_password)
    else:
        print("Admin password updated to the value provided.")
    return new_password

def _add_column_if_missing(cursor, table, column, coltype):
    """Add a column, tolerating only the "already exists" case.

    ALTER TABLE ... ADD COLUMN has no IF NOT EXISTS clause, so this is the
    standard way to make the migration idempotent. But sqlite3.OperationalError
    also covers unrelated failures (e.g. "database is locked", a missing
    table, a syntax error), so we inspect the message and re-raise anything
    that isn't specifically a duplicate-column error instead of swallowing
    it wholesale.
    """
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            pass  # Column already exists — nothing to do
        else:
            raise

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_person TEXT NOT NULL,
        company_name TEXT NOT NULL,
        address TEXT,
        phone TEXT,
        email TEXT,
        gstin TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT NOT NULL,
        description TEXT,
        specification TEXT,
        unit TEXT DEFAULT 'Mtr.',
        rate_per_unit REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS quotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quotation_no TEXT UNIQUE NOT NULL,
        date TEXT NOT NULL,
        customer_id INTEGER REFERENCES customers(id),
        subject TEXT,
        terms TEXT DEFAULT '1. Rates are Ex-works.\n2. GST 18%% Extra as applicable.\n3. Payment: 20%% Advance and Balance against delivery.\n4. Delivery: 10-12 weeks after receipt of order.',
        status TEXT DEFAULT 'Draft',
        notes TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS quotation_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quotation_id INTEGER REFERENCES quotations(id) ON DELETE CASCADE,
        item_id INTEGER REFERENCES items(id),
        item_name TEXT,
        description TEXT,
        specification TEXT,
        quantity REAL DEFAULT 0,
        unit TEXT,
        rate REAL DEFAULT 0,
        gst_percent REAL DEFAULT 18,
        sort_order INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS proforma_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pi_no TEXT UNIQUE NOT NULL,
        order_no TEXT,
        order_date TEXT,
        date TEXT NOT NULL,
        customer_id INTEGER REFERENCES customers(id),
        subject TEXT,
        terms TEXT DEFAULT '1. Rates are Ex-works.\n2. GST 18%% Extra as applicable.\n3. Payment: 20%% Advance and Balance against delivery.\n4. Delivery: 10-12 weeks after receipt of order.',
        status TEXT DEFAULT 'Draft',
        notes TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS proforma_invoice_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        proforma_id INTEGER REFERENCES proforma_invoices(id) ON DELETE CASCADE,
        item_id INTEGER REFERENCES items(id),
        item_name TEXT,
        description TEXT,
        specification TEXT,
        quantity REAL DEFAULT 0,
        unit TEXT,
        rate REAL DEFAULT 0,
        gst_percent REAL DEFAULT 18,
        sort_order INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS customer_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
        item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
        UNIQUE(customer_id, item_id)
    );
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS item_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
        item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
        UNIQUE(category_id, item_id)
    );
    CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        designation TEXT,
        phone TEXT,
        basic_salary REAL DEFAULT 0,
        overtime_rate REAL DEFAULT 1.5,
        working_hours_per_day REAL DEFAULT 8,
        advance REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER REFERENCES employees(id) ON DELETE CASCADE,
        date TEXT NOT NULL,
        is_present INTEGER DEFAULT 1,
        check_in TEXT,
        check_out TEXT,
        overtime_hours REAL DEFAULT 0,
        notes TEXT,
        UNIQUE(employee_id, date)
    );
    CREATE TABLE IF NOT EXISTS packing_lists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pl_no TEXT UNIQUE NOT NULL,
        pi_id INTEGER REFERENCES proforma_invoices(id),
        date TEXT NOT NULL,
        customer_id INTEGER REFERENCES customers(id),
        subject TEXT,
        status TEXT DEFAULT 'Draft',
        notes TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS packing_list_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        packing_list_id INTEGER REFERENCES packing_lists(id) ON DELETE CASCADE,
        item_id INTEGER REFERENCES items(id),
        item_name TEXT,
        description TEXT,
        specification TEXT,
        total_qty REAL DEFAULT 0,
        unit TEXT,
        bag_qty REAL DEFAULT 0,
        sort_order INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS packing_list_bags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        packing_list_item_id INTEGER REFERENCES packing_list_items(id) ON DELETE CASCADE,
        bag_no INTEGER NOT NULL,
        qty REAL NOT NULL,
        weight REAL,
        remarks TEXT
    );

    -- SQLite does not automatically index foreign key columns (unlike most
    -- other RDBMSs), so JOINs and ON DELETE CASCADE lookups on these would
    -- otherwise force a full table scan of the child table. Columns already
    -- covered by a UNIQUE constraint as their leftmost member (e.g.
    -- attendance(employee_id, date)) don't need a duplicate index here.
    CREATE INDEX IF NOT EXISTS idx_quotations_customer_id ON quotations(customer_id);
    CREATE INDEX IF NOT EXISTS idx_quotations_created_by ON quotations(created_by);
    CREATE INDEX IF NOT EXISTS idx_quotation_items_quotation_id ON quotation_items(quotation_id);
    CREATE INDEX IF NOT EXISTS idx_quotation_items_item_id ON quotation_items(item_id);
    CREATE INDEX IF NOT EXISTS idx_proforma_invoices_customer_id ON proforma_invoices(customer_id);
    CREATE INDEX IF NOT EXISTS idx_proforma_invoices_created_by ON proforma_invoices(created_by);
    CREATE INDEX IF NOT EXISTS idx_proforma_invoice_items_proforma_id ON proforma_invoice_items(proforma_id);
    CREATE INDEX IF NOT EXISTS idx_proforma_invoice_items_item_id ON proforma_invoice_items(item_id);
    CREATE INDEX IF NOT EXISTS idx_customer_items_item_id ON customer_items(item_id);
    CREATE INDEX IF NOT EXISTS idx_item_categories_item_id ON item_categories(item_id);
    CREATE INDEX IF NOT EXISTS idx_packing_lists_pi_id ON packing_lists(pi_id);
    CREATE INDEX IF NOT EXISTS idx_packing_lists_customer_id ON packing_lists(customer_id);
    CREATE INDEX IF NOT EXISTS idx_packing_lists_created_by ON packing_lists(created_by);
    CREATE INDEX IF NOT EXISTS idx_packing_list_items_packing_list_id ON packing_list_items(packing_list_id);
    CREATE INDEX IF NOT EXISTS idx_packing_list_items_item_id ON packing_list_items(item_id);
    CREATE INDEX IF NOT EXISTS idx_packing_list_bags_packing_list_item_id ON packing_list_bags(packing_list_item_id);
    """)
    # Migrations for existing databases: add columns that didn't exist in
    # earlier releases. Only "duplicate column name" is expected/benign
    # here; anything else (e.g. "database is locked") is a real failure
    # and must not be silently swallowed.
    _add_column_if_missing(c, "proforma_invoices", "order_date", "TEXT")
    _add_column_if_missing(c, "employees", "joining_date", "TEXT")
    _add_column_if_missing(c, "employees", "leaving_date", "TEXT")

    # Seed a default admin user — but never with a hardcoded password.
    # Set ADMIN_PASSWORD in the environment before first run to choose
    # your own; otherwise a random one is generated. It's printed to the
    # console AND saved to a restricted-permission file (see
    # _deliver_generated_admin_password), so a deployment where stdout
    # isn't captured doesn't lock the administrator out permanently — and
    # if it's ever lost anyway, `python3 database.py reset-admin` recovers it.
    admin_exists = c.execute("SELECT 1 FROM users WHERE username=?", ('admin',)).fetchone()
    if not admin_exists:
        admin_password = os.environ.get('ADMIN_PASSWORD')
        generated = admin_password is None
        if generated:
            admin_password = secrets.token_urlsafe(12)
        c.execute("INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)",
                  ('admin', hash_password(admin_password), 'Administrator', 'admin'))
        if generated:
            _deliver_generated_admin_password(admin_password)

    # Seed item catalog
    count = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if count == 0:
        items = [
            ("SUGAR ELEVATOR CHAIN","Sugar Elevator Chain 66.60 MM Pitch","PLATE THICKNESS : 5 MM\nPIN DIA : 11 MM\nBUSH DIA : 16 MM\nROLLER DIA : 24 MM\nSIDE LINK : 32 X 5 MM\nATTACHMENT LINK : 65 X 5 MM / 75 X 5 MM","Mtr.",2700),
            ("DOUBLE STRAND PINTLE CHAIN","Double Strand Pintle Chain 50.80 MM Pitch","PLATE THICKNESS : 6 MM\nPIN DIA : 14 MM\nBUSH DIA : 18 MM\nSIDE LINK : 38 X 6 MM\nATTACHMENT : 70 X 6 MM","Mtr.",3200),
            ("G TYPE CHAIN","G Type Welded Steel Chain 150 MM Pitch","PLATE THICKNESS : 10 MM\nPIN DIA : 18 MM\nBUSH DIA : 24 MM\nROLLER DIA : 38 MM\nSIDE LINK : 50 X 10 MM","Mtr.",4500),
            ("ROLLER CHAIN","Standard Roller Chain 25.40 MM Pitch","PLATE THICKNESS : 2 MM\nPIN DIA : 5 MM\nROLLER DIA : 8 MM\nBUSH DIA : 6 MM","Mtr.",550),
            ("ATTACHMENT CHAIN","Attachment Chain with Extended Pins 38.10 MM Pitch","PLATE THICKNESS : 4 MM\nPIN DIA : 10 MM\nBUSH DIA : 14 MM\nROLLER DIA : 20 MM\nATTACHMENT LENGTH : 50 MM","Mtr.",1800),
            ("PINTLE CHAIN","Single Strand Pintle Chain 101.60 MM Pitch","PLATE THICKNESS : 8 MM\nPIN DIA : 16 MM\nBUSH DIA : 22 MM\nSIDE LINK : 45 X 8 MM","Mtr.",2950),
            ("CRANK CHAIN","Crank Chain for Sugar Mills 76.20 MM Pitch","PLATE THICKNESS : 6 MM\nPIN DIA : 12 MM\nBUSH DIA : 16 MM\nROLLER DIA : 24 MM\nSIDE LINK : 36 X 6 MM","Mtr.",2450),
            ("BAGASSE CARRIER CHAIN","Bagasse Carrier Chain 152.40 MM Pitch","PLATE THICKNESS : 8 MM\nPIN DIA : 20 MM\nBUSH DIA : 28 MM\nROLLER DIA : 40 MM\nATTACHMENT : 80 X 8 MM","Mtr.",2850),
            ("FORGED CHAIN","Forged Link Chain 142.25 MM Pitch","PLATE THICKNESS : 12 MM\nPIN DIA : 22 MM\nBUSH DIA : 30 MM\nSIDE LINK : 55 X 12 MM","Mtr.",3600),
            ("SLAT CHAIN","Slat Chain for Conveyor 38.10 MM Pitch","PLATE THICKNESS : 3 MM\nPIN DIA : 8 MM\nROLLER DIA : 14 MM\nSLAT WIDTH : 50 MM","Mtr.",1250),
            ("BUCHER CHAIN","Bucher Type Chain 100 MM Pitch","PLATE THICKNESS : 8 MM\nPIN DIA : 16 MM\nBUSH DIA : 22 MM\nROLLER DIA : 32 MM","Mtr.",3100),
            ("BUCHER HARVESTING CHAIN","Bucher Harvesting Chain 101.60 MM Pitch","PLATE THICKNESS : 6 MM\nPIN DIA : 14 MM\nBUSH DIA : 20 MM\nROLLER DIA : 28 MM\nATTACHMENT : 65 X 6 MM","Mtr.",3400),
            ("STEEL BUSHD ROLLER CHAIN","Steel Bushed Roller Chain 152.40 MM Pitch","PLATE THICKNESS : 10 MM\nPIN DIA : 20 MM\nBUSH DIA : 28 MM\nROLLER DIA : 42 MM\nSIDE LINK : 60 X 10 MM","Mtr.",3200),
        ]
        c.executemany("INSERT INTO items (item_name,description,specification,unit,rate_per_unit) VALUES (?,?,?,?,?)", items)
    conn.commit()
    conn.close()

def gen_quotation_no(conn):
    year = datetime.now().strftime("%Y")
    prefix = f"AQ-{year}-"
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(quotation_no, ?) AS INTEGER)) FROM quotations WHERE quotation_no LIKE ?",
        (len(prefix) + 1, f"{prefix}%")).fetchone()
    num = (row[0] or 0) + 1
    return f"{prefix}{num:03d}"

def gen_proforma_no(conn):
    year = datetime.now().strftime("%Y")
    prefix = f"API-{year}-"
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(pi_no, ?) AS INTEGER)) FROM proforma_invoices WHERE pi_no LIKE ?",
        (len(prefix) + 1, f"{prefix}%")).fetchone()
    num = (row[0] or 0) + 1
    return f"{prefix}{num:03d}"

def gen_packing_list_no(conn):
    year = datetime.now().strftime("%Y")
    prefix = f"PL-{year}-"
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(pl_no, ?) AS INTEGER)) FROM packing_lists WHERE pl_no LIKE ?",
        (len(prefix) + 1, f"{prefix}%")).fetchone()
    num = (row[0] or 0) + 1
    return f"{prefix}{num:03d}"

if __name__ == '__main__':
    # `python3 database.py`               -> just (re)run init/migrations.
    # `python3 database.py reset-admin`   -> reset/create admin with
    #                                         ADMIN_PASSWORD from the env,
    #                                         or a freshly generated one.
    # `python3 database.py reset-admin PW`-> reset/create admin with PW.
    # This is the recovery path if a generated password from first run
    # was never seen (console output missed, credentials file deleted).
    if len(sys.argv) > 1 and sys.argv[1] == 'reset-admin':
        init_db()  # make sure the users table exists first
        reset_admin_password(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        init_db()
        print("Database initialized (schema + migrations applied).")
