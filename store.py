"""Atomic capacity reservations over half-open integer-minute intervals."""
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Missing(ValueError):
    pass


class Conflict(ValueError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be an integer from {low} to {high}.')
    return value


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', value):
        raise ValueError('Names and request keys need 1–48 ASCII letters, digits, underscores or hyphens.')
    return value


def fields(body, expected):
    if not isinstance(body, dict) or set(body) != set(expected):
        raise ValueError('Expected exactly these fields: '+', '.join(expected))


def reservation(body):
    fields(body, ['request_key', 'start', 'end', 'units', 'note'])
    name(body['request_key'])
    integer(body['start'], 0, 525599, 'Start')
    integer(body['end'], 1, 525600, 'End')
    integer(body['units'], 1, 1000, 'Units')
    if body['start'] >= body['end']:
        raise ValueError('Start must be strictly before end.')
    if not isinstance(body['note'], str) or len(body['note']) > 120 or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in body['note']):
        raise ValueError('Notes allow at most 120 characters without control characters or surrogates.')
    return dict(body)


def occupancy(bookings, start=None, end=None):
    """Sweep grouped endpoints; ending reservations free capacity at their end."""
    active = [b for b in bookings if b['status'] == 'active']
    if start is None:
        if not active:
            return []
        start = min(b['start'] for b in active)
        end = max(b['end'] for b in active)
    events = {start: [], end: []}
    for b in active:
        a, z = max(start, b['start']), min(end, b['end'])
        if a < z:
            events.setdefault(a, []).append((b['id'], b['units']))
            events.setdefault(z, []).append((b['id'], -b['units']))
    holding, result = {}, []
    points = sorted(events)
    for i, point in enumerate(points[:-1]):
        for ident, delta in events[point]:
            if delta > 0:
                holding[ident] = delta
            else:
                holding.pop(ident, None)
        result.append({'start': point, 'end': points[i+1], 'used': sum(holding.values()), 'booking_ids': sorted(holding)})
    return result


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS resources(name TEXT PRIMARY KEY, capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 1000));
                CREATE TABLE IF NOT EXISTS bookings(
                  id INTEGER PRIMARY KEY, resource TEXT NOT NULL REFERENCES resources(name),
                  request_key TEXT NOT NULL, payload TEXT NOT NULL,
                  start INTEGER NOT NULL, end INTEGER NOT NULL, units INTEGER NOT NULL,
                  note TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','cancelled')),
                  version INTEGER NOT NULL, UNIQUE(resource,request_key), CHECK(start<end));
                CREATE INDEX IF NOT EXISTS overlap ON bookings(resource,status,start,end);
            ''')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def resource(db, key):
        row = db.execute('SELECT * FROM resources WHERE name=?', (name(key),)).fetchone()
        if row is None:
            raise Missing('Resource not found.')
        return dict(row)

    @staticmethod
    def public(row):
        return {key: value for key, value in dict(row).items() if key != 'payload'}

    def create(self, body):
        fields(body, ['name', 'capacity'])
        key = name(body['name'])
        capacity = integer(body['capacity'], 1, 1000, 'Capacity')
        with self.connection(True) as db:
            if db.execute('SELECT 1 FROM resources WHERE name=?', (key,)).fetchone():
                raise Conflict('Resource already exists; capacity is immutable.')
            if db.execute('SELECT count(*) FROM resources').fetchone()[0] >= 100:
                raise Conflict('Workspace limit: 100 resources.')
            db.execute('INSERT INTO resources VALUES (?,?)', (key, capacity))
        return {'name': key, 'capacity': capacity}

    def list(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM resources ORDER BY name')]

    def snapshot(self, key):
        with self.connection() as db:
            resource = self.resource(db, key)
            bookings = [self.public(row) for row in db.execute('SELECT * FROM bookings WHERE resource=? ORDER BY id', (key,))]
            segments = occupancy(bookings)
            return {'resource': resource, 'bookings': bookings, 'segments': segments,
                    'peak': max((s['used'] for s in segments), default=0),
                    'unit_minutes': sum((b['end']-b['start'])*b['units'] for b in bookings if b['status']=='active'),
                    'capacity_ok': all(s['used'] <= resource['capacity'] for s in segments)}

    def preview_in(self, db, key, body):
        resource = self.resource(db, key)
        bookings = [dict(row) for row in db.execute('SELECT * FROM bookings WHERE resource=? AND status=\'active\' AND start<? AND end>? ORDER BY id', (key, body['end'], body['start']))]
        segments = occupancy(bookings, body['start'], body['end'])
        conflicts = [{**s, 'requested': body['units'], 'excess': s['used']+body['units']-resource['capacity']} for s in segments if s['used']+body['units']>resource['capacity']]
        return {'fits': not conflicts, 'capacity': resource['capacity'], 'available_min': resource['capacity']-max((s['used'] for s in segments), default=0), 'segments': segments, 'conflicts': conflicts}

    def preview(self, key, body):
        body = reservation(body)
        with self.connection() as db:
            return self.preview_in(db, key, body)

    def reserve(self, key, body):
        body = reservation(body)
        payload = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
        with self.connection(True) as db:
            self.resource(db, key)
            old = db.execute('SELECT * FROM bookings WHERE resource=? AND request_key=?', (key,body['request_key'])).fetchone()
            if old is not None:
                if old['payload'] != payload:
                    raise Conflict('This request key was used with a different payload.')
                return {'replayed': True, 'booking': self.public(old)}
            if db.execute('SELECT count(*) FROM bookings').fetchone()[0] >= 10000:
                raise Conflict('Workspace limit: 10000 bookings including cancelled history.')
            if db.execute('SELECT count(*) FROM bookings WHERE resource=?', (key,)).fetchone()[0] >= 500:
                raise Conflict('Resource limit: 500 bookings including cancelled history.')
            check = self.preview_in(db, key, body)
            if not check['fits']:
                raise Conflict('Insufficient capacity for this interval.', check)
            cursor = db.execute('INSERT INTO bookings(resource,request_key,payload,start,end,units,note,status,version) VALUES (?,?,?,?,?,?,?,\'active\',1)', (key,body['request_key'],payload,body['start'],body['end'],body['units'],body['note']))
            row = db.execute('SELECT * FROM bookings WHERE id=?', (cursor.lastrowid,)).fetchone()
            return {'replayed': False, 'booking': self.public(row)}

    def cancel(self, key, ident, body):
        fields(body, ['expected_version'])
        integer(body['expected_version'], 1, 2, 'Expected version')
        integer(ident, 1, 10000, 'Booking id')
        with self.connection(True) as db:
            self.resource(db, key)
            row = db.execute('SELECT * FROM bookings WHERE id=? AND resource=?', (ident,key)).fetchone()
            if row is None:
                raise Missing('Booking not found.')
            if row['version'] != body['expected_version']:
                raise Conflict(f'Stale version: current version is {row["version"]}.')
            if row['status'] == 'active':
                db.execute('UPDATE bookings SET status=\'cancelled\',version=2 WHERE id=?', (ident,))
            return self.public(db.execute('SELECT * FROM bookings WHERE id=?', (ident,)).fetchone())
