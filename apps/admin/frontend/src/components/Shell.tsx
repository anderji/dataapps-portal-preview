/**
 * App shell: sidebar navigation, a header with who is signed in and the ways out (launcher,
 * switch user), and the routed page in `AppShell.Main`.
 */
import type { ReactNode } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router';
import { AppShell, Button, Link, Sidebar, SidebarProvider } from '@keboola/design';
import {
  AppWindow,
  KeyRound,
  LayoutDashboard,
  PanelLeft,
  ShieldCheck,
  Radar,
  ScrollText,
  UserRound,
  Users,
  UsersRound,
} from '@keboola/design/icons';
import { useCurrentSession } from './SessionGate';
import { switchUserHref } from './switchUser';
import { ThemeToggle } from './ThemeToggle';

type NavItem = { to: string; label: string; icon: ReactNode; end?: boolean };

const NAV: NavItem[] = [
  { to: '/', label: 'Overview', icon: <LayoutDashboard />, end: true },
  { to: '/apps', label: 'Apps', icon: <AppWindow /> },
  { to: '/discovery', label: 'Discovery', icon: <Radar /> },
  { to: '/users', label: 'Users', icon: <UserRound /> },
  { to: '/groups', label: 'IdP groups', icon: <UsersRound /> },
  { to: '/local-groups', label: 'Local groups', icon: <Users /> },
  { to: '/datasets', label: 'Datasets', icon: <ShieldCheck /> },
  { to: '/grants', label: 'Grants', icon: <KeyRound /> },
  { to: '/audit', label: 'Audit', icon: <ScrollText /> },
];

function Navigation() {
  const { app_name } = useCurrentSession();
  return (
    <Sidebar>
      <Sidebar.Header>
        <span className="tw:px-2 tw:font-semibold tw:text-sidebar-foreground">{app_name}</span>
      </Sidebar.Header>
      <Sidebar.Content>
        <nav aria-label="Main">
          <Sidebar.Menu>
            {NAV.map((item) => (
              <Sidebar.MenuItem key={item.to}>
                <Sidebar.MenuButton asChild icon={item.icon} tooltip={item.label}>
                  <NavLink to={item.to} end={item.end}>
                    {item.label}
                  </NavLink>
                </Sidebar.MenuButton>
              </Sidebar.MenuItem>
            ))}
          </Sidebar.Menu>
        </nav>
      </Sidebar.Content>
    </Sidebar>
  );
}

function Header() {
  const { user, app_name, launcher_url, switch_user_path } = useCurrentSession();
  const { pathname } = useLocation();
  const groupCount = user.groups.length;
  return (
    <div className="tw:flex tw:flex-1 tw:flex-wrap tw:items-center tw:justify-between tw:gap-3 tw:py-2">
      <div className="tw:flex tw:items-center tw:gap-2">
        <Sidebar.Trigger>
          <Button variant="invisible" size="icon-sm" aria-label="Toggle navigation">
            <PanelLeft className="tw:size-4" />
          </Button>
        </Sidebar.Trigger>
        <span className="tw:font-semibold tw:text-foreground">{app_name}</span>
      </div>
      <div className="tw:flex tw:flex-wrap tw:items-center tw:gap-3 tw:text-sm tw:text-muted-foreground">
        <span>
          Signed in as <b className="tw:text-foreground">{user.email}</b>
        </span>
        <span>
          {groupCount} {groupCount === 1 ? 'group' : 'groups'}
        </span>
        <Button asChild variant="outline" size="small">
          <Link href={launcher_url} target="_self">
            Launcher
          </Link>
        </Button>
        <Button asChild variant="outline" size="small">
          <Link href={switchUserHref(switch_user_path, pathname)} target="_self">
            Switch user
          </Link>
        </Button>
        <ThemeToggle />
      </div>
    </div>
  );
}

export function Shell() {
  // The provider's default variant is "none", which renders no rail at all; "inset" is the
  // plain bordered rail. Below the mobile breakpoint the rail becomes a sheet opened from the
  // header's trigger.
  return (
    <SidebarProvider leftVariant="inset" leftMobileSheet>
      <AppShell>
        <AppShell.Sidebar>
          <Navigation />
        </AppShell.Sidebar>
        <AppShell.Header>
          <Header />
        </AppShell.Header>
        <AppShell.Main>
          <Outlet />
        </AppShell.Main>
      </AppShell>
    </SidebarProvider>
  );
}
