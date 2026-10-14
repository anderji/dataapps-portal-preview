/**
 * Add / set one user attribute (key + value). Attributes parameterise row-level security in the
 * dashboards; the key pattern mirrors the store's.
 *
 * Built from FormGroup + useId rather than useAppForm on purpose: the Users page renders one of
 * these per user, and the form connectors use the field *name* as the DOM id, so several
 * useAppForm instances with a `key` field would emit duplicate ids and break label association.
 */
import { useId, useState, type FormEvent } from 'react';
import { Alert, Button, FormGroup, Label, toast } from '@keboola/design';
import { errorMessage } from '../api/client';
import { useSetAttribute } from '../api/hooks';
import { config } from '../config';

const KEY_HINT = 'Lowercase letters, digits and "_"; starts with a letter; up to 64.';

export function AttributeForm({ email, submitLabel }: { email: string; submitLabel: string }) {
  const setAttribute = useSetAttribute();
  const keyId = useId();
  const valueId = useId();
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [touched, setTouched] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const keyError = touched && !config.patterns.attributeKey.test(key) ? KEY_HINT : null;
  const valueError = touched && !value ? 'Enter a value.' : null;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setTouched(true);
    if (!config.patterns.attributeKey.test(key) || !value) return;
    setServerError(null);
    try {
      const result = await setAttribute.mutateAsync({ email, key, value });
      toast.success(result.message);
      setKey('');
      setValue('');
      setTouched(false);
    } catch (error) {
      setServerError(errorMessage(error));
    }
  };

  return (
    <form onSubmit={(event) => void submit(event)} aria-label={`${submitLabel} for ${email}`} noValidate>
      <div className="tw:flex tw:flex-col tw:gap-3">
        <div className="tw:grid tw:items-start tw:gap-4 tw:md:grid-cols-[1fr_1fr_auto]">
          <FormGroup state={keyError ? 'error' : undefined}>
            <Label htmlFor={keyId}>Key</Label>
            <FormGroup.TextInput
              id={keyId}
              value={key}
              onValueChange={setKey}
              placeholder="company"
              aria-required
              aria-invalid={Boolean(keyError)}
            />
            {keyError && <FormGroup.Error>{keyError}</FormGroup.Error>}
          </FormGroup>
          <FormGroup state={valueError ? 'error' : undefined}>
            <Label htmlFor={valueId}>Value</Label>
            <FormGroup.TextInput
              id={valueId}
              value={value}
              onValueChange={setValue}
              aria-required
              aria-invalid={Boolean(valueError)}
            />
            {valueError && <FormGroup.Error>{valueError}</FormGroup.Error>}
          </FormGroup>
          <div className="tw:md:pt-6">
            <Button
              type="submit"
              variant="secondary"
              isLoading={setAttribute.isPending}
              disabled={setAttribute.isPending}
            >
              {submitLabel}
            </Button>
          </div>
        </div>
        {serverError && (
          <Alert variant="error" title="Could not save the attribute">
            {serverError}
          </Alert>
        )}
      </div>
    </form>
  );
}
