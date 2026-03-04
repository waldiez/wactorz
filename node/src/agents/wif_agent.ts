/**
 * WifAgent — finance expert (Node.js port).
 * In-memory expense tracker + financial calculators. agentType: "financier"
 */

import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

interface Expense {
  amount:    number;
  category:  string;
  note:      string;
  timestamp: number;
}

const HELP = `**WIF — Finance Expert** 💹
_Waldiez In Finance_

\`\`\`
add <amount> [category] [note]          log an expense
budget <amount>                         set monthly budget limit
report                                  spending breakdown by category
balance                                 budget vs total spent
compound <principal> <rate%> <years>    compound interest (monthly)
loan <principal> <rate%> <months>       monthly loan payment
roi <investment> <return>               return on investment %
tax <income> [bracket%]                 simple tax estimate (default 25%)
tip <bill> [percent]                    tip calculator (default 15%)
help                                    this message
\`\`\``;

export class WifAgent extends Actor {
  private _expenses: Expense[] = [];
  private _budget = 0;

  constructor(publish: MqttPublisher, actorId?: string) {
    super("wif-agent", publish, actorId);
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("financier");
  }

  override async handleMessage(msg: Message): Promise<void> {
    if (msg.type !== MessageType.Task && msg.type !== MessageType.Text) return;
    let text = Actor.extractText(msg.payload).trim();
    if (!text) return;
    text = Actor.stripPrefix(text, "@wif-agent", "@wif_agent");
    const parts = text.trim().split(/\s+/);
    const cmd = (parts[0] ?? "").toLowerCase();
    const args = parts.slice(1);
    const reply = this._dispatch(cmd, args);
    this.replyChat(reply);
    this.metrics.tasksCompleted++;
  }

  private _dispatch(cmd: string, args: string[]): string {
    switch (cmd) {
      case "add":      return this._cmdAdd(args);
      case "budget":   return this._cmdBudget(args);
      case "report":   return this._cmdReport();
      case "balance":  return this._cmdBalance();
      case "compound": return this._cmdCompound(args);
      case "loan":     return this._cmdLoan(args);
      case "roi":      return this._cmdRoi(args);
      case "tax":      return this._cmdTax(args);
      case "tip":      return this._cmdTip(args);
      case "help":
      case "":         return HELP;
      default:         return `Unknown command: \`${cmd}\`. Type \`help\`.`;
    }
  }

  private _cmdAdd(args: string[]): string {
    if (!args[0]) return "Usage: `add <amount> [category] [note]`";
    const amount = parseFloat(args[0].replace(/^[$€£¥+]/, ""));
    if (isNaN(amount) || amount <= 0) return `Invalid amount: \`${args[0]}\``;
    const category = args[1]?.toLowerCase() ?? "misc";
    const note = args.slice(2).join(" ");
    this._expenses.push({ amount, category, note, timestamp: Date.now() });
    const header = `Logged **$${amount.toFixed(2)}** → \`${category}\`` + (note ? ` — _${note}_` : "");
    if (this._budget > 0) {
      const spent = this._expenses.reduce((s, e) => s + e.amount, 0);
      const pct = Math.min((spent / this._budget) * 100, 999);
      const icon = pct >= 100 ? "🚨" : pct >= 80 ? "⚠" : "✓";
      return `${header}\n${icon} Budget: $${spent.toFixed(2)} / $${this._budget.toFixed(2)} (${pct.toFixed(0)}%)`;
    }
    return header;
  }

  private _cmdBudget(args: string[]): string {
    if (!args[0]) return "Usage: `budget <amount>`";
    const amount = parseFloat(args[0].replace(/^[$€£¥]/, ""));
    if (isNaN(amount)) return `Invalid amount: \`${args[0]}\``;
    this._budget = amount;
    const spent = this._expenses.reduce((s, e) => s + e.amount, 0);
    const pct = amount > 0 ? Math.min((spent / amount) * 100, 999) : 0;
    return `Budget set: **$${amount.toFixed(2)}** | Spent so far: $${spent.toFixed(2)} (${pct.toFixed(0)}%)`;
  }

  private _cmdReport(): string {
    if (this._expenses.length === 0) return "No expenses yet. Try: `add 25 food coffee`";
    const byCat: Record<string, number> = {};
    for (const e of this._expenses) byCat[e.category] = (byCat[e.category] ?? 0) + e.amount;
    const total = Object.values(byCat).reduce((s, v) => s + v, 0);
    const rows = Object.entries(byCat)
      .sort(([, a], [, b]) => b - a)
      .map(([cat, amt]) => `  **${cat}**: $${amt.toFixed(2)} (${((amt / total) * 100).toFixed(0)}%)`);
    return `**Expense Report** (${this._expenses.length} transactions)\n\n${rows.join("\n")}\n\n**Total: $${total.toFixed(2)}**`;
  }

  private _cmdBalance(): string {
    const spent = this._expenses.reduce((s, e) => s + e.amount, 0);
    if (this._budget <= 0) {
      return `**Balance**\n\nTotal spent: **$${spent.toFixed(2)}** (${this._expenses.length} transactions)\nBudget: _not set_`;
    }
    const rem = this._budget - spent;
    const pct = Math.min((spent / this._budget) * 100, 999);
    const icon = pct >= 100 ? "🚨" : pct >= 80 ? "⚠" : "✓";
    return (
      `**Monthly Budget Balance**\n\n${icon} $${spent.toFixed(2)} / $${this._budget.toFixed(2)} (${pct.toFixed(0)}%)\n` +
      (rem >= 0 ? `$${rem.toFixed(2)} left` : `$${(-rem).toFixed(2)} over budget`)
    );
  }

  private _cmdCompound(args: string[]): string {
    if (args.length < 3) return "Usage: `compound <principal> <rate%> <years>`";
    const [p, r, t] = args.map((a, i) => parseFloat(i === 1 ? a.replace(/%$/, "") : a.replace(/^\$/, "")));
    if ([p, r, t].some(isNaN)) return "Invalid numbers.";
    const n  = 12;
    const fv = p * Math.pow(1 + r / 100 / n, n * t);
    return `**Compound Interest**\n\nPrincipal: $${p.toLocaleString("en")} | Rate: ${r}% | Term: ${t}y\n→ **Future Value: $${fv.toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}** (interest: $${(fv - p).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })})`;
  }

  private _cmdLoan(args: string[]): string {
    if (args.length < 3) return "Usage: `loan <principal> <rate%> <months>`";
    const p      = parseFloat(args[0].replace(/^\$/, ""));
    const annual = parseFloat(args[1].replace(/%$/, ""));
    const n      = parseFloat(args[2]);
    if ([p, annual, n].some(isNaN)) return "Invalid numbers.";
    const r = annual / 100 / 12;
    const monthly = r ? p * r * Math.pow(1 + r, n) / (Math.pow(1 + r, n) - 1) : p / n;
    const total   = monthly * n;
    return `**Loan Calculator**\n\nPrincipal: $${p.toLocaleString("en")} | Rate: ${annual}% | Term: ${n}mo\n→ **Monthly: $${monthly.toFixed(2)}** | Total: $${total.toFixed(2)} | Interest: $${(total - p).toFixed(2)}`;
  }

  private _cmdRoi(args: string[]): string {
    if (args.length < 2) return "Usage: `roi <investment> <return>`";
    const inv = parseFloat(args[0].replace(/^\$/, ""));
    const ret = parseFloat(args[1].replace(/^\$/, ""));
    if ([inv, ret].some(isNaN) || inv === 0) return "Invalid numbers.";
    const gain = ret - inv;
    const roi  = (gain / inv) * 100;
    return `**ROI**\n\nInvestment: $${inv.toFixed(2)} | Return: $${ret.toFixed(2)}\n→ Gain: $${gain >= 0 ? "+" : ""}${gain.toFixed(2)} | **ROI: ${roi >= 0 ? "+" : ""}${roi.toFixed(2)}%**`;
  }

  private _cmdTax(args: string[]): string {
    if (!args[0]) return "Usage: `tax <income> [bracket%]`";
    const income = parseFloat(args[0].replace(/^\$/, ""));
    const rate   = args[1] ? parseFloat(args[1].replace(/%$/, "")) : 25;
    if ([income, rate].some(isNaN)) return "Invalid numbers.";
    const tax = income * rate / 100;
    return `**Tax Estimate**\n\nIncome: $${income.toLocaleString("en")} | Rate: ${rate}%\n→ Tax: **$${tax.toLocaleString("en", { minimumFractionDigits: 2 })}** | Net: **$${(income - tax).toLocaleString("en", { minimumFractionDigits: 2 })}**\n_Simplified estimate — consult a professional._`;
  }

  private _cmdTip(args: string[]): string {
    if (!args[0]) return "Usage: `tip <bill> [percent]`";
    const bill = parseFloat(args[0].replace(/^\$/, ""));
    const pct  = args[1] ? parseFloat(args[1].replace(/%$/, "")) : 15;
    if ([bill, pct].some(isNaN)) return "Invalid numbers.";
    const tip   = bill * pct / 100;
    const total = bill + tip;
    const splits = [1, 2, 3, 4, 5].map((n) => `  ${n} people: $${(total / n).toFixed(2)}/person`).join("\n");
    return `**Tip Calculator**\n\nBill: $${bill.toFixed(2)} | Tip (${pct.toFixed(0)}%): $${tip.toFixed(2)} | **Total: $${total.toFixed(2)}**\n\n${splits}`;
  }
}
