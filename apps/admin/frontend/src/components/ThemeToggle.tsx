/** Light / dark / system switch wired to the root ThemeProvider's scheme. */
import { ColorModeSwitch, useTheme } from '@keboola/design';

export function ThemeToggle() {
  const { scheme, setScheme } = useTheme();
  return <ColorModeSwitch value={scheme} onValueChange={setScheme} size="small" />;
}
