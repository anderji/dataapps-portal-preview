/**
 * "Who" of a row line or a mask line: everyone, a user, a local group or an IdP group. The type is a
 * short Select; the user / group itself is a searchable Combobox over the existing lists.
 */
import { Combobox, Select } from '@keboola/design';
import { useGroups, useLocalGroups, useUsers } from '../../api/hooks';
import type { LineSubjectType } from '../../api/types';
import { SUBJECT_TYPE_LABEL, type DraftSubject } from './model';

const PICK_LABEL: Record<Exclude<LineSubjectType, 'everyone'>, string> = {
  user: 'user',
  local_group: 'local group',
  group: 'IdP group',
};

/** id -> label of every candidate of one subject type. */
function useCandidates(type: LineSubjectType): { candidates: Map<string, string>; isLoading: boolean } {
  const users = useUsers();
  const localGroups = useLocalGroups();
  const groups = useGroups();
  const candidates = new Map<string, string>();
  if (type === 'user') {
    for (const u of users.data?.users ?? []) {
      candidates.set(u.email, u.display_name ? `${u.display_name} (${u.email})` : u.email);
    }
    return { candidates, isLoading: users.isPending };
  }
  if (type === 'local_group') {
    for (const g of localGroups.data ?? []) candidates.set(g.local_group_id, g.name);
    return { candidates, isLoading: localGroups.isPending };
  }
  if (type === 'group') {
    for (const g of groups.data?.groups ?? []) candidates.set(g.group_id, g.name);
    return { candidates, isLoading: groups.isPending };
  }
  return { candidates, isLoading: false };
}

type Props = {
  subject: DraftSubject;
  onChange: (subject: DraftSubject) => void;
  /** Accessible name prefix, e.g. "Row line 2". */
  label: string;
  /** In a mask list "everyone" reads as "everyone else": earlier lines already took their people. */
  everyoneLabel?: string;
};

export function SubjectPicker({ subject, onChange, label, everyoneLabel = SUBJECT_TYPE_LABEL.everyone }: Props) {
  const { candidates, isLoading } = useCandidates(subject.type);
  // A stored subject the lists no longer know (or not loaded yet) stays selectable under its label.
  if (subject.id && !candidates.has(subject.id)) candidates.set(subject.id, subject.label || subject.id);
  const typeItems = (Object.keys(SUBJECT_TYPE_LABEL) as LineSubjectType[]).map((type) => ({
    value: type,
    label: type === 'everyone' ? everyoneLabel : SUBJECT_TYPE_LABEL[type],
  }));

  // Wraps instead of squeezing: in a narrow column the group or user picker moves to its own line.
  return (
    <div className="tw:flex tw:flex-wrap tw:items-start tw:gap-2">
      <Select
        aria-label={`${label}: who`}
        className="tw:w-44"
        value={subject.type}
        items={typeItems}
        onValueChange={(type) => onChange({ type: type as LineSubjectType, id: '', label: '' })}
      />
      {subject.type !== 'everyone' && (
        <Combobox
          aria-label={`${label}: which ${PICK_LABEL[subject.type]}`}
          className="tw:min-w-48 tw:flex-1"
          value={subject.id || null}
          items={[...candidates.keys()]}
          itemToStringLabel={(id) => candidates.get(id) ?? id}
          onValueChange={(id) => onChange({ ...subject, id: id ?? '', label: id ? (candidates.get(id) ?? id) : '' })}
          placeholder={`Pick a ${PICK_LABEL[subject.type]}`}
          empty={`No matching ${PICK_LABEL[subject.type]}`}
          isLoading={isLoading}
        />
      )}
    </div>
  );
}
