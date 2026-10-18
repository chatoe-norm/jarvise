import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { infoTipAriaExpanded, infoTipPanel, toggleInfoTip } from "./infoTip.ts";

describe("InfoTip", () => {
  it("toggles open and closed", () => {
    assert.equal(toggleInfoTip(false), true);
    assert.equal(toggleInfoTip(true), false);
  });

  it("exposes aria-expanded from the open state", () => {
    assert.equal(infoTipAriaExpanded(false), "false");
    assert.equal(infoTipAriaExpanded(true), "true");
  });

  it("expands inline below sm and uses a popover from sm up", () => {
    assert.equal(infoTipPanel(375), "inline");
    assert.equal(infoTipPanel(639), "inline");
    assert.equal(infoTipPanel(640), "popover");
    assert.equal(infoTipPanel(768), "popover");
    assert.equal(infoTipPanel(1440), "popover");
  });
});
