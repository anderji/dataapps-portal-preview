/**
 * Discovery: scan this project's Data Apps with the admin app's own Storage token and import the
 * new ones. Imported apps get no grants. The server does not trust the submitted rows - it scans
 * again and imports only apps that really exist, with the confirmed name and local ID.
 * Each row also shows the app's own Keboola login (its authorization providers and whether "/" needs
 * them), so an app anyone with the URL can open stands out before it is imported.
 */
import { useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  EmptyState,
  Link,
  PageHeader,
  PageLayout,
  Skeleton,
  Table,
  TextInput,
  toast,
} from '@keboola/design';
import { Download, Radar } from '@keboola/design/icons';
import { errorMessage } from '../api/client';
import { useDiscoveryImport, useDiscoveryInfo, useDiscoveryScan } from '../api/hooks';
import type { AuthProvider, DiscoveredApp } from '../api/types';
import { AuthModeBadge, DiscoveryStatusBadge } from '../components/Badges';
import { Code, Muted } from '../components/Code';
import { LoadError } from '../components/PageStates';
import { config } from '../config';

/** Editable choices for one new row, keyed by the Data App ID. */
type Draft = { selected: boolean; name: string; local_id: string };

function draftsFor(results: DiscoveredApp[]): Record<string, Draft> {
  return Object.fromEntries(
    results
      .filter((r) => r.status === 'new')
      .map((r) => [r.ds_app_id, { selected: true, name: r.name, local_id: r.local_id }]),
  );
}

/** Readable names of the Keboola login provider types; anything else is shown as configured. */
const PROVIDER_TYPES: Record<string, string> = {
  password: 'Password',
  oidc: 'OIDC',
  github: 'GitHub',
  gitlab: 'GitLab',
};

/** The issuer's host and path ("login.microsoftonline.com/<tenant>"), enough to tell IdPs apart. */
function issuerLabel(issuerUrl: string): string {
  try {
    const url = new URL(issuerUrl);
    return `${url.host}${url.pathname.replace(/\/v2\.0\/?$/, '').replace(/\/$/, '')}`;
  } catch {
    return issuerUrl;
  }
}

function Authorization({ row }: { row: DiscoveredApp }) {
  return (
    <div className="tw:flex tw:flex-col tw:items-start tw:gap-1">
      <AuthModeBadge mode={row.auth_mode} />
      {row.auth_providers.map((p: AuthProvider) => (
        <span key={p.id} className="tw:text-xs">
          {PROVIDER_TYPES[p.type] ?? p.type}
          {p.issuer_url && <Code muted> {issuerLabel(p.issuer_url)}</Code>}
        </span>
      ))}
    </div>
  );
}

function ResultRow({
  row,
  draft,
  onChange,
}: {
  row: DiscoveredApp;
  draft: Draft | undefined;
  onChange: (draft: Draft) => void;
}) {
  const isNew = row.status === 'new' && draft !== undefined;
  const localIdInvalid = isNew && !config.patterns.appId.test(draft.local_id);
  return (
    <Table.Row>
      <Table.Cell>
        {isNew && (
          <Checkbox
            aria-label={`Import ${row.name}`}
            checked={draft.selected}
            onCheckedChange={(checked: boolean) => onChange({ ...draft, selected: checked })}
          />
        )}
      </Table.Cell>
      <Table.Cell>
        <div className="tw:flex tw:flex-col tw:gap-1">
          {isNew ? (
            <>
              <TextInput
                aria-label={`Name of ${row.ds_app_id}`}
                value={draft.name}
                onValueChange={(name) => onChange({ ...draft, name })}
                sizing="small"
              />
              {row.name_source !== 'configuration' && (
                <span className="tw:text-xs tw:text-muted-foreground">
                  name from the {row.name_source} - edit if needed
                </span>
              )}
            </>
          ) : (
            <span>{row.name}</span>
          )}
          <Code muted>Data App {row.ds_app_id}</Code>
        </div>
      </Table.Cell>
      <Table.Cell>
        <div className="tw:flex tw:flex-col tw:items-start tw:gap-1">
          {isNew ? (
            <>
              <TextInput
                aria-label={`App ID for ${row.ds_app_id}`}
                value={draft.local_id}
                onValueChange={(local_id) => onChange({ ...draft, local_id })}
                state={localIdInvalid ? 'error' : 'default'}
                aria-invalid={localIdInvalid}
                sizing="small"
              />
              {localIdInvalid && (
                <span className="tw:text-xs tw:text-destructive" role="alert">
                  Lowercase letters, digits, &quot;-&quot; and &quot;_&quot;; up to 64.
                </span>
              )}
            </>
          ) : (
            <Code>{row.local_id}</Code>
          )}
          <DiscoveryStatusBadge status={row.status} />
        </div>
      </Table.Cell>
      <Table.Cell>
        <Authorization row={row} />
      </Table.Cell>
      <Table.Cell>{row.state}</Table.Cell>
      <Table.Cell>
        {row.url ? <Link href={row.url}>{row.url}</Link> : <span className="tw:text-muted-foreground">none yet</span>}
      </Table.Cell>
    </Table.Row>
  );
}

function Results({ results, onImported }: { results: DiscoveredApp[]; onImported: () => void }) {
  const [drafts, setDrafts] = useState(() => draftsFor(results));
  const [importError, setImportError] = useState<string | null>(null);
  const importApps = useDiscoveryImport();
  const hasNew = results.some((r) => r.status === 'new');
  const selected = Object.entries(drafts).filter(([, d]) => d.selected);
  const invalid = selected.some(([, d]) => !config.patterns.appId.test(d.local_id));

  const submit = async () => {
    setImportError(null);
    try {
      const result = await importApps.mutateAsync(
        selected.map(([ds_app_id, d]) => ({ ds_app_id, local_id: d.local_id.trim(), name: d.name.trim() })),
      );
      if (result.message) toast.success(result.message);
      if (result.error) setImportError(result.error);
      else onImported();
    } catch (error) {
      setImportError(errorMessage(error));
    }
  };

  return (
    <Card bordered padding="none">
      <Card.Header divider>
        <Card.Title as="h2">Data Apps in this project ({results.length})</Card.Title>
        {hasNew && (
          <Button
            size="small"
            onClick={() => void submit()}
            isLoading={importApps.isPending}
            disabled={importApps.isPending || selected.length === 0 || invalid}
          >
            <Download className="tw:size-4" /> Import selected
          </Button>
        )}
      </Card.Header>
      <Card.Content>
        {importError && (
          <div className="tw:p-4">
            <Alert variant="error" title="Import failed">
              {importError}
            </Alert>
          </div>
        )}
        {results.some((r) => r.auth_mode === 'unknown') && (
          <div className="tw:p-4">
            <Alert variant="warning" title="Some logins are unknown">
              The login of a Data App is in its configuration, and the admin app&apos;s Storage token cannot read
              the configurations of every app here. Check those apps in the Keboola UI.
            </Alert>
          </div>
        )}
        {results.length === 0 ? (
          <EmptyState variant="compact">
            <EmptyState.Title>No Data Apps found.</EmptyState.Title>
          </EmptyState>
        ) : (
          <Table aria-label="Discovered Data Apps">
            <Table.Header>
              <Table.Row>
                <Table.Head>Import</Table.Head>
                <Table.Head>Name</Table.Head>
                <Table.Head>Local app ID</Table.Head>
                <Table.Head>Authorization</Table.Head>
                <Table.Head>State</Table.Head>
                <Table.Head>URL</Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {results.map((row) => (
                <ResultRow
                  key={row.ds_app_id}
                  row={row}
                  draft={drafts[row.ds_app_id]}
                  onChange={(draft) => setDrafts((current) => ({ ...current, [row.ds_app_id]: draft }))}
                />
              ))}
            </Table.Body>
          </Table>
        )}
      </Card.Content>
    </Card>
  );
}

export function DiscoveryPage() {
  const info = useDiscoveryInfo();
  const scan = useDiscoveryScan();

  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Index title="Discover Data Apps" />
      </PageLayout.Header>
      <PageLayout.Content>
        <div className="tw:flex tw:flex-col tw:gap-4">
          <Card bordered>
            <Card.Content>
              <div className="tw:flex tw:flex-col tw:gap-4">
                <Muted>
                  Scans the Data Apps of <b>this</b> Keboola project with the Storage token the admin app already
                  has - no Manage token, no other projects. Imported apps are registered with no grants: nobody can
                  open them until you grant access.
                </Muted>
                {info.isPending ? (
                  <Skeleton height="lg" className="tw:w-48" />
                ) : info.isError ? (
                  <LoadError error={info.error} onRetry={() => void info.refetch()} />
                ) : info.data.available ? (
                  <div>
                    <Button onClick={() => scan.mutate()} isLoading={scan.isPending} disabled={scan.isPending}>
                      <Radar className="tw:size-4" /> Scan this project
                    </Button>
                  </div>
                ) : (
                  <Alert variant="warning" title="Discovery is not available here">
                    Discovery is available only when the admin app runs in Keboola (it needs the app&apos;s Storage
                    token). Register apps by hand on the Apps page instead.
                  </Alert>
                )}
                {scan.isError && (
                  <Alert variant="error" title="Scan failed">
                    {errorMessage(scan.error)}
                  </Alert>
                )}
              </div>
            </Card.Content>
          </Card>
          {scan.data && (
            // A new scan remounts the table so its drafts start from the fresh results.
            <Results key={scan.submittedAt} results={scan.data.results} onImported={() => scan.reset()} />
          )}
        </div>
      </PageLayout.Content>
    </PageLayout>
  );
}
