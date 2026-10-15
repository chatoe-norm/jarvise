import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { formatNum } from "@/lib/utils";

export function KpiStrip({
  items,
}: {
  items: Array<{ label: string; value: unknown; hint?: string }>;
}) {
  return (
    <div className="grid grid-cols-2 gap-2.5 sm:gap-3 lg:grid-cols-4">
      {items.map((item) => (
        <Card key={item.label} className="p-1 sm:p-2">
          <CardHeader className="p-3 pb-1">
            <CardTitle className="text-xs sm:text-sm text-[var(--color-muted)] font-medium">
              {item.label}
            </CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-0">
            <div className="text-lg sm:text-2xl font-bold tracking-tight tabular-nums text-[var(--color-foreground)]">
              {typeof item.value === "string" || typeof item.value === "number"
                ? formatNum(item.value)
                : "—"}
            </div>
            {item.hint ? (
              <div className="mt-0.5 text-[10px] sm:text-xs text-[var(--color-muted)] truncate">
                {item.hint}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
