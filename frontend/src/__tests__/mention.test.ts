import { describe, it, expect, vi, beforeEach } from "vitest";
import { MentionPopup } from "../ui/MentionPopup";
import type { AgentInfo } from "../types/agent";

function makeAgent(name: string): AgentInfo {
  return { id: name, name, state: "running", protected: false };
}

function makeInput(): HTMLTextAreaElement {
  document.body.innerHTML = "";
  const input = document.createElement("textarea");
  document.body.appendChild(input);
  return input;
}

function agents() {
  return [makeAgent("alpha"), makeAgent("bravo"), makeAgent("charlie")];
}

function triggerInput(input: HTMLTextAreaElement, value: string, cursor?: number) {
  input.value = value;
  input.selectionStart = cursor ?? value.length;
  input.selectionEnd = cursor ?? value.length;
  input.dispatchEvent(new Event("input"));
}

function ul() {
  return document.getElementById("mention-popup") as HTMLUListElement;
}

describe("MentionPopup", () => {
  let input: HTMLTextAreaElement;

  beforeEach(() => {
    input = makeInput();
    vi.clearAllMocks();
  });

  it("appends popup element to DOM on construction", () => {
    new MentionPopup(input, agents);
    expect(document.getElementById("mention-popup")).not.toBeNull();
  });

  it("shows popup when @ is typed with matching agents", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@al");
    expect(ul().style.display).toBe("block");
    expect(ul().querySelectorAll("li").length).toBe(1);
  });

  it("hides popup when text has no @ context", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@al");
    triggerInput(input, "hello");
    expect(ul().style.display).toBe("none");
  });

  it("hides popup when no agents match", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@zzz");
    expect(ul().style.display).toBe("none");
  });

  it("filters agents case-insensitively", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@BR");
    const items = ul().querySelectorAll("li");
    expect(items.length).toBe(1);
    expect(items[0]!.textContent).toBe("@bravo");
  });

  it("shows all agents for bare @", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@");
    expect(ul().querySelectorAll("li").length).toBe(3);
  });

  it("ArrowDown moves focus to next item", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@"); // shows all 3 agents
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    const items = ul().querySelectorAll<HTMLLIElement>("li");
    // focusedIdx starts at 0, ArrowDown → 1
    expect(items[1]?.classList.contains("focused")).toBe(true);
  });

  it("ArrowUp clamps to 0", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@a");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true, cancelable: true }));
    const items = ul().querySelectorAll<HTMLLIElement>("li");
    expect(items[0]?.classList.contains("focused")).toBe(true);
  });

  it("ArrowDown clamps at last item", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@"); // all 3 agents
    for (let i = 0; i < 10; i++) {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    }
    const items = ul().querySelectorAll<HTMLLIElement>("li");
    // only items.length-1 max
    const lastFocused = Array.from(items).findIndex((li) => li.classList.contains("focused"));
    expect(lastFocused).toBe(items.length - 1);
  });

  it("Enter selects focused item and replaces @partial in input", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@al");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    expect(input.value).toContain("@alpha ");
    expect(ul().style.display).toBe("none");
  });

  it("Tab selects focused item", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@br");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
    expect(input.value).toContain("@bravo ");
  });

  it("Escape hides popup", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@al");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    expect(ul().style.display).toBe("none");
  });

  it("clicking outside input and popup hides popup", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@al");
    document.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(ul().style.display).toBe("none");
  });

  it("clicking on input itself does not hide popup", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@al");
    input.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(ul().style.display).toBe("block");
  });

  it("mousedown on list item selects agent", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@br");
    const li = ul().querySelector<HTMLLIElement>("li")!;
    li.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    expect(input.value).toContain("@bravo ");
  });

  it("keydown is no-op when popup is hidden", () => {
    new MentionPopup(input, agents);
    expect(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    }).not.toThrow();
  });

  it("Enter is no-op when no item is focused", () => {
    new MentionPopup(input, agents);
    // popup is hidden, focusedIdx = -1
    expect(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    }).not.toThrow();
  });

  it("select() falls back gracefully if getMentionContext returns null atPos", () => {
    new MentionPopup(input, agents);
    // Show popup, then clear input so getMentionContext returns no @
    triggerInput(input, "@al");
    input.value = "no-at-sign";
    input.selectionStart = 10;
    // Manually trigger Enter which calls select → getMentionContext → null → hide
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    expect(ul().style.display).toBe("none");
  });

  it("Enter when focusedIdx is out of bounds covers false branch of guard", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@"); // shows all 3, focusedIdx = 0
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    // focusedIdx is now 1; clear popup items while popup stays visible
    ul().innerHTML = ""; // items.length = 0
    // focusedIdx (1) >= items.length (0) → false branch of the guard
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    expect(ul().style.display).toBe("block"); // popup still open
  });

  it("Enter when focused item has no data-name skips select()", () => {
    new MentionPopup(input, agents);
    triggerInput(input, "@al"); // shows alpha, focusedIdx = 0
    ul().querySelector("li")!.removeAttribute("data-name"); // dataset["name"] undefined
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    expect(input.value).toBe("@al"); // value unchanged
  });

  it("selection preserves text after cursor", () => {
    new MentionPopup(input, agents);
    input.value = "@al world";
    input.selectionStart = 3; // cursor after "@al"
    input.selectionEnd = 3;
    input.dispatchEvent(new Event("input"));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    expect(input.value).toContain("@alpha ");
    expect(input.value).toContain("world");
  });
});
