from flask import Flask, render_template, request, redirect, url_for, flash, Response, session, send_file, jsonify
import sqlite3
import datetime
import csv
import io
import os
from functools import wraps
from fpdf import FPDF
import barcode
from barcode.writer import SVGWriter

# Anchor all file paths to this file's own directory so the app works no matter
# what the current working directory is (matters for WSGI hosts like
# PythonAnywhere and for any future packaged/bundled build).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__)
# Secret key and access codes come from the environment when available, with
# safe local fallbacks. CHANGE THESE in the hosting environment.
app.secret_key = os.environ.get('LOTTERY_SECRET_KEY', 'super_secret_lottery_key_12345')
app.config['PERMANENT_SESSION_LIFETIME'] = datetime.timedelta(hours=12)

# Single passcode to enter the app (any staff member), plus a separate PIN that
# still guards the manager-only backroom.
APP_PASSCODE = os.environ.get('LOTTERY_PASSCODE', '1111')
MANAGER_PIN = os.environ.get('LOTTERY_MANAGER_PIN', '1234')
WIPE_PASSWORD = os.environ.get('LOTTERY_WIPE_PASSWORD', 'SuperSecret123!')

# A real scratch-ticket barcode tops out around 16 digits; anything much longer
# is almost certainly a scanner double-fire concatenating two reads.
MAX_BARCODE_LEN = 24
# How long a soft-deleted backroom pack stays visible (struck-through) so any
# funny business remains obvious.
DELETED_PACK_VISIBLE_WEEKS = 6


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS games
                 (game_number TEXT PRIMARY KEY, name TEXT, price REAL, tickets_per_pack INTEGER)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS packs
                 (pack_id TEXT PRIMARY KEY, game_number TEXT, status TEXT, slot_number INTEGER, current_ticket INTEGER)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS audits
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, slot_number INTEGER, pack_id TEXT, tickets_sold INTEGER, cash_expected REAL, method TEXT)''')

    # Employee table with UNIQUE constraint on PIN. PINs are no longer used to
    # log in (the app passcode does that) but the column is kept so existing
    # data and the UNIQUE/NOT NULL constraints stay valid.
    conn.execute('''CREATE TABLE IF NOT EXISTS employees
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, pin TEXT UNIQUE NOT NULL)''')
    conn.execute('INSERT OR IGNORE INTO employees (name, pin) VALUES ("Manager", "9999")')

    # Change log — an audit trail of every inventory/game/cashier change, used
    # as the go-to record for chasing discrepancies.
    conn.execute('''CREATE TABLE IF NOT EXISTS change_log
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, actor TEXT,
                  category TEXT, action TEXT, target TEXT, old_value TEXT,
                  new_value TEXT, details TEXT)''')

    try:
        conn.execute('ALTER TABLE audits ADD COLUMN method TEXT')
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute('ALTER TABLE audits ADD COLUMN cashier_name TEXT')
    except sqlite3.OperationalError:
        pass

    # Soft-delete marker for backroom packs (kept for the audit trail).
    try:
        conn.execute('ALTER TABLE packs ADD COLUMN deleted_at TEXT')
    except sqlite3.OperationalError:
        pass

    # When a pack was scanned in, so backroom stock can show newest first.
    try:
        conn.execute('ALTER TABLE packs ADD COLUMN received_at TEXT')
    except sqlite3.OperationalError:
        pass

    # Display label for a dispenser slot, e.g. "21", or "21A"/"21B" when a
    # double-size slot holds two different packs.
    try:
        conn.execute('ALTER TABLE packs ADD COLUMN slot_label TEXT')
    except sqlite3.OperationalError:
        pass

    # Rep-return tracking: cashier records a pending return, manager confirms.
    for col in ('returned_by', 'returned_at', 'return_confirmed_by', 'return_confirmed_at'):
        try:
            conn.execute(f'ALTER TABLE packs ADD COLUMN {col} TEXT')
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


def log_change(actor, category, action, target='', old_value='', new_value='', details=''):
    """Write a single entry to the change_log audit trail. Opens its own
    connection so callers can log after committing their own work."""
    conn = get_db_connection()
    conn.execute('''INSERT INTO change_log
                    (timestamp, actor, category, action, target, old_value, new_value, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                 (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  actor, category, action, str(target), str(old_value), str(new_value), details))
    conn.commit()
    conn.close()


def pdf_to_bytes(pdf):
    """Return PDF bytes across both fpdf (PyFPDF) and fpdf2 output styles."""
    out = pdf.output(dest='S')
    if isinstance(out, str):
        return out.encode('latin-1')
    return bytes(out)


def current_actor():
    """Best-effort name of whoever is performing an action, for the logs."""
    return session.get('cashier_name') or 'STAFF'


def parse_ticket_barcode(raw):
    """Parse a scanned lottery barcode into (game_num, pack_id, ticket_num).
    Returns None if the barcode is too short to be valid."""
    raw = (raw or '').strip()
    if len(raw) < 12:
        return None

    clean_barcode = raw[:-2]
    ticket_num = int(clean_barcode[-3:])
    game_pack_str = clean_barcode[:-3]

    # For longer barcodes (24+ digits), the game_pack_str contains
    # additional data (validation, security) that we do not need.
    # The game+pack portion is always at the beginning.
    if len(game_pack_str) > 11:
        game_pack_str = game_pack_str[:9]

    if len(game_pack_str) == 9:
        game_num = game_pack_str[:3]
    else:
        game_num = game_pack_str[:4]

    pack_num = game_pack_str[len(game_num):]
    pack_id = f"{game_num}-{pack_num}"
    return game_num, pack_id, ticket_num


def validate_reading(ticket_num, current_ticket):
    """Return an error message if a shift reading is out of range, else None.

    A reading sets the pack's pointer directly, so it can only land within the
    pack: from #000 up to the last known ticket. (Unlike a sale quantity, it
    can't run past the bottom.) This guards the manual override in particular,
    where the number is typed rather than scanned."""
    if ticket_num < 0:
        return "Ticket number can't be negative."
    if ticket_num > current_ticket:
        return f"Reading #{ticket_num:03d} is higher than expected (#{current_ticket:03d})."
    return None


def fetch_pending_returns(conn):
    """Packs a cashier has marked as returned to the rep, awaiting manager sign-off."""
    return conn.execute('''
        SELECT p.pack_id, p.slot_number, p.slot_label, p.returned_by, p.returned_at,
               COALESCE(g.name, '⚠ Unknown game #' || p.game_number) AS name
        FROM packs p LEFT JOIN games g ON p.game_number = g.game_number
        WHERE p.status = 'RETURN_PENDING'
        ORDER BY p.returned_at DESC
    ''').fetchall()


def line_qty(data):
    """Quantity for a cart line. In 'bulk' mode the cashier scans the first and
    last ticket and we charge the whole range; in 'single' mode each scan is one
    ticket, so the quantity is simply the number of scans."""
    scans = data.get('scans', [])
    if not scans:
        return 0
    if data.get('mode') == 'bulk':
        return (max(scans) - min(scans)) + 1
    return len(scans)


# Ensure tables exist as soon as the module is imported. WSGI hosts (e.g.
# PythonAnywhere) import the app object without ever running the __main__ block,
# so this must not be gated behind if __name__ == '__main__'.
init_db()


# --- ACCESS CONTROL ---

@app.before_request
def require_unlock():
    """Gate the whole app behind a single passcode. Only the unlock screen,
    static assets, and webhook deployments are reachable while locked."""
    if request.endpoint in ('unlock', 'static', 'github_webhook'):
        return
    if not session.get('app_unlocked'):
        return redirect(url_for('unlock'))


def manager_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_manager'):
            flash("You must enter the Manager PIN to access the backroom.", "danger")
            return redirect(url_for('manager_login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/unlock', methods=['GET', 'POST'])
def unlock():
    """Single entry point: enter the shared app passcode and pick your name.
    The chosen name tags every sale and shift reading for the session."""
    conn = get_db_connection()
    employees = conn.execute('SELECT * FROM employees ORDER BY name ASC').fetchall()
    conn.close()

    if request.method == 'POST':
        passcode = request.form.get('passcode', '').strip()
        name = request.form.get('cashier_name', '').strip()
        if passcode != APP_PASSCODE:
            flash("Incorrect passcode.", "danger")
        elif not name:
            flash("Please select your name.", "danger")
        else:
            session.permanent = True
            session['app_unlocked'] = True
            session['cashier_name'] = name
            session['cart'] = {}
            session['scan_mode'] = 'single'
            return redirect(url_for('index'))

    return render_template('unlock.html', employees=employees)


@app.route('/logout')
def logout():
    session.clear()  # Wipes the user, their cart, and any manager access.
    flash("You have been logged out.", "info")
    return redirect(url_for('unlock'))


# --- FRONT OF HOUSE (CASHIER ROUTES) ---

@app.route('/')
def index():
    conn = get_db_connection()
    # Active packs, plus sold-out packs that still hold a slot (shown as "OUT"
    # until the slot is restocked).
    dispenser_packs = conn.execute('''
        SELECT p.*, g.name, g.price
        FROM packs p JOIN games g ON p.game_number = g.game_number
        WHERE p.status = "DISPENSER"
           OR (p.status = "SOLD_OUT" AND p.slot_number IS NOT NULL)
        ORDER BY p.slot_number, p.slot_label
    ''').fetchall()
    pending_returns = fetch_pending_returns(conn)
    conn.close()
    return render_template('index.html', dispenser=dispenser_packs, pending_returns=pending_returns)


@app.route('/shift_scan', methods=['POST'])
def shift_scan():
    """Log a shift reading: staff scan the ticket now showing at the front of a
    dispenser slot. The gap between the pack's last known ticket and the scanned
    one is the number sold since the last reading."""
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.args.get('json') == '1'
    cashier = current_actor()
    parsed = parse_ticket_barcode(request.form.get('barcode', ''))
    if not parsed:
        msg = "Invalid barcode length."
        if is_ajax:
            return jsonify({'success': False, 'message': msg})
        flash(msg, "danger")
        return redirect(url_for('index') + '#shift-reading')

    game_num, pack_id, ticket_num = parsed

    conn = get_db_connection()
    game = conn.execute('SELECT price, name FROM games WHERE game_number = ?', (game_num,)).fetchone()
    pack = conn.execute('SELECT slot_number, current_ticket FROM packs WHERE pack_id = ? AND status = "DISPENSER"', (pack_id,)).fetchone()

    if not game or not pack:
        conn.close()
        msg = f"Pack {pack_id} is not active in a dispenser slot."
        if is_ajax:
            return jsonify({'success': False, 'message': msg})
        flash(msg, "danger")
        return redirect(url_for('index') + '#shift-reading')

    error = validate_reading(ticket_num, pack['current_ticket'])
    if error:
        conn.close()
        msg = f"{error} Nothing logged."
        if is_ajax:
            return jsonify({'success': False, 'message': msg})
        flash(msg, "danger")
        return redirect(url_for('index') + '#shift-reading')

    tickets_sold = pack['current_ticket'] - ticket_num
    cash_expected = tickets_sold * game['price']
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute('''INSERT INTO audits (timestamp, slot_number, pack_id, tickets_sold, cash_expected, method, cashier_name)
                   VALUES (?, ?, ?, ?, ?, "SHIFT_SCAN", ?)''',
                 (timestamp, pack['slot_number'], pack_id, tickets_sold, cash_expected, cashier))
    conn.execute('UPDATE packs SET current_ticket = ? WHERE pack_id = ?', (ticket_num, pack_id))
    conn.commit()
    conn.close()

    msg = f"Logged {pack_id}: {tickets_sold} sold (${cash_expected:.2f})"
    if is_ajax:
        return jsonify({'success': True, 'message': msg, 'pack_id': pack_id, 'tickets_sold': tickets_sold, 'cash_expected': cash_expected})

    flash(f"Shift reading logged for {pack_id}: {tickets_sold} sold (${cash_expected:.2f}).", "success")
    return redirect(url_for('index') + '#shift-reading')


@app.route('/shift_reading_manual', methods=['POST'])
def shift_reading_manual():
    """Manager override of a shift reading by typing the current ticket number
    instead of scanning. Protected by the Manager PIN."""
    if request.form.get('pin', '').strip() != MANAGER_PIN:
        flash("Invalid Manager PIN for manual override.", "danger")
        return redirect(url_for('index'))

    pack_id = request.form.get('pack_id')
    try:
        ticket_num = int(request.form.get('scanned_ticket'))
    except (TypeError, ValueError):
        flash("Enter a valid ticket number.", "danger")
        return redirect(url_for('index'))

    conn = get_db_connection()
    pack = conn.execute('''SELECT p.slot_number, p.current_ticket, g.price
                           FROM packs p JOIN games g ON p.game_number = g.game_number
                           WHERE p.pack_id = ? AND p.status = "DISPENSER"''', (pack_id,)).fetchone()

    if not pack:
        conn.close()
        flash(f"Pack {pack_id} is not active in a dispenser slot.", "danger")
        return redirect(url_for('index'))

    error = validate_reading(ticket_num, pack['current_ticket'])
    if error:
        conn.close()
        flash(error, "danger")
        return redirect(url_for('index'))

    old_ticket = pack['current_ticket']
    tickets_sold = old_ticket - ticket_num
    cash_expected = tickets_sold * pack['price']
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute('''INSERT INTO audits (timestamp, slot_number, pack_id, tickets_sold, cash_expected, method, cashier_name)
                   VALUES (?, ?, ?, ?, ?, "MANUAL_OVERRIDE", ?)''',
                 (timestamp, pack['slot_number'], pack_id, tickets_sold, cash_expected, current_actor()))
    conn.execute('UPDATE packs SET current_ticket = ? WHERE pack_id = ?', (ticket_num, pack_id))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'INVENTORY', 'MANUAL_READING', pack_id,
               old_value=f"#{old_ticket:03d}", new_value=f"#{ticket_num:03d}",
               details=f"{tickets_sold} counted sold")
    flash(f"Manual reading saved for {pack_id}: {tickets_sold} sold (${cash_expected:.2f}).", "success")
    return redirect(url_for('index'))


@app.route('/mark_empty', methods=['POST'])
def mark_empty():
    """Mark a dispenser pack as sold out and clear its slot. Protected by the
    Manager PIN. Any tickets still on the pack are logged as sold, on the
    assumption that an emptied pack has been fully dispensed."""
    if request.form.get('pin', '').strip() != MANAGER_PIN:
        flash("Invalid Manager PIN.", "danger")
        return redirect(url_for('index'))

    pack_id = request.form.get('pack_id')
    conn = get_db_connection()
    pack = conn.execute('''SELECT p.slot_number, p.current_ticket, g.price
                           FROM packs p JOIN games g ON p.game_number = g.game_number
                           WHERE p.pack_id = ? AND p.status = "DISPENSER"''', (pack_id,)).fetchone()

    if not pack:
        conn.close()
        flash(f"Pack {pack_id} is not active in a dispenser slot.", "danger")
        return redirect(url_for('index'))

    tickets_sold = pack['current_ticket'] + 1  # tickets 0..current_ticket remain
    cash_expected = tickets_sold * pack['price']
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn.execute('''INSERT INTO audits (timestamp, slot_number, pack_id, tickets_sold, cash_expected, method, cashier_name)
                   VALUES (?, ?, ?, ?, ?, "MARK_EMPTY", ?)''',
                 (timestamp, pack['slot_number'], pack_id, tickets_sold, cash_expected, current_actor()))
    # Keep the slot number so the dispenser shows an "OUT" stamp until restocked.
    conn.execute('UPDATE packs SET status = "SOLD_OUT", current_ticket = 0 WHERE pack_id = ?', (pack_id,))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'INVENTORY', 'MARK_EMPTY', pack_id,
               details=f"{tickets_sold} tickets logged as sold on empty-out")
    flash(f"Pack {pack_id} marked empty and cleared from its slot.", "success")
    return redirect(url_for('index'))


@app.route('/return_pack', methods=['POST'])
def return_pack():
    """Cashier records a pack being returned to the lottery rep. No manager code
    needed (cashiers are sometimes alone). It becomes a PENDING return for a
    manager to confirm later — the record lives online in the meantime."""
    raw = request.form.get('barcode', '').strip()

    # Same trigger-finger guards as receiving.
    if len(raw) > MAX_BARCODE_LEN:
        flash("That scan looks too long — likely a double-scan. Please scan again.", "danger")
        return redirect(url_for('index') + '#return-rep')
    now_ts = datetime.datetime.now().timestamp()
    last = session.get('last_return')
    if last and last.get('code') == raw and (now_ts - last.get('ts', 0)) < 2:
        flash("Ignored a duplicate scan.", "info")
        return redirect(url_for('index') + '#return-rep')
    session['last_return'] = {'code': raw, 'ts': now_ts}

    parsed = parse_ticket_barcode(raw)
    if not parsed:
        flash("Invalid barcode.", "danger")
        return redirect(url_for('index') + '#return-rep')
    game_num, pack_id, _ = parsed

    conn = get_db_connection()
    pack = conn.execute('SELECT status FROM packs WHERE pack_id = ?', (pack_id,)).fetchone()
    if not pack or pack['status'] not in ('BACKROOM', 'DISPENSER'):
        conn.close()
        flash(f"Pack {pack_id} isn't available to return (must be in backroom or dispenser).", "danger")
        return redirect(url_for('index') + '#return-rep')

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute('UPDATE packs SET status = "RETURN_PENDING", returned_by = ?, returned_at = ? WHERE pack_id = ?',
                 (current_actor(), ts, pack_id))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'RETURN', 'RETURN_PENDING', pack_id,
               old_value=pack['status'], new_value='RETURN_PENDING',
               details='awaiting manager confirmation')
    flash(f"Return recorded for {pack_id}. 📸 Photograph the rep's receipt and text it to the work group.", "success")
    return redirect(url_for('index') + '#return-rep')


@app.route('/sell_lottery', methods=['GET', 'POST'])
def sell_lottery():
    if 'cart' not in session:
        session['cart'] = {}

    if request.method == 'POST':
        # Switch scan mode (single ticket vs. bulk range).
        if 'set_mode' in request.form:
            mode = request.form.get('set_mode')
            session['scan_mode'] = 'bulk' if mode == 'bulk' else 'single'
            session.modified = True
            return redirect(url_for('sell_lottery'))

        # Empty the whole cart.
        if 'clear_cart' in request.form:
            session['cart'] = {}
            session.pop('last_pack', None)
            session.modified = True
            return redirect(url_for('sell_lottery'))

        # Undo the most recent scan.
        if 'undo_last' in request.form:
            cart = session.get('cart', {})
            lp = session.get('last_pack')
            if lp and lp in cart and cart[lp]['scans']:
                cart[lp]['scans'].pop()
                if not cart[lp]['scans']:
                    del cart[lp]
                    session.pop('last_pack', None)
                flash("Removed last scan.", "info")
            session.modified = True
            return redirect(url_for('sell_lottery'))

        # Remove an entire line.
        if 'remove_pack' in request.form:
            cart = session.get('cart', {})
            pid = request.form.get('remove_pack')
            if pid in cart:
                del cart[pid]
                if session.get('last_pack') == pid:
                    session.pop('last_pack', None)
            session.modified = True
            return redirect(url_for('sell_lottery'))

        # Otherwise this is a barcode scan.
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.args.get('json') == '1'
        raw_scan = request.form.get('barcode', '').strip()
        now_ts = datetime.datetime.now().timestamp()
        last_sell = session.get('last_sell')
        if last_sell and last_sell.get('code') == raw_scan and (now_ts - last_sell.get('ts', 0)) < 2:
            msg = "Ignored duplicate scan."
            if is_ajax:
                return jsonify({'success': False, 'message': msg})
            flash(msg, "info")
            return redirect(url_for('sell_lottery'))
        session['last_sell'] = {'code': raw_scan, 'ts': now_ts}

        parsed = parse_ticket_barcode(raw_scan)
        if not parsed:
            msg = "Invalid barcode length."
            if is_ajax:
                return jsonify({'success': False, 'message': msg})
            flash(msg, "danger")
            return redirect(url_for('sell_lottery'))

        game_num, pack_id, ticket_num = parsed

        conn = get_db_connection()
        game = conn.execute('SELECT price, name FROM games WHERE game_number = ?', (game_num,)).fetchone()
        pack = conn.execute('SELECT status, current_ticket FROM packs WHERE pack_id = ?', (pack_id,)).fetchone()
        conn.close()

        if not game or not pack or pack['status'] != 'DISPENSER':
            msg = f"Pack {pack_id} is not active in dispenser."
            if is_ajax:
                return jsonify({'success': False, 'message': msg})
            flash(msg, "danger")
            return redirect(url_for('sell_lottery'))

        if ticket_num > pack['current_ticket']:
            msg = f"Ticket #{ticket_num:03d} has already been sold!"
            if is_ajax:
                return jsonify({'success': False, 'message': msg})
            flash(msg, "danger")
            return redirect(url_for('sell_lottery'))

        mode = session.get('scan_mode', 'single')
        cart = session['cart']
        entry = cart.get(pack_id)
        if entry:
            entry['scans'].append(ticket_num)
            entry['mode'] = mode
        else:
            entry = {
                'game_name': game['name'],
                'game_number': game_num,
                'price': game['price'],
                'scans': [ticket_num],
                'mode': mode,
            }
            cart[pack_id] = entry

        top_t = max(entry['scans'])
        if line_qty(entry) > top_t + 1:
            entry['scans'].pop()
            if not entry['scans']:
                del cart[pack_id]
            session.modified = True
            msg = f"Only {top_t + 1} ticket(s) remain at or below #{top_t:03d}."
            if is_ajax:
                return jsonify({'success': False, 'message': msg})
            flash(msg, "danger")
            return redirect(url_for('sell_lottery'))

        session['last_pack'] = pack_id
        session.modified = True

        msg = f"Added {pack_id} #{ticket_num:03d} to cart"
        if mode == 'bulk':
            if len(entry['scans']) == 1:
                msg = f"First ticket #{ticket_num:03d} captured — now scan LAST ticket."
            else:
                msg = f"Bulk range #{min(entry['scans']):03d}–#{max(entry['scans']):03d} captured."

        if is_ajax:
            return jsonify({'success': True, 'message': msg, 'pack_id': pack_id, 'ticket_num': ticket_num})

        flash(msg, "success" if mode != 'bulk' or len(entry['scans']) > 1 else "info")
        return redirect(url_for('sell_lottery'))

    # Calculate live totals for the page.
    cart_display = []
    grand_total = 0
    for pid, data in session.get('cart', {}).items():
        qty = line_qty(data)
        line_total = qty * data['price']
        grand_total += line_total
        cart_display.append({
            'pack_id': pid, 'game_name': data['game_name'],
            'game_number': data.get('game_number') or pid.split('-')[0],
            'qty': qty, 'price': data['price'], 'line_total': line_total,
            'scans': list(data['scans']),
            'min_t': min(data['scans']), 'max_t': max(data['scans']),
            'mode': data.get('mode', 'single'),
        })

    return render_template('sell_lottery.html', cart_display=cart_display,
                           grand_total=grand_total,
                           scan_mode=session.get('scan_mode', 'single'))


@app.route('/process_cart', methods=['POST'])
def process_cart():
    if not session.get('cart'):
        return redirect(url_for('sell_lottery'))

    conn = get_db_connection()
    grand_total = 0
    receipt_data = []
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cashier = current_actor()

    for pack_id, data in session['cart'].items():
        qty = line_qty(data)
        if qty <= 0:
            continue
        top_t = max(data['scans'])
        line_total = qty * data['price']
        grand_total += line_total

        receipt_data.append({'name': data['game_name'], 'qty': qty, 'total': line_total})

        pack = conn.execute('SELECT slot_number FROM packs WHERE pack_id = ?', (pack_id,)).fetchone()
        slot_number = pack['slot_number'] if pack else None
        # Advance the pointer by the quantity sold, from the top of the run.
        # (top_t - qty) lands on the next unsold ticket; -1 means sold out.
        new_ticket = top_t - qty

        conn.execute('''INSERT INTO audits (timestamp, slot_number, pack_id, tickets_sold, cash_expected, method, cashier_name)
                       VALUES (?, ?, ?, ?, ?, "CART_CHECKOUT", ?)''',
                     (timestamp, slot_number, pack_id, qty, line_total, cashier))

        if new_ticket < 0:
            # Keep the slot number so the dispenser can show an "OUT" stamp on
            # that slot until it's restocked.
            conn.execute('UPDATE packs SET status = "SOLD_OUT", current_ticket = 0 WHERE pack_id = ?', (pack_id,))
        else:
            conn.execute('UPDATE packs SET current_ticket = ? WHERE pack_id = ?', (new_ticket, pack_id))

    conn.commit()
    conn.close()

    session['receipt'] = receipt_data
    session['cart'] = {}
    session.pop('last_pack', None)

    return redirect(url_for('pos_checkout', amount=f"{grand_total:.2f}"))


@app.route('/pos_checkout/<amount>')
def pos_checkout(amount):
    try:
        total_dollars = float(amount)
        total_cents = int(total_dollars * 100)

        # Build the 11-digit payload for the Gilbarco register.
        prefix = "2"
        plu = "88888"
        price_str = f"{total_cents:05d}"
        barcode_data = f"{prefix}{plu}{price_str}"

        os.makedirs(STATIC_DIR, exist_ok=True)
        upc = barcode.get('upca', barcode_data, writer=SVGWriter())
        upc.save(os.path.join(STATIC_DIR, 'checkout_barcode'))

        return render_template('pos_checkout.html', total=f"{total_dollars:.2f}",
                               receipt=session.get('receipt', []))
    except ValueError:
        flash("Invalid amount for checkout.", "danger")
        return redirect(url_for('index'))


# --- BACK OF HOUSE (MANAGER ROUTES) ---

@app.route('/manager_login', methods=['GET', 'POST'])
def manager_login():
    if request.method == 'POST':
        if request.form['pin'].strip() == MANAGER_PIN:
            session.permanent = True
            session['is_manager'] = True
            flash("Manager access granted.", "success")
            return redirect(url_for('games_management'))
        else:
            flash("Invalid PIN.", "danger")
    return render_template('login.html')


@app.route('/games')
@manager_required
def games_management():
    conn = get_db_connection()
    games = conn.execute('SELECT * FROM games ORDER BY price ASC, game_number ASC').fetchall()
    cutoff = (datetime.datetime.now() - datetime.timedelta(weeks=DELETED_PACK_VISIBLE_WEEKS)).strftime("%Y-%m-%d %H:%M:%S")
    backroom_packs = conn.execute('''
        SELECT p.*, COALESCE(g.name, '⚠ Unknown game #' || p.game_number) AS name
        FROM packs p LEFT JOIN games g ON p.game_number = g.game_number
        WHERE p.status = "BACKROOM"
           OR (p.status = "DELETED" AND p.deleted_at >= ?)
        ORDER BY (p.status = "DELETED"), p.received_at DESC
    ''', (cutoff,)).fetchall()
    employees = conn.execute('SELECT * FROM employees ORDER BY name ASC').fetchall()
    pending_returns = fetch_pending_returns(conn)
    conn.close()
    prefill_game = session.pop('prefill_game', '')
    return render_template('games.html', games=games, backroom=backroom_packs,
                           employees=employees, prefill_game=prefill_game,
                           pending_returns=pending_returns)


@app.route('/delete_employee', methods=['POST'])
@manager_required
def delete_employee():
    emp_id = request.form.get('emp_id')
    conn = get_db_connection()
    emp = conn.execute('SELECT name FROM employees WHERE id = ?', (emp_id,)).fetchone()
    conn.execute('DELETE FROM employees WHERE id = ? AND name != "Manager"', (emp_id,))
    conn.commit()
    conn.close()
    if emp:
        log_change(current_actor(), 'CASHIER', 'DELETE_CASHIER', emp['name'])
    flash("Cashier removed from system.", "info")
    return redirect(url_for('games_management'))


@app.route('/add_employee', methods=['POST'])
@manager_required
def add_employee():
    name = request.form.get('name', '').strip()
    pin = (request.form.get('pin') or '').strip()
    if not name:
        flash("Enter a cashier name.", "danger")
        return redirect(url_for('games_management'))

    conn = get_db_connection()
    # PINs are no longer used to log in, so auto-assign a unique placeholder
    # when none is supplied (keeps the UNIQUE/NOT NULL column satisfied).
    if not pin:
        row = conn.execute('SELECT COALESCE(MAX(id), 0) + 1001 AS n FROM employees').fetchone()
        pin = str(row['n'])

    added = False
    try:
        conn.execute('INSERT INTO employees (name, pin) VALUES (?, ?)', (name, pin))
        conn.commit()
        added = True
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

    if added:
        log_change(current_actor(), 'CASHIER', 'ADD_CASHIER', name)
        flash(f'Success: {name} has been added.', 'success')
    else:
        flash(f'Error: that ID/PIN is already in use. Try again.', 'danger')

    return redirect(url_for('games_management'))


@app.route('/receive_scan', methods=['POST'])
@manager_required
def receive_scan():
    raw = request.form.get('barcode', '').strip()

    # Trigger-finger guard 1: a wildly long string is almost certainly two scans
    # concatenated by a double-fire.
    if len(raw) > MAX_BARCODE_LEN:
        flash("That scan looks too long — likely a double-scan. Please scan again.", "danger")
        return redirect(url_for('games_management'))

    # Trigger-finger guard 2: ignore the identical barcode fired twice in quick
    # succession (within 2 seconds).
    now_ts = datetime.datetime.now().timestamp()
    last = session.get('last_receive')
    if last and last.get('code') == raw and (now_ts - last.get('ts', 0)) < 2:
        flash("Ignored a duplicate scan.", "info")
        return redirect(url_for('games_management'))
    session['last_receive'] = {'code': raw, 'ts': now_ts}

    parsed = parse_ticket_barcode(raw)
    if not parsed:
        flash("ERROR: Invalid Barcode", "danger")
        return redirect(url_for('games_management'))

    game_num, pack_id, _ = parsed

    conn = get_db_connection()
    pack = conn.execute('SELECT * FROM packs WHERE pack_id = ?', (pack_id,)).fetchone()

    if pack:
        # Re-scanning a soft-deleted pack restores it — the "re-scan to fix" flow.
        if pack['status'] == 'DELETED':
            restored_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute('UPDATE packs SET status = "BACKROOM", deleted_at = NULL, received_at = ? WHERE pack_id = ?', (restored_at, pack_id))
            conn.commit()
            conn.close()
            log_change(current_actor(), 'CORRECTION', 'RESTORE_PACK', pack_id,
                       old_value='DELETED', new_value='BACKROOM',
                       details='re-scanned; restored to backroom')
            flash(f"Pack {pack_id} was previously deleted — restored to Backroom Stock.", "success")
            return redirect(url_for('games_management'))
        conn.close()
        flash(f"NOTICE: Pack {pack_id} is already in the system.", "warning")
        return redirect(url_for('games_management'))

    game = conn.execute('SELECT tickets_per_pack FROM games WHERE game_number = ?', (game_num,)).fetchone()
    if not game:
        # Don't invent a placeholder game/price. Make the manager set it up first
        # so the name and denomination are real. The game number is pre-filled
        # into the "Add a New Game" form below.
        conn.close()
        session['prefill_game'] = game_num
        flash(f"Game #{game_num} is not set up yet. Enter its name and price under "
              f"\"Add a New Game\" below, then re-scan the pack.", "warning")
        return redirect(url_for('games_management'))

    starting_ticket = game['tickets_per_pack'] - 1
    received_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute('INSERT INTO packs (pack_id, game_number, status, slot_number, current_ticket, received_at) VALUES (?, ?, "BACKROOM", NULL, ?, ?)',
                 (pack_id, game_num, starting_ticket, received_at))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'INVENTORY', 'RECEIVE_PACK', pack_id,
               new_value='BACKROOM', details=f"start ticket #{starting_ticket:03d}")
    flash(f"Stock Received: Pack {pack_id} added to Backroom Stock!", "info")
    return redirect(url_for('games_management'))


@app.route('/delete_pack', methods=['POST'])
@manager_required
def delete_pack():
    """Soft-delete a backroom pack (a mis-scan correction). The row is kept and
    stays visible struck-through for a few weeks so nothing quietly disappears.
    Re-scanning the pack restores it."""
    pack_id = request.form.get('pack_id')
    conn = get_db_connection()
    pack = conn.execute('SELECT status FROM packs WHERE pack_id = ?', (pack_id,)).fetchone()
    if not pack or pack['status'] != 'BACKROOM':
        conn.close()
        flash(f"Pack {pack_id} is not in Backroom Stock.", "danger")
        return redirect(url_for('games_management'))

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute('UPDATE packs SET status = "DELETED", deleted_at = ? WHERE pack_id = ?', (ts, pack_id))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'CORRECTION', 'DELETE_PACK', pack_id,
               old_value='BACKROOM', new_value='DELETED',
               details=f'soft-deleted; visible struck-through for {DELETED_PACK_VISIBLE_WEEKS} weeks')
    flash(f"Pack {pack_id} deleted from Backroom Stock (kept visible for {DELETED_PACK_VISIBLE_WEEKS} weeks).", "info")
    return redirect(url_for('games_management'))


@app.route('/activate_pack', methods=['POST'])
@manager_required
def activate_pack():
    pack_id = request.form['pack_id']
    try:
        slot_num = int(request.form['slot_number'])
    except (TypeError, ValueError):
        flash("Enter a valid slot number.", "danger")
        return redirect(url_for('games_management'))

    conn = get_db_connection()

    # Restocking: retire any sold-out ("OUT") pack still sitting in this slot.
    conn.execute('UPDATE packs SET slot_number = NULL, slot_label = NULL WHERE slot_number = ? AND status = "SOLD_OUT"',
                 (slot_num,))

    # Any packs still actively dispensing from this slot (double-size slot use).
    actives = conn.execute('SELECT pack_id, slot_label FROM packs WHERE slot_number = ? AND status = "DISPENSER" AND pack_id != ?',
                           (slot_num, pack_id)).fetchall()

    if len(actives) >= 2:
        conn.close()
        flash(f"Slot #{slot_num} already holds two packs (A & B). Empty one first.", "danger")
        return redirect(url_for('games_management'))
    elif len(actives) == 1:
        # A single existing pack + this one makes a double: label them A and B.
        existing = actives[0]
        if (existing['slot_label'] or '').endswith('B'):
            existing_label = f"{slot_num}B"
            new_label = f"{slot_num}A"
        else:
            existing_label = f"{slot_num}A"
            new_label = f"{slot_num}B"
        conn.execute('UPDATE packs SET slot_label = ? WHERE pack_id = ?', (existing_label, existing['pack_id']))
    else:
        new_label = str(slot_num)

    conn.execute('UPDATE packs SET status = "DISPENSER", slot_number = ?, slot_label = ? WHERE pack_id = ?',
                 (slot_num, new_label, pack_id))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'INVENTORY', 'ACTIVATE_PACK', pack_id,
               old_value='BACKROOM', new_value=f"DISPENSER slot {new_label}")
    flash(f"Pack {pack_id} is now ACTIVE in Slot {new_label}!", "success")
    return redirect(url_for('games_management'))


@app.route('/add_game', methods=['POST'])
@app.route('/update_game', methods=['POST'])
@manager_required
def add_game():
    """Create or update a game. Records the before/after in the change log."""
    game_num = request.form.get('game_number', '').strip()
    name = request.form.get('name', '').strip()

    # If a full ticket barcode got scanned into the game-number field, extract
    # just the game number instead of storing the whole barcode (which then
    # wouldn't match any pack).
    if len(game_num) > 4:
        parsed = parse_ticket_barcode(game_num)
        if parsed:
            game_num = parsed[0]
        else:
            flash("Game # looks wrong — enter just the 3–4 digit game number.", "danger")
            return redirect(url_for('games_management'))
    if not game_num:
        flash("Enter a game number.", "danger")
        return redirect(url_for('games_management'))

    try:
        price = float(request.form.get('price'))
        tickets = int(request.form.get('tickets_per_pack'))
    except (TypeError, ValueError):
        flash("Enter a valid price and tickets-per-pack.", "danger")
        return redirect(url_for('games_management'))

    conn = get_db_connection()
    existing = conn.execute('SELECT * FROM games WHERE game_number = ?', (game_num,)).fetchone()
    conn.execute('INSERT OR REPLACE INTO games (game_number, name, price, tickets_per_pack) VALUES (?, ?, ?, ?)',
                 (game_num, name, price, tickets))
    conn.commit()
    conn.close()

    if existing:
        old = f"{existing['name']} / ${existing['price']:.2f} / {existing['tickets_per_pack']}tpp"
        new = f"{name} / ${price:.2f} / {tickets}tpp"
        log_change(current_actor(), 'GAME', 'UPDATE_GAME', game_num, old_value=old, new_value=new)
        flash(f"Game #{game_num} updated!", "success")
    else:
        log_change(current_actor(), 'GAME', 'ADD_GAME', game_num,
                   new_value=f"{name} / ${price:.2f} / {tickets}tpp")
        flash(f"Game #{game_num} added!", "success")

    return redirect(url_for('games_management'))


@app.route('/confirm_return', methods=['POST'])
@manager_required
def confirm_return():
    """Manager confirms a pending rep-return."""
    pack_id = request.form.get('pack_id')
    conn = get_db_connection()
    pack = conn.execute('SELECT status FROM packs WHERE pack_id = ?', (pack_id,)).fetchone()
    if not pack or pack['status'] != 'RETURN_PENDING':
        conn.close()
        flash(f"Pack {pack_id} is not a pending return.", "danger")
        return redirect(url_for('games_management'))

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute('UPDATE packs SET status = "RETURNED", return_confirmed_by = ?, return_confirmed_at = ? WHERE pack_id = ?',
                 (current_actor(), ts, pack_id))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'RETURN', 'RETURN_CONFIRMED', pack_id,
               old_value='RETURN_PENDING', new_value='RETURNED')
    flash(f"Return confirmed for {pack_id}.", "success")
    return redirect(url_for('games_management'))


@app.route('/cancel_return', methods=['POST'])
@manager_required
def cancel_return():
    """Manager rejects a pending return and puts the pack back where it was.
    (A pack that still holds a slot number was on the dispenser.)"""
    pack_id = request.form.get('pack_id')
    conn = get_db_connection()
    pack = conn.execute('SELECT status, slot_number FROM packs WHERE pack_id = ?', (pack_id,)).fetchone()
    if not pack or pack['status'] != 'RETURN_PENDING':
        conn.close()
        flash(f"Pack {pack_id} is not a pending return.", "danger")
        return redirect(url_for('games_management'))

    restored = 'DISPENSER' if pack['slot_number'] is not None else 'BACKROOM'
    conn.execute('UPDATE packs SET status = ?, returned_by = NULL, returned_at = NULL WHERE pack_id = ?',
                 (restored, pack_id))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'RETURN', 'RETURN_CANCELLED', pack_id,
               old_value='RETURN_PENDING', new_value=restored)
    flash(f"Return cancelled — {pack_id} restored to {restored}.", "info")
    return redirect(url_for('games_management'))


@app.route('/delete_game', methods=['POST'])
@manager_required
def delete_game():
    """Remove a game from the circulation list. Blocked if any pack still
    references it (including soft-deleted ones) so reports never orphan."""
    game_num = request.form.get('game_number', '').strip()
    conn = get_db_connection()
    game = conn.execute('SELECT * FROM games WHERE game_number = ?', (game_num,)).fetchone()
    if not game:
        conn.close()
        flash(f"Game #{game_num} not found.", "danger")
        return redirect(url_for('games_management'))

    # Only live packs block deletion. Soft-deleted (struck-through) packs are
    # already corrections and shouldn't stop the game from being removed.
    pack_count = conn.execute('SELECT COUNT(*) AS n FROM packs WHERE game_number = ? AND status != "DELETED"',
                              (game_num,)).fetchone()['n']
    if pack_count:
        conn.close()
        flash(f"Can't delete #{game_num} — {pack_count} live pack(s) still reference it. Remove those packs first.", "danger")
        return redirect(url_for('games_management'))

    conn.execute('DELETE FROM games WHERE game_number = ?', (game_num,))
    conn.commit()
    conn.close()

    log_change(current_actor(), 'CORRECTION', 'DELETE_GAME', game_num,
               old_value=f"{game['name']} / ${game['price']:.2f}", new_value='removed')
    flash(f"Game #{game_num} removed from circulation.", "info")
    return redirect(url_for('games_management'))


@app.route('/upload_games_csv', methods=['POST'])
@manager_required
def upload_games_csv():
    if 'file' not in request.files:
        flash("No file found.", "danger")
        return redirect(url_for('games_management'))

    file = request.files['file']
    if file.filename == '':
        flash("No file selected.", "danger")
        return redirect(url_for('games_management'))

    if file and file.filename.endswith('.csv'):
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.reader(stream)
        conn = get_db_connection()
        count = 0

        for i, row in enumerate(csv_input):
            if i == 0:
                continue
            if len(row) >= 4:
                try:
                    game_num = str(row[0]).strip()
                    name = str(row[1]).strip()
                    price = float(str(row[2]).replace('$', '').strip())
                    tickets_per_pack = int(str(row[3]).strip())
                    conn.execute('INSERT OR REPLACE INTO games (game_number, name, price, tickets_per_pack) VALUES (?, ?, ?, ?)',
                                 (game_num, name, price, tickets_per_pack))
                    count += 1
                except ValueError:
                    continue

        conn.commit()
        conn.close()
        log_change(current_actor(), 'GAME', 'BULK_UPLOAD_GAMES', file.filename,
                   details=f"{count} games synced")
        flash(f"Successfully uploaded and synced {count} games from CSV!", "success")
    else:
        flash("Please upload a valid .csv file.", "danger")

    return redirect(url_for('games_management'))


@app.route('/wipe_test_data', methods=['POST'])
@manager_required
def wipe_test_data():
    master_password = request.form.get('master_password')

    if master_password == WIPE_PASSWORD:
        conn = get_db_connection()
        conn.execute('DELETE FROM packs')
        conn.execute('DELETE FROM audits')
        conn.commit()
        conn.close()
        log_change(current_actor(), 'SYSTEM', 'WIPE_DATA', 'packs + audits',
                   details="all physical packs and sales audits deleted")
        flash("SYSTEM RESET: All physical packs and audit logs have been completely wiped.", "success")
    else:
        flash("ACCESS DENIED: Incorrect Master Password.", "danger")

    return redirect(url_for('games_management'))


# --- REPORTS, CHANGE LOG & BACKUP ---

@app.route('/export_shift')
def export_shift():
    """Shift sales report grouped by cashier with per-line detail, per-cashier
    subtotals, and a grand total."""
    conn = get_db_connection()
    audits = conn.execute('''
        SELECT a.timestamp, a.pack_id, a.tickets_sold, a.cash_expected,
               a.method, a.cashier_name, g.name AS game_name, g.price
        FROM audits a
        JOIN packs p ON a.pack_id = p.pack_id
        JOIN games g ON p.game_number = g.game_number
        ORDER BY a.cashier_name ASC, a.timestamp ASC
    ''').fetchall()
    conn.close()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Cashier Shift Sales Report", align='C')
    pdf.ln(10)
    pdf.set_font("Arial", '', 9)
    pdf.cell(0, 6, f"Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", align='C')
    pdf.ln(6)
    pdf.ln(3)

    def header_row():
        pdf.set_font("Arial", 'B', 9)
        pdf.cell(40, 8, 'Time', border=1)
        pdf.cell(30, 8, 'Pack', border=1)
        pdf.cell(55, 8, 'Game', border=1)
        pdf.cell(20, 8, 'Method', border=1, align='C')
        pdf.cell(20, 8, 'Tkts', border=1, align='C')
        pdf.cell(25, 8, 'Amount', border=1, align='R')
        pdf.ln()

    grand_total = 0
    grand_tickets = 0
    current_name = None
    cashier_total = 0
    cashier_tickets = 0

    def cashier_subtotal():
        pdf.set_font("Arial", 'B', 9)
        pdf.cell(145, 8, f"Subtotal for {current_name}", border=1, align='R')
        pdf.cell(20, 8, str(cashier_tickets), border=1, align='C')
        pdf.cell(25, 8, f"${cashier_total:.2f}", border=1, align='R')
        pdf.ln(10)

    for a in audits:
        name = a['cashier_name'] if a['cashier_name'] else 'Unassigned'
        if name != current_name:
            if current_name is not None:
                cashier_subtotal()
            current_name = name
            cashier_total = 0
            cashier_tickets = 0
            pdf.set_font("Arial", 'B', 11)
            pdf.cell(0, 8, f"Cashier: {current_name}")
            pdf.ln(8)
            header_row()

        pdf.set_font("Arial", '', 9)
        pdf.cell(40, 8, str(a['timestamp']), border=1)
        pdf.cell(30, 8, str(a['pack_id']), border=1)
        pdf.cell(55, 8, str(a['game_name'])[:30], border=1)
        pdf.cell(20, 8, str(a['method'] or '')[:8], border=1, align='C')
        pdf.cell(20, 8, str(a['tickets_sold']), border=1, align='C')
        pdf.cell(25, 8, f"${a['cash_expected']:.2f}", border=1, align='R')
        pdf.ln()

        cashier_total += a['cash_expected'] or 0
        cashier_tickets += a['tickets_sold'] or 0
        grand_total += a['cash_expected'] or 0
        grand_tickets += a['tickets_sold'] or 0

    if current_name is not None:
        cashier_subtotal()

    pdf.set_font("Arial", 'B', 12)
    pdf.cell(145, 10, "GRAND TOTAL", border=1, align='R')
    pdf.cell(20, 10, str(grand_tickets), border=1, align='C')
    pdf.cell(25, 10, f"${grand_total:.2f}", border=1, align='R')
    pdf.ln()

    return Response(pdf_to_bytes(pdf), mimetype="application/pdf",
                    headers={"Content-disposition": "attachment; filename=Shift_Sales_Report.pdf"})


@app.route('/export_inventory')
@manager_required
def export_inventory():
    conn = get_db_connection()
    packs = conn.execute('''
        SELECT p.pack_id, g.name, g.price, p.status, p.slot_number, p.slot_label, p.current_ticket
        FROM packs p
        JOIN games g ON p.game_number = g.game_number
        WHERE p.status IN ("DISPENSER", "BACKROOM")
        ORDER BY p.status DESC, p.slot_number ASC
    ''').fetchall()
    conn.close()

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['Pack ID', 'Game Name', 'Denomination', 'Location/Status', 'Slot Number', 'Current Ticket #'])
    for p in packs:
        slot = p['slot_label'] or p['slot_number'] or 'N/A'
        cw.writerow([p['pack_id'], p['name'], f"${p['price']:.2f}", p['status'], slot, f"{p['current_ticket']:03d}"])

    return Response(si.getvalue(), mimetype="text/csv",
                    headers={"Content-disposition": "attachment; filename=Games_Inventory_Report.csv"})


@app.route('/change_log')
@manager_required
def change_log():
    category = request.args.get('category', '').strip()
    conn = get_db_connection()
    if category:
        rows = conn.execute('SELECT * FROM change_log WHERE category = ? ORDER BY id DESC',
                            (category,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM change_log ORDER BY id DESC').fetchall()
    categories = conn.execute('SELECT DISTINCT category FROM change_log ORDER BY category').fetchall()
    conn.close()
    return render_template('change_log.html', rows=rows, categories=categories, selected=category)


@app.route('/export_change_log_csv')
@manager_required
def export_change_log_csv():
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM change_log ORDER BY id DESC').fetchall()
    conn.close()

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['Timestamp', 'Staff', 'Category', 'Action', 'Target', 'Old Value', 'New Value', 'Details'])
    for r in rows:
        cw.writerow([r['timestamp'], r['actor'], r['category'], r['action'],
                     r['target'], r['old_value'], r['new_value'], r['details']])

    return Response(si.getvalue(), mimetype="text/csv",
                    headers={"Content-disposition": "attachment; filename=Change_Log.csv"})


@app.route('/export_change_log_pdf')
@manager_required
def export_change_log_pdf():
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM change_log ORDER BY id DESC').fetchall()
    conn.close()

    pdf = FPDF(orientation='L')
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Inventory & Games Change Log", align='C')
    pdf.ln(10)
    pdf.set_font("Arial", '', 9)
    pdf.cell(0, 6, f"Generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", align='C')
    pdf.ln(6)
    pdf.ln(2)

    pdf.set_font("Arial", 'B', 8)
    pdf.cell(35, 7, 'Timestamp', border=1)
    pdf.cell(25, 7, 'Staff', border=1)
    pdf.cell(25, 7, 'Category', border=1)
    pdf.cell(30, 7, 'Action', border=1)
    pdf.cell(35, 7, 'Target', border=1)
    pdf.cell(45, 7, 'Old', border=1)
    pdf.cell(45, 7, 'New', border=1)
    pdf.cell(0, 7, 'Details', border=1)
    pdf.ln()

    pdf.set_font("Arial", '', 8)
    for r in rows:
        pdf.cell(35, 7, str(r['timestamp']), border=1)
        pdf.cell(25, 7, str(r['actor'] or '')[:14], border=1)
        pdf.cell(25, 7, str(r['category'] or '')[:14], border=1)
        pdf.cell(30, 7, str(r['action'] or '')[:18], border=1)
        pdf.cell(35, 7, str(r['target'] or '')[:20], border=1)
        pdf.cell(45, 7, str(r['old_value'] or '')[:28], border=1)
        pdf.cell(45, 7, str(r['new_value'] or '')[:28], border=1)
        pdf.cell(0, 7, str(r['details'] or '')[:40], border=1)
        pdf.ln()

    return Response(pdf_to_bytes(pdf), mimetype="application/pdf",
                    headers={"Content-disposition": "attachment; filename=Change_Log.pdf"})


def pdf_to_bytes(pdf):
    """Helper to convert FPDF instance to bytes for Response output."""
    try:
        out = pdf.output(dest='S')
    except TypeError:
        out = pdf.output()
        
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return str(out).encode('latin1')


def get_shift_reconciliation_data(sort_by='slot'):
    """Fetch active dispenser packs, calculate tickets sold and cash expected,
    and sort according to cashier/manager preference."""
    conn = get_db_connection()
    packs_data = conn.execute('''
        SELECT p.pack_id, p.game_number, p.status, p.slot_number, p.slot_label, p.current_ticket,
               COALESCE(g.name, 'Unknown Game') AS name,
               COALESCE(g.price, 0.0) AS price,
               COALESCE(g.tickets_per_pack, 0) AS tickets_per_pack
        FROM packs p
        LEFT JOIN games g ON p.game_number = g.game_number
        WHERE p.status IN ('ACTIVE', 'SOLD_OUT')
    ''').fetchall()
    conn.close()

    items = []
    total_tickets_sold = 0
    total_revenue = 0.0

    for row in packs_data:
        p = dict(row)
        curr_t = p['current_ticket'] or 0
        price = p['price'] or 0.0

        sold = curr_t
        revenue = sold * price

        p['tickets_sold'] = sold
        p['revenue'] = revenue
        items.append(p)

        total_tickets_sold += sold
        total_revenue += revenue

    # Sorting options
    if sort_by == 'price':
        # Sort by Ticket Price descending, then slot number
        items.sort(key=lambda x: (-x['price'], x['slot_number'] or 0, str(x['slot_label'] or '')))
    elif sort_by == 'sales_rank':
        # Sort by Sales/Revenue descending, then tickets sold descending
        items.sort(key=lambda x: (-x['revenue'], -x['tickets_sold'], x['slot_number'] or 0))
    else:
        # Default: Sort by Dispenser Slot Number ascending
        items.sort(key=lambda x: (x['slot_number'] or 0, str(x['slot_label'] or '')))

    # Denomination Summary grouping
    denom_summary = {}
    for item in items:
        pr = item['price']
        if pr not in denom_summary:
            denom_summary[pr] = {'price': pr, 'count': 0, 'tickets_sold': 0, 'revenue': 0.0}
        denom_summary[pr]['count'] += 1
        denom_summary[pr]['tickets_sold'] += item['tickets_sold']
        denom_summary[pr]['revenue'] += item['revenue']

    denom_list = list(denom_summary.values())
    denom_list.sort(key=lambda x: -x['price'])

    return {
        'items': items,
        'denom_summary': denom_list,
        'total_tickets_sold': total_tickets_sold,
        'total_revenue': total_revenue,
        'pack_count': len(items),
        'sort_by': sort_by,
        'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }


@app.route('/reconciliation')
def shift_reconciliation():
    sort_by = request.args.get('sort_by', 'slot')
    data = get_shift_reconciliation_data(sort_by)
    return render_template('reconciliation.html', **data)


@app.route('/reconciliation/pdf')
def shift_reconciliation_pdf():
    sort_by = request.args.get('sort_by', 'slot')
    data = get_shift_reconciliation_data(sort_by)

    sort_labels = {
        'slot': 'Dispenser Slot Number',
        'price': 'Price per Ticket (High to Low)',
        'sales_rank': 'Sales Rank per Denomination'
    }
    sort_desc = sort_labels.get(sort_by, 'Dispenser Slot Number')

    pdf = FPDF(orientation='L')
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Store Lottery Shift Reconciliation Report", align='C')
    pdf.ln(10)
    pdf.set_font("Arial", '', 10)
    pdf.cell(0, 6, f"Generated: {data['timestamp']} | Cashier: {session.get('cashier_name', 'Staff')} | Sorted By: {sort_desc}", align='C')
    pdf.ln(6)
    pdf.ln(4)

    # Summary Cards Box
    pdf.set_font("Arial", 'B', 10)
    pdf.set_fill_color(240, 244, 248)
    pdf.cell(90, 8, f" Total Revenue: ${data['total_revenue']:.2f}", border=1, fill=True)
    pdf.cell(90, 8, f" Total Tickets Sold: {data['total_tickets_sold']}", border=1, fill=True)
    pdf.cell(97, 8, f" Active Dispenser Slots: {data['pack_count']}", border=1, fill=True)
    pdf.ln(12)

    # Table Header
    pdf.set_font("Arial", 'B', 9)
    pdf.set_fill_color(220, 220, 220)
    pdf.cell(20, 8, 'Slot #', border=1, fill=True)
    pdf.cell(25, 8, 'Game #', border=1, fill=True)
    pdf.cell(75, 8, 'Game Name', border=1, fill=True)
    pdf.cell(25, 8, 'Price ($)', border=1, fill=True, align='R')
    pdf.cell(45, 8, 'Pack ID', border=1, fill=True)
    pdf.cell(30, 8, 'Curr Ticket', border=1, fill=True, align='C')
    pdf.cell(27, 8, 'Sold', border=1, fill=True, align='R')
    pdf.cell(30, 8, 'Revenue ($)', border=1, fill=True, align='R')
    pdf.ln()

    # Table Rows
    pdf.set_font("Arial", '', 9)
    for item in data['items']:
        slot_str = str(item['slot_label'] or item['slot_number'] or '')
        pdf.cell(20, 7, slot_str, border=1)
        pdf.cell(25, 7, str(item['game_number']), border=1)
        pdf.cell(75, 7, str(item['name'])[:35], border=1)
        pdf.cell(25, 7, f"${item['price']:.2f}", border=1, align='R')
        pdf.cell(45, 7, str(item['pack_id']), border=1)
        pdf.cell(30, 7, f"#{item['current_ticket']:03d}", border=1, align='C')
        pdf.cell(27, 7, str(item['tickets_sold']), border=1, align='R')
        pdf.cell(30, 7, f"${item['revenue']:.2f}", border=1, align='R')
        pdf.ln()

    # Digital Verification Badge
    pdf.ln(10)
    pdf.set_font("Arial", 'B', 10)
    pdf.cell(0, 8, f"Digital Audit Record - Logged by {session.get('cashier_name', 'Staff')} on {data['timestamp']}", align='C')
    pdf.ln(8)

    filename = f"Shift_Reconciliation_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return Response(pdf_to_bytes(pdf), mimetype="application/pdf",
                    headers={"Content-disposition": f"attachment; filename={filename}"})


@app.route('/complete_shift', methods=['POST'])
def complete_shift():
    conn = get_db_connection()
    cashier = current_actor()
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('''
        INSERT INTO change_log (timestamp, actor, category, action, target, details)
        VALUES (?, ?, 'SHIFT', 'SIGN_OFF', 'Dispenser', ?)
    ''', (timestamp, cashier, f'Shift audit signed off by {cashier}'))
    conn.commit()
    conn.close()
    flash(f"Shift signed off and logged by {cashier} at {timestamp}.", "success")
    return redirect(url_for('index'))


@app.route('/backup_database')
@manager_required
def backup_database():
    if os.path.exists(DB_PATH):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(DB_PATH, as_attachment=True, download_name=f"lottery_backup_{timestamp}.db")
    else:
        flash("Database file not found!", "danger")
        return redirect(url_for('games_management'))


@app.route('/github_webhook', methods=['GET', 'POST'])
def github_webhook():
    """
    Automated zero-click deployment endpoint for GitHub Webhooks.
    Pulls the latest code from GitHub and reloads the PythonAnywhere WSGI server automatically.
    """
    import subprocess
    import glob
    try:
        # Automatically stash any local database/runtime changes so git pull never conflicts
        subprocess.run(['git', 'stash'], cwd=BASE_DIR, check=False)

        # Pull latest commits from main branch
        pull_output = subprocess.check_output(['git', 'pull', 'origin', 'main'], cwd=BASE_DIR, stderr=subprocess.STDOUT, text=True)

        # Touch PythonAnywhere WSGI configuration files to trigger web app auto-reload
        reloaded = []
        possible_wsgi = (
            glob.glob('/var/www/*_wsgi.py') +
            glob.glob('/var/www/*_pythonanywhere_com_wsgi.py') +
            [os.path.expanduser('~/.pythonanywhere_wsgi.py')]
        )
        for wf in possible_wsgi:
            if os.path.exists(wf):
                try:
                    subprocess.run(['touch', wf], check=False)
                    reloaded.append(wf)
                except Exception:
                    pass

        return f"Auto-deployment successful!\n\nPull Output:\n{pull_output}\nReloaded WSGI targets: {reloaded}", 200
    except Exception as e:
        return f"Auto-deployment error: {str(e)}", 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

