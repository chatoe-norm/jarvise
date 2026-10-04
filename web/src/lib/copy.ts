/** Owner-facing wording. Stored Claude reasons may still say หลักคำสอน. */

export function ownerReasonCopy(reason: string | null | undefined): string {
  if (reason == null || reason === "") return "—";
  return reason.replaceAll("หลักคำสอน", "กฎการเทรด");
}
