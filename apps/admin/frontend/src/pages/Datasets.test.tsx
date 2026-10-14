import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import {
  DATASETS,
  DRUG_SALES,
  DRUG_SALES_EMPTY,
  GROUP_SALES,
  HIDE_TEST_ROWS,
  LOCAL_GROUP_SALES,
  MASK_REVENUE,
  SHOW_EVERYONE_PUBLIC,
  SHOW_SALES_ALL,
} from '../test/fixtures';
import { mockFetch, renderApp, SESSION, type Routes } from '../test/utils';

type User = ReturnType<typeof userEvent.setup>;

const ALICE = { email: 'alice@example.com', display_name: 'Alice', active: true, attributes: { company: 'PHARMA_A' } };
const BOB = { email: 'bob@example.com', display_name: '', active: true, attributes: {} };
const URL = '/api/datasets/drug_sales';

/** Pick an option of a Select or a Combobox by its accessible name. */
async function choose(user: User, name: string | RegExp, option: string | RegExp, scope: HTMLElement = document.body) {
  await user.click(within(scope).getByRole('combobox', { name }));
  await user.click(await screen.findByRole('option', { name: option }));
}

const line = (name: string) => screen.getByRole('listitem', { name });

describe('Datasets list', () => {
  it('has a nav entry and lists datasets with line counts', async () => {
    mockFetch({ 'GET /api/session': { body: SESSION }, 'GET /api/datasets': { body: DATASETS } });
    renderApp('/datasets');
    const nav = await screen.findByRole('navigation', { name: 'Main' });
    expect(within(nav).getByRole('link', { name: 'Datasets' })).toHaveAttribute('href', '/datasets');
    expect(within(nav).queryByRole('link', { name: 'Data policies' })).not.toBeInTheDocument();
    const table = await screen.findByRole('table', { name: 'Datasets' });
    const row = within(table).getByText('drug_sales').closest('tr') as HTMLElement;
    expect(row).toHaveTextContent('2 show lines');
    expect(row).toHaveTextContent('1 hide line');
    expect(row).toHaveTextContent('1 column');
    expect(within(row).getByRole('link', { name: 'Edit drug_sales' })).toHaveAttribute('href', '/datasets/drug_sales');
    const patients = within(table).getByText('patients').closest('tr') as HTMLElement;
    expect(patients).toHaveTextContent('nobody sees rows');
  });

  it('says when data is not readable and shows an empty list', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/datasets': { body: { datasets: [], data_available: false } },
    });
    renderApp('/datasets');
    expect(await screen.findByText('No datasets.')).toBeInTheDocument();
    expect(screen.getByText('Data not readable right now')).toBeInTheDocument();
  });
});

describe('Dataset editor', () => {
  const routes = (extra: Routes = {}): Routes => ({
    'GET /api/session': { body: SESSION },
    [`GET ${URL}`]: { body: DRUG_SALES },
    'GET /api/users': { body: { users: [ALICE, BOB] } },
    'GET /api/local-groups': { body: [LOCAL_GROUP_SALES] },
    'GET /api/groups': { body: { groups: [GROUP_SALES], providers: {} } },
    [`GET ${URL}/values/company`]: { body: { values: ['PHARMA_A', 'PHARMA_B', null], truncated: false } },
    [`GET ${URL}/values/drug`]: { body: { values: ['Zyrtec', 'Euthyrox'], truncated: false } },
    [`GET ${URL}/values/is_test`]: { body: { values: [true, false], truncated: false } },
    [`GET ${URL}/values/revenue`]: { body: { values: [10, 20], truncated: false } },
    ...extra,
  });

  it('renders the saved row lines and masks', async () => {
    mockFetch(routes());
    renderApp('/datasets/drug_sales');
    const first = await screen.findByRole('listitem', { name: 'Row line 1' });
    expect(within(first).getByRole('combobox', { name: 'Row line 1: who' })).toHaveTextContent('Everyone');
    expect(within(first).getByRole('combobox', { name: 'Row line 1: which rows' })).toHaveTextContent(
      'sees only rows where',
    );
    expect(within(first).getByRole('combobox', { name: 'Row line 1, condition 1: column' })).toHaveTextContent('company');
    expect(within(first).getByRole('combobox', { name: 'Row line 1, condition 1: compare with' })).toHaveTextContent(
      "the user's company",
    );
    const second = line('Row line 2');
    expect(within(second).getByRole('combobox', { name: 'Row line 2: who' })).toHaveTextContent('A local group');
    expect(within(second).getByRole('combobox', { name: 'Row line 2: which local group' })).toHaveValue('Sales managers');
    expect(within(second).getByRole('combobox', { name: 'Row line 2: which rows' })).toHaveTextContent('sees all rows');
    const hide = within(screen.getByRole('region', { name: 'Always hide rows where' })).getByRole('listitem', {
      name: 'Row line 3',
    });
    expect(within(hide).getByRole('combobox', { name: 'Row line 3, condition 1: value' })).toHaveValue('true');

    const revenue = screen.getByRole('region', { name: 'Masking of revenue' });
    expect(within(revenue).getByText('First matching line wins. Anyone no line names sees the real value.')).toBeInTheDocument();
    expect(within(revenue).getByRole('combobox', { name: 'Mask on revenue, line 1: which IdP group' })).toHaveValue(
      'Sales team',
    );
    expect(within(revenue).getByRole('combobox', { name: 'Mask on revenue, line 2: who' })).toHaveTextContent(
      'Everyone else',
    );
    expect(within(revenue).getByRole('combobox', { name: 'Mask on revenue, line 2: treatment' })).toHaveTextContent(
      'Column hidden',
    );
    const company = screen.getByRole('region', { name: 'Masking of company' });
    expect(within(company).getByText('Clear for everyone')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load example rules/ })).not.toBeInTheDocument();
  });

  it('shows a 404 as a load error', async () => {
    mockFetch(routes({ [`GET ${URL}`]: { status: 404, body: { detail: "Unknown dataset 'drug_sales'." } } }));
    renderApp('/datasets/drug_sales');
    expect(await screen.findByText("Unknown dataset 'drug_sales'.")).toBeInTheDocument();
  });

  it('saves a new show line built from a condition with a typed value, as one exact PUT', async () => {
    const { calls } = mockFetch(
      routes({
        [`GET ${URL}`]: { body: DRUG_SALES_EMPTY },
        [`PUT ${URL}`]: { body: { ...DRUG_SALES, message: 'Rules of drug_sales saved.' } },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await user.click(await screen.findByRole('button', { name: 'Add show line' }));
    const added = line('Row line 1');
    await choose(user, 'Row line 1: who', 'A user', added);
    await choose(user, 'Row line 1: which user', 'Alice (alice@example.com)', added);
    await choose(user, 'Row line 1: which rows', 'sees only rows where', added);
    await choose(user, 'Row line 1, condition 1: column', /^drug/, added);
    const value = within(added).getByRole('combobox', { name: 'Row line 1, condition 1: value' });
    await user.type(value, 'Aspirin');
    await user.click(await screen.findByRole('option', { name: 'Use "Aspirin"' }));

    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('Rules of drug_sales saved.')).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
      rows: [
        {
          kind: 'show',
          subject: { type: 'user', id: 'alice@example.com' },
          filter: { conditions: [{ column: 'drug', op: 'is', value: 'Aspirin' }] },
        },
      ],
      masks: {},
    });
    // Reseeded from the answer: the saved lines replace the drafts.
    expect(await screen.findByRole('listitem', { name: 'Row line 3' })).toBeInTheDocument();
  });

  it('builds conditions: a suggested value, a user attribute, a list and an emptiness check', async () => {
    const { calls } = mockFetch(
      routes({
        [`GET ${URL}`]: { body: DRUG_SALES_EMPTY },
        [`PUT ${URL}`]: { body: { ...DRUG_SALES_EMPTY, message: 'Saved.' } },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await user.click(await screen.findByRole('button', { name: 'Add show line' }));
    const added = line('Row line 1');
    await choose(user, 'Row line 1: which rows', 'sees only rows where', added);

    // 1: company is a value suggested from the data.
    const value = within(added).getByRole('combobox', { name: 'Row line 1, condition 1: value' });
    await user.click(value);
    await user.click(await screen.findByRole('option', { name: 'PHARMA_B' }));

    // 2: company is the user's region (an attribute).
    await user.click(within(added).getByRole('button', { name: 'Add condition' }));
    await choose(user, 'Row line 1, condition 2: compare with', "the user's region", added);
    expect(within(added).queryByRole('combobox', { name: 'Row line 1, condition 2: value' })).not.toBeInTheDocument();

    // 3: drug is one of two values.
    await user.click(within(added).getByRole('button', { name: 'Add condition' }));
    await choose(user, 'Row line 1, condition 3: column', /^drug/, added);
    await choose(user, 'Row line 1, condition 3: operator', 'is one of', added);
    const values = within(added).getByRole('combobox', { name: 'Row line 1, condition 3: values' });
    await user.click(values);
    await user.click(await screen.findByRole('option', { name: 'Zyrtec' }));
    await user.click(await screen.findByRole('option', { name: 'Euthyrox' }));
    await user.keyboard('{Escape}');

    // 4: revenue has a value; the number column offers comparisons, a text column does not.
    await user.click(within(added).getByRole('button', { name: 'Add condition' }));
    await user.click(within(added).getByRole('combobox', { name: 'Row line 1, condition 4: operator' }));
    expect(screen.queryByRole('option', { name: 'is greater than' })).not.toBeInTheDocument();
    await user.keyboard('{Escape}');
    await choose(user, 'Row line 1, condition 4: column', /^revenue/, added);
    await user.click(within(added).getByRole('combobox', { name: 'Row line 1, condition 4: operator' }));
    expect(await screen.findByRole('option', { name: 'is greater than' })).toBeInTheDocument();
    await user.click(screen.getByRole('option', { name: 'has a value' }));

    await user.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Saved.');
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
      rows: [
        {
          kind: 'show',
          subject: { type: 'everyone', id: '' },
          filter: {
            conditions: [
              { column: 'company', op: 'is', value: 'PHARMA_B' },
              { column: 'company', op: 'is', source: 'attribute:region' },
              { column: 'drug', op: 'one_of', values: ['Zyrtec', 'Euthyrox'] },
              { column: 'revenue', op: 'is_not_empty' },
            ],
          },
        },
      ],
      masks: {},
    });
  });

  it("offers the user's email as a value source and asks the server only for the column's values", async () => {
    const { calls } = mockFetch(routes());
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    const first = await screen.findByRole('listitem', { name: 'Row line 1' });
    await choose(user, 'Row line 1, condition 1: compare with', "the user's email", first);
    expect(within(first).getByRole('combobox', { name: 'Row line 1, condition 1: compare with' })).toHaveTextContent(
      "the user's email",
    );
    await choose(user, 'Row line 1, condition 1: compare with', 'a value', first);
    await user.click(within(first).getByRole('combobox', { name: 'Row line 1, condition 1: value' }));
    expect(await screen.findByRole('option', { name: 'PHARMA_A' })).toBeInTheDocument();
    expect(calls.some((c) => c.url === `${URL}/values/company`)).toBe(true);
  });

  it('reorders and deletes row lines, adds a hide line and saves the new order', async () => {
    const { calls } = mockFetch(routes({ [`PUT ${URL}`]: { body: { ...DRUG_SALES, message: 'Saved order.' } } }));
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await screen.findByRole('listitem', { name: 'Row line 1' });
    expect(screen.getByRole('button', { name: 'Move row line 1 up' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Move row line 1 down' }));
    expect(within(line('Row line 1')).getByRole('combobox', { name: 'Row line 1: who' })).toHaveTextContent('A local group');
    await user.click(screen.getByRole('button', { name: 'Add hide line' }));
    await user.click(screen.getByRole('button', { name: 'Add hide line' }));
    await user.click(screen.getByRole('button', { name: 'Delete row line 5' }));
    expect(screen.queryByRole('listitem', { name: 'Row line 5' })).not.toBeInTheDocument();
    const hide = line('Row line 4');
    await choose(user, 'Row line 4, condition 1: column', /^company/, hide);
    await choose(user, 'Row line 4, condition 1: operator', 'is empty', hide);

    await user.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Saved order.');
    const body = calls.find((c) => c.method === 'PUT')?.body as { rows: unknown[] };
    expect(body.rows).toEqual([
      { rule_id: SHOW_SALES_ALL.rule_id, kind: 'show', subject: { type: 'local_group', id: 'sales-managers' }, filter: { all: true } },
      {
        rule_id: SHOW_EVERYONE_PUBLIC.rule_id,
        kind: 'show',
        subject: { type: 'everyone', id: '' },
        filter: SHOW_EVERYONE_PUBLIC.filter,
      },
      { rule_id: HIDE_TEST_ROWS.rule_id, kind: 'hide', subject: { type: 'everyone', id: '' }, filter: HIDE_TEST_ROWS.filter },
      { kind: 'hide', subject: { type: 'everyone', id: '' }, filter: { conditions: [{ column: 'company', op: 'is_empty' }] } },
    ]);
  });

  it('adds mask exceptions with an "everyone else" line and reorders them', async () => {
    const { calls } = mockFetch(routes({ [`PUT ${URL}`]: { body: { ...DRUG_SALES, message: 'Masks saved.' } } }));
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    const drug = await screen.findByRole('region', { name: 'Masking of drug' });
    await user.click(within(drug).getByRole('button', { name: 'Add exception on drug' }));
    expect(within(drug).getByText('First matching line wins. Anyone no line names sees the real value.')).toBeInTheDocument();
    await choose(user, 'Mask on drug, line 1: who', 'A local group', drug);
    await choose(user, 'Mask on drug, line 1: which local group', 'Sales managers', drug);
    await choose(user, 'Mask on drug, line 1: treatment', /^Sees the real value/, drug);
    await user.click(within(drug).getByRole('button', { name: 'Add everyone else on drug' }));
    await choose(user, 'Mask on drug, line 2: treatment', /^Replaced by a code/, drug);
    expect(within(drug).queryByRole('button', { name: 'Add everyone else on drug' })).not.toBeInTheDocument();

    const revenue = screen.getByRole('region', { name: 'Masking of revenue' });
    await user.click(within(revenue).getByRole('button', { name: 'Move mask on revenue, line 2 up' }));
    await user.click(within(revenue).getByRole('button', { name: 'Delete mask on revenue, line 2' }));

    await user.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Masks saved.');
    expect((calls.find((c) => c.method === 'PUT')?.body as { masks: unknown }).masks).toEqual({
      drug: [
        { subject: { type: 'local_group', id: 'sales-managers' }, treatment: 'clear' },
        { subject: { type: 'everyone', id: '' }, treatment: 'mask_hash' },
      ],
      revenue: [{ mask_id: MASK_REVENUE[1].mask_id, subject: { type: 'everyone', id: '' }, treatment: 'hidden' }],
    });
  });

  it('shows the server 400 detail and keeps the drafts', async () => {
    mockFetch(
      routes({ [`PUT ${URL}`]: { status: 400, body: { detail: "Row line 2: unknown local group 'sales-managers'." } } }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await screen.findByRole('listitem', { name: 'Row line 1' });
    await user.click(screen.getByRole('button', { name: 'Delete row line 1' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText("Row line 2: unknown local group 'sales-managers'.")).toBeInTheDocument();
    expect(screen.queryByRole('listitem', { name: 'Row line 3' })).not.toBeInTheDocument();
  });

  it('loads the example rules into an empty dataset and lists skipped lines', async () => {
    const { calls } = mockFetch(
      routes({
        [`GET ${URL}`]: { body: DRUG_SALES_EMPTY },
        [`POST ${URL}/load-examples`]: {
          body: {
            ...DRUG_SALES,
            message: 'Loaded 3 example lines.',
            skipped: ['Row line 2: IdP group abc is not registered.'],
          },
        },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    expect(await screen.findByText('Nobody sees any row yet.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Load example rules/ }));
    expect(await screen.findByText('Loaded 3 example lines.')).toBeInTheDocument();
    expect(screen.getByText('Row line 2: IdP group abc is not registered.')).toBeInTheDocument();
    expect(await screen.findByRole('listitem', { name: 'Row line 3' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Load example rules/ })).not.toBeInTheDocument();
    expect(calls.find((c) => c.method === 'POST')?.headers.get('X-Access-Csrf')).toBe('1');
  });

  it('does not offer examples when the dataset has none and shows a 409', async () => {
    mockFetch(
      routes({
        [`GET ${URL}`]: { body: { ...DRUG_SALES_EMPTY, has_examples: true } },
        [`POST ${URL}/load-examples`]: { status: 409, body: { detail: 'drug_sales already has rules.' } },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await user.click(await screen.findByRole('button', { name: /Load example rules/ }));
    expect(await screen.findByText('drug_sales already has rules.')).toBeInTheDocument();
  });

  it('works without data: no value requests, a note, free values', async () => {
    const { calls } = mockFetch(routes({ [`GET ${URL}`]: { body: { ...DRUG_SALES, data_available: false } } }));
    renderApp('/datasets/drug_sales');
    await screen.findByRole('listitem', { name: 'Row line 1' });
    expect(screen.getByText('Data not readable right now')).toBeInTheDocument();
    await waitFor(() => expect(calls.some((c) => c.url.includes('/values/'))).toBe(false));
  });
});
