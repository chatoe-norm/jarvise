export const FULLY_AUTONOMOUS_LINE = "สถานะ: ทำงานอัตโนมัติเต็มรูปแบบ (Fully Autonomous)";

/** The green autonomy line is shown only while auto-decide is on. */
export function fullyAutonomousLine(enabled: boolean): string | null {
  return enabled ? FULLY_AUTONOMOUS_LINE : null;
}
