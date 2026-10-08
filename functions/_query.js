// Turns a query string into parameterised SQL over one loaded version. Field names are checked
// against the version's field list and every value is bound, so nothing from the URL is spliced
// into SQL except names that are known to exist.

export const LIMIT_DEFAULT = 100;
export const LIMIT_MAX = 10000;
const MAX_PARAMS = 90; // D1 binds at most 100
const OPS = { eq: '=', neq: '!=', gt: '>', gte: '>=', lt: '<', lte: '<=' };
const METRICS = ['count', 'sum', 'avg', 'min', 'max'];
const RESERVED = new Set(['select', 'order', 'limit', 'offset', 'format', 'group', 'metric']);

export class QueryError extends Error {}

const q = (name) => `"${name}"`;

function typed(field, raw) {
  if (field.type === 'integer' || field.type === 'number') {
    const n = Number(raw);
    if (raw === '' || !Number.isFinite(n))
      throw new QueryError(`${field.name} takes a number, not ${raw}`);
    return n;
  }
  if (field.type === 'boolean') {
    if (raw === 'true') return 1;
    if (raw === 'false') return 0;
    throw new QueryError(`${field.name} takes true or false, not ${raw}`);
  }
  return raw;
}

function fieldMap(fields) {
  const m = new Map();
  for (const f of fields) m.set(f.name, f);
  return m;
}

function need(m, name, what) {
  const f = m.get(name);
  if (!f)
    throw new QueryError(
      `no field ${name}${what ? ' to ' + what : ''}; fields are ${[...m.keys()].join(', ')}`,
    );
  return f;
}

// field=op.value, field=in.(a,b), field=is.null, field=not.is.null, field=like.*text*
function where(params, m, binds) {
  const clauses = [];
  for (const [key, value] of params) {
    if (RESERVED.has(key)) continue;
    const f = need(m, key, 'filter on');
    let v = value,
      not = false;
    if (v.startsWith('not.')) {
      not = true;
      v = v.slice(4);
    }
    const dot = v.indexOf('.');
    if (dot < 0)
      throw new QueryError(`filter ${key}=${value} needs an operator, such as ${key}=eq.${value}`);
    const op = v.slice(0, dot),
      arg = v.slice(dot + 1);
    let sql;
    if (op in OPS) {
      binds.push(typed(f, arg));
      sql = `${q(key)} ${OPS[op]} ?`;
    } else if (op === 'is' && arg === 'null') sql = `${q(key)} IS NULL`;
    else if (op === 'like' || op === 'ilike') {
      // A literal % or _ matches itself; only * is a wildcard.
      binds.push(arg.replace(/[\\%_]/g, '\\$&').replace(/\*/g, '%'));
      sql =
        op === 'like'
          ? `${q(key)} LIKE ? ESCAPE '\\'`
          : `LOWER(${q(key)}) LIKE LOWER(?) ESCAPE '\\'`;
    } else if (op === 'in') {
      const m2 = arg.match(/^\((.*)\)$/);
      if (!m2) throw new QueryError(`in takes a list in brackets, such as ${key}=in.(a,b)`);
      const items = m2[1]
        .split(',')
        .map((s) => s.trim())
        .filter((s) => s !== '');
      if (!items.length) throw new QueryError(`in needs at least one value`);
      for (const it of items) binds.push(typed(f, it));
      sql = `${q(key)} IN (${items.map(() => '?').join(',')})`;
    } else
      throw new QueryError(
        `unknown operator ${op}; use eq, neq, gt, gte, lt, lte, like, ilike, in or is.null`,
      );
    clauses.push(not ? `NOT (${sql})` : sql);
  }
  if (binds.length > MAX_PARAMS)
    throw new QueryError(`too many filter values (${binds.length}); the limit is ${MAX_PARAMS}`);
  return clauses.length ? ` WHERE ${clauses.join(' AND ')}` : '';
}

function orderBy(spec, allowed) {
  if (!spec) return '';
  const parts = spec
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((p) => {
      const [name, dir = 'asc'] = p.split('.');
      if (!allowed.has(name)) throw new QueryError(`cannot order by ${name}`);
      if (dir !== 'asc' && dir !== 'desc')
        throw new QueryError(`order direction is asc or desc, not ${dir}`);
      return `${q(name)} ${dir.toUpperCase()}`;
    });
  return parts.length ? ` ORDER BY ${parts.join(', ')}` : '';
}

function paging(params) {
  const limit = params.has('limit') ? Number(params.get('limit')) : LIMIT_DEFAULT;
  const offset = params.has('offset') ? Number(params.get('offset')) : 0;
  if (!Number.isInteger(limit) || limit < 1 || limit > LIMIT_MAX)
    throw new QueryError(`limit is 1 to ${LIMIT_MAX}`);
  if (!Number.isInteger(offset) || offset < 0)
    throw new QueryError('offset is a whole number from 0');
  return { limit, offset };
}

export function rowsQuery(table, fields, params) {
  const m = fieldMap(fields);
  const cols = params.get('select')
    ? params
        .get('select')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
        .map((n) => need(m, n, 'select').name)
    : fields.map((f) => f.name);
  if (!cols.length) throw new QueryError('select names no fields');
  const binds = [];
  const w = where(params, m, binds);
  // Without an order, rows come in the publisher's order so a page boundary never moves.
  const o = orderBy(params.get('order'), new Set(m.keys())) || ' ORDER BY rowid';
  const { limit, offset } = paging(params);
  // One row more than asked says whether there is a next page without a second count query.
  const sql = `SELECT ${cols.map(q).join(', ')} FROM ${q(table)}${w}${o} LIMIT ${limit + 1} OFFSET ${offset}`;
  return { sql, binds, limit, offset, cols };
}

export function aggregateQuery(table, fields, params) {
  const m = fieldMap(fields);
  const group = (params.get('group') || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((n) => need(m, n, 'group by').name);
  const specs = (params.get('metric') || 'count')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  const metrics = specs.map((s) => {
    const [fn, name] = s.split('.');
    if (!METRICS.includes(fn))
      throw new QueryError(`metric is one of ${METRICS.join(', ')}, as count or sum.field`);
    if (fn === 'count')
      return name
        ? { sql: `COUNT(${q(need(m, name, 'count').name)})`, as: `count_${name}` }
        : { sql: 'COUNT(*)', as: 'count' };
    const f = need(m, name, fn);
    // A boolean is stored as 0 or 1, so its sum counts the rows where it is true.
    if (fn !== 'min' && fn !== 'max' && !['integer', 'number', 'boolean'].includes(f.type))
      throw new QueryError(`${fn} needs a numeric field, and ${name} is ${f.type}`);
    return { sql: `${fn.toUpperCase()}(${q(f.name)})`, as: `${fn}_${name}` };
  });
  const binds = [];
  const w = where(params, m, binds);
  const allowed = new Set([...group, ...metrics.map((x) => x.as)]);
  const o =
    orderBy(params.get('order'), allowed) ||
    (group.length ? ` ORDER BY ${group.map(q).join(', ')}` : '');
  const { limit, offset } = paging(params);
  const select = [...group.map(q), ...metrics.map((x) => `${x.sql} AS ${q(x.as)}`)];
  const g = group.length ? ` GROUP BY ${group.map(q).join(', ')}` : '';
  const sql = `SELECT ${select.join(', ')} FROM ${q(table)}${w}${g}${o} LIMIT ${limit + 1} OFFSET ${offset}`;
  return { sql, binds, limit, offset, cols: [...group, ...metrics.map((x) => x.as)] };
}

export function toCSV(cols, rows) {
  const cell = (v) => {
    if (v === null || v === undefined) return '';
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return (
    [cols.join(','), ...rows.map((r) => cols.map((c) => cell(r[c])).join(','))].join('\n') + '\n'
  );
}
