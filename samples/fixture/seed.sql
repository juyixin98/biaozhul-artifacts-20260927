-- Synthetic rows. Deliberately fake; no production data.
INSERT INTO users (id, name, email, role, status, created_at) VALUES
 (1, 'Ada Lovelace',  'ada@example.test',  'admin',  'active',   '2026-01-04T09:00:00Z'),
 (2, 'Alan Turing',   'alan@example.test', 'member', 'active',   '2026-02-11T12:30:00Z'),
 (3, 'Grace Hopper',  'grace@example.test','admin',  'disabled', '2026-03-21T08:15:00Z'),
 (4, 'Edsger Dijkstra','edsger@example.test','member','pending', '2026-04-02T17:45:00Z');

INSERT INTO customers (id, name, email, created_at) VALUES
 (1, 'Babbage Corp', 'contact@babbage.test', '2026-01-10T00:00:00Z'),
 (2, 'Analytical Ltd', 'hello@analytical.test', '2026-02-18T00:00:00Z');

INSERT INTO orders (id, user_id, amount_cents, status, created_at) VALUES
 (100, 1, 4200, 'open',     '2026-05-01T10:00:00Z'),
 (101, 2, 1750, 'paid',     '2026-05-03T11:00:00Z'),
 (102, 1, 990,  'cancelled','2026-05-04T12:00:00Z');
