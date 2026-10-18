/** Below the `sm` breakpoint the explanation expands in place. From 640px it is a popover. */
export const INFO_TIP_POPOVER_MIN_PX = 640;

export function toggleInfoTip(open: boolean): boolean {
  return !open;
}

export function infoTipAriaExpanded(open: boolean): "true" | "false" {
  return open ? "true" : "false";
}

export function infoTipPanel(viewportWidth: number): "inline" | "popover" {
  return viewportWidth >= INFO_TIP_POPOVER_MIN_PX ? "popover" : "inline";
}
