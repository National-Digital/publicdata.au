// Turns a query string into parameterised SQL over one loaded version. Field names are checked
// against the version's field list and every value is bound, so nothing from the URL is spliced
// into SQL except names that are known to exist.

export const LIMIT_DEFAULT = 100;
export const LIMIT_MAX = 10000;
const MAX_PARAMS = 90; // D1 binds at most 100
export const OPS = { eq: '=', neq: '!=', gt: '>', gte: '>=', lt: '<', lte: '<=' };
export const METRICS = ['count', 'sum', 'avg', 'min', 'max'];
export const RESERVED = new Set([
  'select',
  'order',
  'limit',
  'offset',
  'format',
  'group',
  'metric',
]);

export class QueryError extends Error {}

const q = (name) => `"${name}"`;

export function typed(field, raw) {
  if (field.type === 'integer' || field.type === 'number') {
    const n = Number(raw);
    if (raw === '' || !Number.isFinite(n)) {
      throw new QueryError(`${field.name} takes a number, not ${raw}`);
    }
    return n;
  }
  if (field.type === 'boolean') {
    if (raw === 'true') {
      return 1;
    }
    if (raw === 'false') {
      return 0;
    }
    throw new QueryError(`${field.name} takes true or false, not ${raw}`);
  }
  return raw;
}

export function fieldMap(fields) {
  const m = new Map();
  for (const f of fields) {
    m.set(f.name, f);
  }
  return m;
}

export function need(m, name, what) {
  const f = m.get(name);
  if (!f) {
    throw new QueryError(
      `no field ${name}${what ? ' to ' + what : ''}; fields are ${[...m.keys()].join(', ')}`,
    );
  }
  return f;
}

// field=op.value, field=in.(a,b), field=is.null, field=not.is.null, field=like.*text*, parsed to
// { name, op, not, args } with every value typed. The SQL and the Parquet reader both start here.
export function filterSpecs(params, m) {
  const specs = [];
  let n = 0;
  for (const [key, value] of params) {
    if (RESERVED.has(key)) {
      continue;
    }
    const f = need(m, key, 'filter on');
    let v = value,
      not = false;
    if (v.startsWith('not.')) {
      not = true;
      v = v.slice(4);
    }
    const dot = v.indexOf('.');
    if (dot < 0) {
      throw new QueryError(`filter ${key}=${value} needs an operator, such as ${key}=eq.${value}`);
    }
    const op = v.slice(0, dot),
      arg = v.slice(dot + 1);
    let args;
    if (op in OPS) {
      args = [typed(f, arg)];
    } else if (op === 'is' && arg === 'null') {
      args = [];
    } else if (op === 'like' || op === 'ilike') {
      args = [arg];
    } else if (op === 'in') {
      const m2 = arg.match(/^\((.*)\)$/);
      if (!m2) {
        throw new QueryError(`in takes a list in brackets, such as ${key}=in.(a,b)`);
      }
      const items = m2[1]
        .split(',')
        .map((s) => s.trim())
        .filter((s) => s !== '');
      if (!items.length) {
        throw new QueryError(`in needs at least one value`);
      }
      args = items.map((it) => typed(f, it));
    } else {
      throw new QueryError(
        `unknown operator ${op}; use eq, neq, gt, gte, lt, lte, like, ilike, in or is.null`,
      );
    }
    n += args.length;
    specs.push({ name: key, op, not, args });
  }
  if (n > MAX_PARAMS) {
    throw new QueryError(`too many filter values (${n}); the limit is ${MAX_PARAMS}`);
  }
  return specs;
}

// A literal % or _ matches itself; only * is a wildcard.
export const likePattern = (arg) => arg.replace(/[\\%_]/g, '\\$&').replace(/\*/g, '%');

function where(params, m, binds) {
  const clauses = filterSpecs(params, m).map(({ name, op, not, args }) => {
    let sql;
    if (op in OPS) {
      binds.push(args[0]);
      sql = `${q(name)} ${OPS[op]} ?`;
    } else if (op === 'is') {
      sql = `${q(name)} IS NULL`;
    } else if (op === 'in') {
      binds.push(...args);
      sql = `${q(name)} IN (${args.map(() => '?').join(',')})`;
    } else {
      binds.push(likePattern(args[0]));
      sql =
        op === 'like'
          ? `${q(name)} LIKE ? ESCAPE '\\'`
          : `LOWER(${q(name)}) LIKE LOWER(?) ESCAPE '\\'`;
    }
    return not ? `NOT (${sql})` : sql;
  });
  return clauses.length ? ` WHERE ${clauses.join(' AND ')}` : '';
}

export function orderSpecs(spec, allowed) {
  if (!spec) {
    return [];
  }
  return spec
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((p) => {
      const [name, dir = 'asc'] = p.split('.');
      if (!allowed.has(name)) {
        throw new QueryError(`cannot order by ${name}`);
      }
      if (dir !== 'asc' && dir !== 'desc') {
        throw new QueryError(`order direction is asc or desc, not ${dir}`);
      }
      return { name, dir };
    });
}

export function paging(params) {
  const limit = params.has('limit') ? Number(params.get('limit')) : LIMIT_DEFAULT;
  const offset = params.has('offset') ? Number(params.get('offset')) : 0;
  if (!Number.isInteger(limit) || limit < 1 || limit > LIMIT_MAX) {
    throw new QueryError(`limit is 1 to ${LIMIT_MAX}`);
  }
  if (!Number.isInteger(offset) || offset < 0) {
    throw new QueryError('offset is a whole number from 0');
  }
  return { limit, offset };
}

export function selectFields(params, fields, m) {
  const cols = params.get('select')
    ? params
        .get('select')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
        .map((n) => need(m, n, 'select').name)
    : fields.map((f) => f.name);
  if (!cols.length) {
    throw new QueryError('select names no fields');
  }
  return cols;
}

// `fileOrder` names the columns the version's Parquet query copy is sorted by when the table was
// loaded in another order; it is empty when the rowid already follows the copy.
export function rowsQuery(table, fields, params, fileOrder = []) {
  const m = fieldMap(fields);
  const cols = selectFields(params, fields, m);
  const binds = [];
  const w = where(params, m, binds);
  // Without an order, and for ties, rows come in the order of the version's Parquet file, as the
  // Parquet engine gives them, so a page boundary never moves and both engines agree.
  const asked = orderSpecs(params.get('order'), new Set(m.keys())).map(
    ({ name, dir }) => `${q(name)} ${dir.toUpperCase()}`,
  );
  const sorted = fileOrder.filter((c) => m.has(c)).map((c) => `${q(c)} ASC NULLS LAST`);
  const o = ` ORDER BY ${[...asked, ...sorted, 'rowid'].join(', ')}`;
  const { limit, offset } = paging(params);
  // One row more than asked says whether there is a next page without a second count query.
  const sql = `SELECT ${cols.map(q).join(', ')} FROM ${q(table)}${w}${o} LIMIT ${limit + 1} OFFSET ${offset}`;
  return { sql, binds, limit, offset, cols };
}

export function groupFields(params, m) {
  return (params.get('group') || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((n) => need(m, n, 'group by').name);
}

// { fn, field, as } per metric; field is null for count(*).
export function metricSpecs(params, m) {
  const specs = (params.get('metric') || 'count')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  return specs.map((s) => {
    const [fn, name] = s.split('.');
    if (!METRICS.includes(fn)) {
      throw new QueryError(`metric is one of ${METRICS.join(', ')}, as count or sum.field`);
    }
    if (fn === 'count') {
      return name
        ? { fn, field: need(m, name, 'count').name, as: `count_${name}` }
        : { fn, field: null, as: 'count' };
    }
    const f = need(m, name, fn);
    // A boolean is stored as 0 or 1, so its sum counts the rows where it is true.
    if (fn !== 'min' && fn !== 'max' && !['integer', 'number', 'boolean'].includes(f.type)) {
      throw new QueryError(`${fn} needs a numeric field, and ${name} is ${f.type}`);
    }
    return { fn, field: f.name, as: `${fn}_${name}` };
  });
}

export function aggregateQuery(table, fields, params) {
  const m = fieldMap(fields);
  const group = groupFields(params, m);
  const metrics = metricSpecs(params, m).map((x) => ({
    ...x,
    sql: x.field === null ? 'COUNT(*)' : `${x.fn.toUpperCase()}(${q(x.field)})`,
  }));
  const binds = [];
  const w = where(params, m, binds);
  const allowed = new Set([...group, ...metrics.map((x) => x.as)]);
  // Ties keep group order, as the rollups and the Parquet engine keep it.
  const by = [
    ...orderSpecs(params.get('order'), allowed).map(
      ({ name, dir }) => `${q(name)} ${dir.toUpperCase()}`,
    ),
    ...group.map(q),
  ];
  const o = by.length ? ` ORDER BY ${by.join(', ')}` : '';
  const { limit, offset } = paging(params);
  const select = [...group.map(q), ...metrics.map((x) => `${x.sql} AS ${q(x.as)}`)];
  const g = group.length ? ` GROUP BY ${group.map(q).join(', ')}` : '';
  const sql = `SELECT ${select.join(', ')} FROM ${q(table)}${w}${g}${o} LIMIT ${limit + 1} OFFSET ${offset}`;
  return { sql, binds, limit, offset, cols: [...group, ...metrics.map((x) => x.as)] };
}

export function toCSV(cols, rows) {
  const cell = (v) => {
    if (v === null || v === undefined) {
      return '';
    }
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return (
    [cols.join(','), ...rows.map((r) => cols.map((c) => cell(r[c])).join(','))].join('\n') + '\n'
  );
}
