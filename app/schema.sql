PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS brands (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS staff (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  role TEXT
);

CREATE TABLE IF NOT EXISTS locations (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  job_no TEXT UNIQUE NOT NULL,
  owner TEXT,
  project_name TEXT
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  brand_id INTEGER REFERENCES brands(id),
  model TEXT NOT NULL,
  description TEXT,
  base_unit TEXT DEFAULT '個',
  track_by_serial INTEGER NOT NULL DEFAULT 0,
  safety_stock REAL NOT NULL DEFAULT 0,
  is_kit INTEGER NOT NULL DEFAULT 0,
  comment TEXT,
  UNIQUE(brand_id, model)
);

CREATE TABLE IF NOT EXISTS kit_components (
  id INTEGER PRIMARY KEY,
  parent_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
  component_product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  UNIQUE(parent_product_id, component_product_id)
);

CREATE TABLE IF NOT EXISTS purchase_orders (
  id INTEGER PRIMARY KEY,
  po_no TEXT UNIQUE NOT NULL,
  requester_id INTEGER REFERENCES staff(id),
  date TEXT
);

CREATE TABLE IF NOT EXISTS inbound_orders (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL CHECK(type IN ('hsinchu','office','surplus_return')),
  date TEXT NOT NULL,
  supplier_id INTEGER REFERENCES suppliers(id),
  signer_id INTEGER REFERENCES staff(id),
  po_id INTEGER REFERENCES purchase_orders(id),
  project_id INTEGER REFERENCES projects(id),
  loan_id INTEGER REFERENCES loans(id),
  note TEXT,
  photo_sent INTEGER NOT NULL DEFAULT 0,
  photo_sent_date TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outbound_orders (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL CHECK(type IN ('normal','surplus_transfer')),
  date TEXT NOT NULL,
  notifier_id INTEGER REFERENCES staff(id),
  recipient TEXT,
  signer_id INTEGER REFERENCES staff(id),
  sign_date TEXT,
  project_id INTEGER REFERENCES projects(id),
  shipping_carrier TEXT,
  shipping_no TEXT,
  note TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outbound_lines (
  id INTEGER PRIMARY KEY,
  outbound_id INTEGER NOT NULL REFERENCES outbound_orders(id) ON DELETE CASCADE,
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  unit TEXT,
  from_location_id INTEGER REFERENCES locations(id),
  from_surplus INTEGER NOT NULL DEFAULT 0,
  note TEXT
);

CREATE TABLE IF NOT EXISTS inbound_lines (
  id INTEGER PRIMARY KEY,
  inbound_id INTEGER NOT NULL REFERENCES inbound_orders(id) ON DELETE CASCADE,
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  unit TEXT,
  location_id INTEGER REFERENCES locations(id),
  is_surplus INTEGER NOT NULL DEFAULT 0,
  source_outbound_line_id INTEGER REFERENCES outbound_lines(id),
  note TEXT
);

CREATE TABLE IF NOT EXISTS serial_items (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  serial_no TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'in_stock' CHECK(status IN ('in_stock','shipped','returned')),
  current_location_id INTEGER REFERENCES locations(id),
  inbound_line_id INTEGER REFERENCES inbound_lines(id),
  outbound_line_id INTEGER REFERENCES outbound_lines(id),
  is_surplus INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  UNIQUE(product_id, serial_no)
);

CREATE TABLE IF NOT EXISTS loans (
  id INTEGER PRIMARY KEY,
  loan_no TEXT UNIQUE NOT NULL,
  borrower TEXT,
  out_date TEXT,
  return_date TEXT,
  status TEXT NOT NULL DEFAULT 'out' CHECK(status IN ('out','returned','settled')),
  note TEXT
);

-- 即時庫存視圖（依 product / location / 是否為餘料 分組）
DROP VIEW IF EXISTS stock_balance;
CREATE VIEW stock_balance AS
SELECT product_id, location_id, is_surplus, SUM(qty) AS qty
FROM (
  SELECT product_id, location_id, is_surplus, qty FROM inbound_lines
  UNION ALL
  SELECT product_id, from_location_id AS location_id, from_surplus AS is_surplus, -qty AS qty FROM outbound_lines
)
GROUP BY product_id, location_id, is_surplus;

CREATE INDEX IF NOT EXISTS idx_in_lines_prod ON inbound_lines(product_id);
CREATE INDEX IF NOT EXISTS idx_out_lines_prod ON outbound_lines(product_id);
CREATE INDEX IF NOT EXISTS idx_serial_prod ON serial_items(product_id);
CREATE INDEX IF NOT EXISTS idx_serial_no ON serial_items(serial_no);
