/**
 * Resolves the signed-in session before any page renders.
 *
 * - 401 from /api/session: the request reached the app without identity headers - "Not signed in".
 * - 403: the app gate refused this principal - the deny message, reason code, and who the app
 *   thinks you are (email + IdP groups), plus the way out (launcher / switch user) when known.
 * - Anything else: a retryable load error.
 */
import { createContext, useContext, type ReactNode } from 'react';
import { AuthLayout, Button, Card, ErrorState, Link, Skeleton, Table } from '@keboola/design';
import { LogIn, ShieldX } from '@keboola/design/icons';
import { ApiError } from '../api/client';
import { useSession } from '../api/hooks';
import { deniedBodySchema, type DeniedBody, type Session } from '../api/types';
import { config } from '../config';
import { Code, Muted } from './Code';
import { LoadError } from './PageStates';
import { switchUserHref } from './switchUser';
import { ThemeToggle } from './ThemeToggle';

const SessionContext = createContext<Session | null>(null);

export function useCurrentSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error('useCurrentSession must be used inside <SessionGate>.');
  return session;
}

function GateFrame({ children }: { children: ReactNode }) {
  return (
    <AuthLayout width="wide">
      <AuthLayout.Header productName={config.productName} actions={<ThemeToggle />} />
      <AuthLayout.Content>{children}</AuthLayout.Content>
    </AuthLayout>
  );
}

function parseDenied(error: ApiError): DeniedBody {
  const parsed = deniedBodySchema.safeParse(error.body);
  return parsed.success ? parsed.data : { detail: error.detail, reason: 'unknown' };
}

export function NotSignedIn({ body }: { body: DeniedBody }) {
  return (
    <GateFrame>
      <Card bordered padding="large">
        <ErrorState>
          <ErrorState.Icon>
            <LogIn />
          </ErrorState.Icon>
          <ErrorState.Title as="h1">Not signed in</ErrorState.Title>
          <ErrorState.Description>
            The request reached the app without identity headers from the proxy ({body.detail}).
          </ErrorState.Description>
          {body.switch_user_path && (
            <ErrorState.Actions>
              <Button asChild>
                <Link href={switchUserHref(body.switch_user_path, window.location.pathname)} target="_self">
                  Sign in
                </Link>
              </Button>
            </ErrorState.Actions>
          )}
        </ErrorState>
      </Card>
    </GateFrame>
  );
}

export function AccessDenied({ body }: { body: DeniedBody }) {
  const groups = [...(body.user?.groups ?? [])].sort();
  return (
    <GateFrame>
      <Card bordered padding="large">
        <div className="tw:flex tw:flex-col tw:gap-4">
          <ErrorState variant="destructive">
            <ErrorState.Icon>
              <ShieldX />
            </ErrorState.Icon>
            <ErrorState.Title as="h1">Sorry, you can&apos;t get in here</ErrorState.Title>
            <ErrorState.Description>{body.detail}</ErrorState.Description>
          </ErrorState>
          <Muted>
            Reason code: <Code>{body.reason}</Code>
          </Muted>
          <h2 className="tw:text-base tw:font-semibold">Who the app thinks you are</h2>
          <Table bordered>
            <Table.Body>
              <Table.Row>
                <Table.Head scope="row">Email</Table.Head>
                <Table.Cell>{body.user?.email ?? <Muted>unknown</Muted>}</Table.Cell>
              </Table.Row>
              <Table.Row>
                <Table.Head scope="row">Groups from the IdP</Table.Head>
                <Table.Cell>
                  {groups.length ? (
                    <div className="tw:flex tw:flex-col tw:gap-1">
                      {groups.map((g) => (
                        <Code key={g}>{g}</Code>
                      ))}
                    </div>
                  ) : (
                    <Muted>none</Muted>
                  )}
                </Table.Cell>
              </Table.Row>
            </Table.Body>
          </Table>
          <div className="tw:flex tw:gap-2">
            {body.launcher_url && (
              <Button asChild>
                <Link href={body.launcher_url} target="_self">
                  Go to the launcher
                </Link>
              </Button>
            )}
            {body.switch_user_path && (
              <Button asChild variant="secondary">
                <Link href={switchUserHref(body.switch_user_path, window.location.pathname)} target="_self">
                  Switch user
                </Link>
              </Button>
            )}
          </div>
        </div>
      </Card>
    </GateFrame>
  );
}

export function SessionGate({ children }: { children: ReactNode }) {
  const session = useSession();

  if (session.isPending) {
    return (
      <GateFrame>
        <Skeleton height="lg" count={3} />
      </GateFrame>
    );
  }
  if (session.isError) {
    const error = session.error;
    if (error instanceof ApiError && error.status === 401) return <NotSignedIn body={parseDenied(error)} />;
    if (error instanceof ApiError && error.status === 403) return <AccessDenied body={parseDenied(error)} />;
    return (
      <GateFrame>
        <LoadError error={error} onRetry={() => void session.refetch()} />
      </GateFrame>
    );
  }
  return <SessionContext value={session.data}>{children}</SessionContext>;
}
