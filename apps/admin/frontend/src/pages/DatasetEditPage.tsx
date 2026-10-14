/**
 * Security editor of one dataset: who sees which rows (show lines, then always-hide lines), column
 * masking, one Save for all of it (PUT replaces the full set; the drafts are reseeded from the
 * answer) and the "Test as" panel, which re-runs after each save.
 */
import { useRef, useState } from 'react';
import { Link as RouterLink, useParams } from 'react-router';
import { Alert, Breadcrumb, Button, Card, PageHeader, PageLayout, toast } from '@keboola/design';
import { Sparkles } from '@keboola/design/icons';
import { errorMessage } from '../api/client';
import { useDataset, useLoadDatasetExamples, useSaveDatasetSecurity } from '../api/hooks';
import type { DatasetDetail } from '../api/types';
import { Muted } from '../components/Code';
import { MaskEditor } from '../components/datasets/MaskEditor';
import {
  describeFilter,
  describeSubject,
  refLabel,
  toDrafts,
  toInput,
  type DraftSecurity,
} from '../components/datasets/model';
import { RowLinesEditor } from '../components/datasets/RowLinesEditor';
import { TestPanel } from '../components/datasets/TestPanel';
import type { LineInfo } from '../components/datasets/TestResultView';
import { LoadError, PageSkeleton } from '../components/PageStates';

/** Source of local draft keys; one counter per editor so keys never collide after a reseed. */
function useKeys(): () => string {
  const counter = useRef(0);
  return () => `draft-${counter.current++}`;
}

const isEmpty = (detail: Pick<DatasetDetail, 'rows' | 'masks'>) =>
  detail.rows.length === 0 && Object.values(detail.masks).every((lines) => lines.length === 0);

function DatasetEditor({ detail }: { detail: DatasetDetail }) {
  const name = detail.dataset.name;
  const newKey = useKeys();
  const save = useSaveDatasetSecurity(name);
  const loadExamples = useLoadDatasetExamples(name);
  // `saved` is the last state the server confirmed: it drives "Load example rules" and names the
  // lines in the test explanation (the test runs against saved rules, not the drafts).
  const [saved, setSaved] = useState<DatasetDetail>(detail);
  const [draft, setDraft] = useState<DraftSecurity>(() => toDrafts(detail, newKey));
  const [serverError, setServerError] = useState<string | null>(null);
  const [skipped, setSkipped] = useState<string[]>([]);
  const [savedVersion, setSavedVersion] = useState(0);
  const { columns } = detail.dataset;

  const reseed = (next: DatasetDetail) => {
    setSaved(next);
    setDraft(toDrafts(next, newKey));
    setSavedVersion((v) => v + 1);
  };

  const submit = async () => {
    setServerError(null);
    try {
      const result = await save.mutateAsync(toInput(draft));
      reseed(result);
      setSkipped([]);
      toast.success(result.message);
    } catch (error) {
      setServerError(errorMessage(error));
    }
  };

  const load = async () => {
    setServerError(null);
    try {
      const result = await loadExamples.mutateAsync();
      reseed(result);
      setSkipped(result.skipped.map(refLabel));
      toast.success(result.message);
    } catch (error) {
      setServerError(errorMessage(error));
    }
  };

  const lineInfo = (ruleId: string): LineInfo | undefined => {
    const ordered = [...saved.rows.filter((r) => r.kind === 'show'), ...saved.rows.filter((r) => r.kind === 'hide')];
    const index = ordered.findIndex((r) => r.rule_id === ruleId);
    if (index < 0) return undefined;
    const line = ordered[index];
    return {
      title: `Line ${index + 1}`,
      description: `${describeSubject(line.subject)}: ${describeFilter(line.filter, columns)}`,
    };
  };

  const shared = { dataset: name, columns, attributes: detail.attributes, dataAvailable: detail.data_available, newKey };

  return (
    <div className="tw:flex tw:flex-col tw:gap-4">
      {!detail.data_available && (
        <Alert variant="info" title="Data not readable right now">
          The admin app cannot read this dataset&apos;s data, so values are not suggested and tests show no row counts or
          preview. You can still edit and save the rules.
        </Alert>
      )}
      {isEmpty(saved) && saved.has_examples && (
        <Alert
          variant="info"
          title="Start from the example rules"
          actions={
            <Button
              variant="secondary"
              size="small"
              isLoading={loadExamples.isPending}
              disabled={loadExamples.isPending}
              onClick={() => void load()}
            >
              <Sparkles className="tw:size-4" /> Load example rules
            </Button>
          }
        >
          This dataset has no rules yet. The policy file ships example rules you can load and then adjust.
        </Alert>
      )}
      {skipped.length > 0 && (
        <Alert variant="warning" title="Some example lines were skipped">
          <ul className="tw:list-disc tw:pl-5">
            {skipped.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Alert>
      )}
      <Card bordered>
        <Card.Header layout="stack">
          <Card.Title as="h2">Who sees which rows</Card.Title>
          <Muted>
            Nobody sees a row unless a show line gives it to them. Hide lines then take rows away again, for example
            test data.
          </Muted>
        </Card.Header>
        <Card.Content>
          <RowLinesEditor
            {...shared}
            show={draft.show}
            hide={draft.hide}
            onShowChange={(update) => setDraft((d) => ({ ...d, show: update(d.show) }))}
            onHideChange={(update) => setDraft((d) => ({ ...d, hide: update(d.hide) }))}
          />
        </Card.Content>
      </Card>
      <Card bordered padding="none">
        <Card.Header divider layout="stack">
          <Card.Title as="h2">Column masking</Card.Title>
          <Muted>
            Masking changes what people see in a column of the rows they already have; it never gives them more rows.
            It applies to the whole column for that person.
          </Muted>
        </Card.Header>
        <Card.Content>
          <MaskEditor
            columns={columns}
            masks={draft.masks}
            newKey={newKey}
            onChange={(column, update) =>
              setDraft((d) => ({ ...d, masks: { ...d.masks, [column]: update(d.masks[column] ?? []) } }))
            }
          />
        </Card.Content>
      </Card>
      {serverError && (
        <Alert variant="error" title="Not saved">
          {serverError}
        </Alert>
      )}
      <div className="tw:flex tw:gap-2">
        <Button isLoading={save.isPending} disabled={save.isPending} onClick={() => void submit()}>
          Save
        </Button>
        <Button variant="secondary" onClick={() => setDraft(toDrafts(saved, newKey))}>
          Discard changes
        </Button>
      </div>
      <TestPanel dataset={name} savedVersion={savedVersion} lineInfo={lineInfo} />
    </div>
  );
}

export function DatasetEditPage() {
  const { name = '' } = useParams();
  const dataset = useDataset(name);
  return (
    <PageLayout>
      <PageLayout.Header>
        <PageHeader.Detail
          breadcrumbs={
            <Breadcrumb>
              <Breadcrumb.List>
                <Breadcrumb.Item>
                  <Breadcrumb.Link asChild>
                    <RouterLink to="/datasets">All datasets</RouterLink>
                  </Breadcrumb.Link>
                </Breadcrumb.Item>
              </Breadcrumb.List>
            </Breadcrumb>
          }
          title={name}
          description={dataset.data?.dataset.description || undefined}
        />
      </PageLayout.Header>
      <PageLayout.Content>
        {dataset.isPending ? (
          <PageSkeleton />
        ) : dataset.isError ? (
          <LoadError error={dataset.error} onRetry={() => void dataset.refetch()} />
        ) : (
          <DatasetEditor key={dataset.data.dataset.name} detail={dataset.data} />
        )}
      </PageLayout.Content>
    </PageLayout>
  );
}
