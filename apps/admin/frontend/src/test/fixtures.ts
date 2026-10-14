/** API payloads shared by the page tests, shaped exactly as the contract says. */
export const APP_SALES = {
  app_id: 'sales',
  name: 'Sales dashboard',
  url: 'https://sales.example/',
  project_id: '123',
  config_id: '456',
  source: 'manual',
  active: true,
};

export const APP_HR = {
  app_id: 'hr',
  name: 'HR dashboard',
  url: '',
  project_id: '',
  config_id: '',
  source: 'discovery',
  active: false,
};

export const USER_ALICE = { email: 'alice@example.com', display_name: 'Alice', active: true };

export const GROUP_SALES = {
  group_id: 'g1',
  provider: 'entra',
  external_id: 'sales-team',
  name: 'Sales team',
};

export const OVERVIEW = {
  counts: { apps: 2, users: 1, groups: 1, grants: 1, attributes: 3 },
  apps: [APP_HR, APP_SALES],
  users: [USER_ALICE],
  groups: [GROUP_SALES],
  user_matrix: {
    'alice@example.com': {
      hr: { allowed: false, reason: 'no_grant', matched: [] },
      sales: { allowed: true, reason: 'granted', matched: ['user:alice@example.com'] },
    },
  },
  group_grants: { g1: ['sales'] },
};

export const LOCAL_GROUP_SALES = {
  local_group_id: 'sales-managers',
  name: 'Sales managers',
  description: 'Regional sales leads',
  members: ['alice@example.com'],
  member_count: 1,
};

const DRUG_SALES_COLUMNS = [
  { name: 'company', type: 'VARCHAR' },
  { name: 'drug', type: 'VARCHAR' },
  { name: 'revenue', type: 'NUMBER(38,2)' },
  { name: 'is_test', type: 'BOOLEAN' },
];

export const DATASETS = {
  datasets: [
    {
      name: 'drug_sales',
      description: 'Sales of drugs per company',
      columns: DRUG_SALES_COLUMNS,
      show_lines: 2,
      hide_lines: 1,
      masked_columns: ['customer_id'],
      has_examples: true,
    },
    {
      name: 'patients',
      description: '',
      columns: [
        { name: 'patient_id', type: 'VARCHAR' },
        { name: 'name', type: 'VARCHAR' },
      ],
      show_lines: 0,
      hide_lines: 0,
      masked_columns: [],
      has_examples: false,
    },
  ],
  data_available: true,
};

export const SHOW_EVERYONE_PUBLIC = {
  rule_id: 'aaaaaaaaaaa1',
  kind: 'show',
  subject: { type: 'everyone', id: '', label: 'Everyone' },
  filter: { conditions: [{ column: 'company', op: 'is', source: 'attribute:company' }] },
};

export const SHOW_SALES_ALL = {
  rule_id: 'aaaaaaaaaaa2',
  kind: 'show',
  subject: { type: 'local_group', id: 'sales-managers', label: 'Sales managers' },
  filter: { all: true },
};

export const HIDE_TEST_ROWS = {
  rule_id: 'aaaaaaaaaaa3',
  kind: 'hide',
  subject: { type: 'everyone', id: '', label: 'Everyone' },
  filter: { conditions: [{ column: 'is_test', op: 'is', value: 'true' }] },
};

export const MASK_REVENUE = [
  {
    mask_id: 'bbbbbbbbbbb1',
    subject: { type: 'group', id: 'g1', label: 'Sales team' },
    treatment: 'clear',
  },
  {
    mask_id: 'bbbbbbbbbbb2',
    subject: { type: 'everyone', id: '', label: 'Everyone' },
    treatment: 'hidden',
  },
];

export const DRUG_SALES = {
  dataset: { name: 'drug_sales', description: 'Sales of drugs per company', columns: DRUG_SALES_COLUMNS },
  rows: [SHOW_EVERYONE_PUBLIC, SHOW_SALES_ALL, HIDE_TEST_ROWS],
  masks: { revenue: MASK_REVENUE },
  attributes: ['company', 'region'],
  has_examples: true,
  data_available: true,
};

export const DRUG_SALES_EMPTY = { ...DRUG_SALES, rows: [], masks: {} };

export const TEST_RESULT_ALICE = {
  principal: {
    email: 'alice@example.com',
    local_groups: ['sales-managers'],
    idp_groups: [],
    attributes: { company: 'Pharma A s.r.o.' },
  },
  accessible: true,
  rows: {
    visible: 40,
    total: 120,
    lines: [
      {
        rule_id: 'aaaaaaaaaaa1',
        kind: 'show',
        applies: true,
        reason: "Applies to everyone; compares company with the user's company (Pharma A s.r.o.).",
        matched_rows: 0,
        warning: "No row has company = 'Pharma A s.r.o.' (values: PHARMA_A, PHARMA_B, PHARMA_C)",
      },
      {
        rule_id: 'aaaaaaaaaaa2',
        kind: 'show',
        applies: true,
        reason: 'The user is in local group Sales managers.',
        matched_rows: 120,
        warning: null,
      },
      {
        rule_id: 'aaaaaaaaaaa3',
        kind: 'hide',
        applies: true,
        reason: 'Applies to everyone.',
        matched_rows: 80,
        warning: null,
      },
    ],
  },
  columns: [
    { name: 'company', treatment: 'clear', mask_id: null, reason: 'No mask line on this column.' },
    { name: 'drug', treatment: 'mask_partial', mask_id: 'bbbbbbbbbbb9', reason: 'Line 1 names everyone.' },
    { name: 'revenue', treatment: 'hidden', mask_id: 'bbbbbbbbbbb2', reason: 'Line 2 names everyone.' },
    { name: 'is_test', treatment: 'clear', mask_id: null, reason: 'No mask line on this column.' },
  ],
  preview: {
    columns: ['company', 'drug', 'is_test'],
    rows: [
      ['PHARMA_A', 'Zy***', false],
      ['PHARMA_B', null, false],
    ],
    truncated: true,
  },
  row_filter: "(TRUE) AND NOT (is_test = TRUE)",
  data_available: true,
};
