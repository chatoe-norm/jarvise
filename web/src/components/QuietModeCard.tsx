import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { api } from "@/lib/api";

export function QuietModeCard({
  enabled,
  onRefresh,
}: {
  enabled: boolean;
  onRefresh?: () => void;
}) {
  const { confirm, dialog } = useConfirm();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    const next = !enabled;
    const ok = await confirm({
      title: "โหมดเงียบ: แจ้งเฉพาะเรื่องสำคัญ",
      body: next
        ? "ปิดการแจ้งคิวปกติ คงไว้เฉพาะ FLAT ที่ปิดไม้, kill switch, risk monitor halt, และ ingest พัง"
        : "กลับไปแจ้งคิวปกติทุกใบ",
      confirmLabel: next ? "เปิดโหมดเงียบ" : "ปิดโหมดเงียบ",
    });
    if (ok !== true) return;
    setBusy(true);
    setError(null);
    try {
      await api.setQuietMode(next);
      onRefresh?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "บันทึกไม่สำเร็จ");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      {dialog}
      <CardHeader>
        <CardTitle>โหมดเงียบ: แจ้งเฉพาะเรื่องสำคัญ</CardTitle>
        <CardDescription>ค่าเริ่มต้นปิด เจ้าของเป็นคนเปิด</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-3">
        <Badge variant={enabled ? "outline" : "muted"}>{enabled ? "เปิด" : "ปิด"}</Badge>
        <Button type="button" variant="outline" disabled={busy} onClick={() => void toggle()}>
          {enabled ? "ปิดโหมดเงียบ" : "เปิดโหมดเงียบ"}
        </Button>
        {error ? <p className="text-sm text-destructive">{error}</p> : null}
      </CardContent>
    </Card>
  );
}
