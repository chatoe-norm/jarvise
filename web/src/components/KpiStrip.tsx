import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatNum } from "@/lib/utils";

export function KpiStrip({
  items,
}: {
  items: Array<{ label: string; value: unknown; hint?: string }>;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {items.map((item) => (
        <Card key={item.label}>
          <CardHeader>
            <CardTitle className="text-[var(--color-muted)] font-medium">
              {item.label}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold tabular-nums">
              {typeof item.value === "string" || typeof item.value === "number"
                ? formatNum(item.value)
                : "—"}
            </div>
            {item.hint ? (
              <div className="mt-1 text-xs text-[var(--color-muted)]">{item.hint}</div>
            ) : null}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
