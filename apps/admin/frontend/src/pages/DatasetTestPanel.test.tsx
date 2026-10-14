import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import {
  APP_SALES,
  DATASETS,
  DRUG_SALES,
  DRUG_SALES_EMPTY,
  GROUP_SALES,
  LOCAL_GROUP_SALES,
  TEST_RESULT_ALICE,
} from '../test/fixtures';
import { mockFetch, renderApp, SESSION, type Routes } from '../test/utils';

type User = ReturnType<typeof userEvent.setup>;

const ALICE = { email: 'alice@example.com', display_name: 'Alice', active: true, attributes: {} };
const URL = '/api/datasets/drug_sales';

const routes = (extra: Routes = {}): Routes => ({
  'GET /api/session': { body: SESSION },
  [`GET ${URL}`]: { body: DRUG_SALES },
  'GET /api/users': { body: { users: [ALICE] } },
  'GET /api/local-groups': { body: [LOCAL_GROUP_SALES] },
  'GET /api/groups': { body: { groups: [GROUP_SALES], providers: {} } },
  [`GET ${URL}/values/company`]: { body: { values: ['PHARMA_A'], truncated: false } },
  [`GET ${URL}/values/is_test`]: { body: { values: [true, false], truncated: false } },
  ...extra,
});

async function runTestAs(user: User, groups: string[] = []) {
  await screen.findByRole('heading', { name: 'Test as' });
  await user.click(screen.getByLabelText('User'));
  await user.click(await screen.findByRole('option', { name: 'Alice (alice@example.com)' }));
  for (const group of groups) {
    await user.type(screen.getByLabelText('IdP group ids (optional)'), `${group}{Enter}`);
  }
  await user.click(screen.getByRole('button', { name: /Run test/ }));
}

describe('Test as panel', () => {
  it('runs a test with IdP groups and explains rows, lines, warnings, columns and the preview', async () => {
    const { calls } = mockFetch(routes({ [`POST ${URL}/test`]: { body: TEST_RESULT_ALICE } }));
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await runTestAs(user, ['sales-team-guid']);

    const result = await screen.findByRole('region', { name: 'Test result' });
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
      email: 'alice@example.com',
      groups: ['sales-team-guid'],
    });
    expect(result).toHaveTextContent('Sees 40 of 120 rows.');
    expect(within(result).getByText('sales-managers')).toBeInTheDocument();

    const lines = within(result).getByRole('table', { name: 'Why each line applies' });
    const first = within(lines).getByText('Line 1').closest('tr') as HTMLElement;
    expect(first).toHaveTextContent("Everyone: company is the user's company");
    expect(within(first).getByText('yes')).toBeInTheDocument();
    expect(first).toHaveTextContent("Check this lineNo row has company = 'Pharma A s.r.o.'");
    const hide = within(lines).getByText('Line 3').closest('tr') as HTMLElement;
    expect(hide).toHaveTextContent('removes rows');
    expect(hide).toHaveTextContent('80');

    const columns = within(result).getByRole('table', { name: 'What happens to each column' });
    const revenue = within(columns).getByText('revenue').closest('tr') as HTMLElement;
    expect(revenue).toHaveTextContent('Column hidden');
    expect(revenue).toHaveTextContent('Line 2 names everyone.');

    const preview = within(result).getByRole('table', { name: 'Preview' });
    expect(within(preview).queryByText('revenue')).not.toBeInTheDocument();
    expect(within(preview).getByText('Zy***')).toHaveAttribute('data-masked', 'true');
    expect(within(preview).getByText('PHARMA_A')).not.toHaveAttribute('data-masked');
    expect(within(preview).getByText('empty')).toBeInTheDocument();
    expect(within(result).getByText('Only the first rows are shown.')).toBeInTheDocument();

    // The SQL filter is folded away until asked for.
    expect(within(result).queryByText('(TRUE) AND NOT (is_test = TRUE)')).not.toBeInTheDocument();
    await user.click(within(result).getByRole('button', { name: /Technical details/ }));
    expect(await within(result).findByText('(TRUE) AND NOT (is_test = TRUE)')).toBeInTheDocument();
  });

  it('handles data_available false: no counts, no preview, says why', async () => {
    mockFetch(
      routes({
        [`GET ${URL}`]: { body: { ...DRUG_SALES, data_available: false } },
        [`POST ${URL}/test`]: {
          body: {
            ...TEST_RESULT_ALICE,
            rows: {
              visible: null,
              total: null,
              lines: TEST_RESULT_ALICE.rows.lines.map((l) => ({ ...l, matched_rows: null, warning: null })),
            },
            preview: null,
            data_available: false,
          },
        },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await runTestAs(user);
    const result = await screen.findByRole('region', { name: 'Test result' });
    expect(within(result).getByText('No data to count')).toBeInTheDocument();
    expect(result).toHaveTextContent('Can open this dataset.');
    expect(result).not.toHaveTextContent('rows.');
    expect(within(result).queryByRole('table', { name: 'Preview' })).not.toBeInTheDocument();
    expect(within(result).queryByRole('columnheader', { name: 'Matching rows' })).not.toBeInTheDocument();
  });

  it('says when the user sees nothing and shows a test error', async () => {
    let attempt = 0;
    mockFetch(
      routes({
        [`POST ${URL}/test`]: () => {
          attempt += 1;
          return attempt === 1
            ? { status: 400, body: { detail: "Unknown user 'alice@example.com'." } }
            : {
                body: {
                  ...TEST_RESULT_ALICE,
                  accessible: false,
                  rows: { visible: 0, total: 120, lines: [] },
                  columns: [],
                  preview: null,
                  row_filter: null,
                },
              };
        },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await runTestAs(user);
    expect(await screen.findByText("Unknown user 'alice@example.com'.")).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Run test/ }));
    expect(await screen.findByText('Sees nothing')).toBeInTheDocument();
    expect(screen.getByText('This dataset has no row lines.')).toBeInTheDocument();
  });

  it('re-runs the last test after a save', async () => {
    let tests = 0;
    const { calls } = mockFetch(
      routes({
        [`PUT ${URL}`]: { body: { ...DRUG_SALES, message: 'Saved.' } },
        [`POST ${URL}/test`]: () => {
          tests += 1;
          return {
            body: { ...TEST_RESULT_ALICE, rows: { ...TEST_RESULT_ALICE.rows, visible: tests === 1 ? 40 : 55 } },
          };
        },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await runTestAs(user);
    expect(await screen.findByText('40')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Saved.');
    await waitFor(() => expect(screen.getByRole('region', { name: 'Test result' })).toHaveTextContent('Sees 55 of 120'));
    expect(calls.filter((c) => c.url === `${URL}/test`)).toHaveLength(2);
  });

  it('commits a typed value when the field is left, without picking the create row', async () => {
    const { calls } = mockFetch(
      routes({
        [`GET ${URL}`]: { body: DRUG_SALES_EMPTY },
        [`PUT ${URL}`]: { body: { ...DRUG_SALES_EMPTY, message: 'Saved.' } },
      }),
    );
    const user = userEvent.setup();
    renderApp('/datasets/drug_sales');
    await user.click(await screen.findByRole('button', { name: 'Add hide line' }));
    const value = screen.getByRole('combobox', { name: 'Row line 1, condition 1: value' });
    await user.type(value, 'TEST_CO');
    await user.tab();
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.filter((c) => c.method === 'PUT')).toHaveLength(1);
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
      rows: [
        {
          kind: 'hide',
          subject: { type: 'everyone', id: '' },
          filter: { conditions: [{ column: 'company', op: 'is', value: 'TEST_CO' }] },
        },
      ],
      masks: {},
    });
  });
});

describe('User page: what this user sees', () => {
  const userDetail = {
    user: { email: 'alice@example.com', display_name: 'Alice', active: true },
    attributes: {},
    apps: [APP_SALES],
    granted: [],
  };

  it('summarises the test result per dataset with links, and drops the old cards', async () => {
    const { calls } = mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users/alice%40example.com': { body: userDetail },
      'GET /api/datasets': { body: DATASETS },
      'POST /api/datasets/drug_sales/test': { body: TEST_RESULT_ALICE },
      'POST /api/datasets/patients/test': {
        body: {
          ...TEST_RESULT_ALICE,
          accessible: false,
          rows: { visible: 0, total: 3, lines: [] },
          columns: [],
          preview: null,
        },
      },
    });
    renderApp('/users/alice%40example.com');
    const table = await screen.findByRole('table', { name: 'What this user sees' });
    const drug = (await within(table).findByText('40 of 120')).closest('tr') as HTMLElement;
    expect(within(drug).getByText('can open')).toBeInTheDocument();
    expect(drug).toHaveTextContent('drug');
    expect(drug).toHaveTextContent('revenue');
    expect(drug).toHaveTextContent("No row has company = 'Pharma A s.r.o.'");
    expect(within(drug).getByRole('link', { name: 'Open drug_sales' })).toHaveAttribute('href', '/datasets/drug_sales');
    const patients = (await within(table).findByText('sees nothing')).closest('tr') as HTMLElement;
    expect(within(patients).getByRole('link', { name: 'Open patients' })).toBeInTheDocument();

    expect(calls.find((c) => c.url === '/api/datasets/drug_sales/test')?.body).toEqual({
      email: 'alice@example.com',
      groups: [],
    });
    expect(screen.queryByText('Effective access')).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Data policies' })).not.toBeInTheDocument();
  });

  it('shows a per-dataset error and data not available', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/users/alice%40example.com': { body: userDetail },
      'GET /api/datasets': { body: DATASETS },
      'POST /api/datasets/drug_sales/test': {
        body: { ...TEST_RESULT_ALICE, rows: { ...TEST_RESULT_ALICE.rows, visible: null, total: null }, data_available: false },
      },
      'POST /api/datasets/patients/test': { status: 500, body: { detail: 'Test failed.' } },
    });
    renderApp('/users/alice%40example.com');
    expect(await screen.findByText('no data to count')).toBeInTheDocument();
    expect(await screen.findByText('Test failed.')).toBeInTheDocument();
  });
});

describe('Group pages: dataset rules naming the group', () => {
  it('lists the row and mask lines naming an IdP group', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/groups/g1': { body: { group: GROUP_SALES, providers: {}, apps: [APP_SALES], granted: [] } },
      'GET /api/datasets': { body: DATASETS },
      [`GET ${URL}`]: { body: DRUG_SALES },
      'GET /api/datasets/patients': {
        body: { ...DRUG_SALES_EMPTY, dataset: { name: 'patients', description: '', columns: [] } },
      },
    });
    renderApp('/groups/g1');
    const table = await screen.findByRole('table', { name: 'Dataset rules naming this group' });
    expect(within(table).getByText('Masking of revenue, line 1')).toBeInTheDocument();
    expect(within(table).getByText('Sees the real value')).toBeInTheDocument();
    expect(within(table).getAllByRole('link', { name: 'drug_sales' })[0]).toHaveAttribute('href', '/datasets/drug_sales');
  });

  it('lists the row lines naming a local group, or says none', async () => {
    mockFetch({
      'GET /api/session': { body: SESSION },
      'GET /api/local-groups/sales-managers': { body: LOCAL_GROUP_SALES },
      'GET /api/users': { body: { users: [] } },
      'GET /api/grants': { body: { apps: [APP_SALES], grants: [], users: [], groups: [], local_groups: [] } },
      'GET /api/datasets': { body: { datasets: [DATASETS.datasets[0]], data_available: true } },
      [`GET ${URL}`]: { body: DRUG_SALES },
    });
    renderApp('/local-groups/sales-managers');
    const table = await screen.findByRole('table', { name: 'Dataset rules naming this group' });
    const row = within(table).getByText('Rows, line 2 (adds rows)').closest('tr') as HTMLElement;
    expect(row).toHaveTextContent('All rows');
  });
});
