import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Agent, CursorAgentError } from "@cursor/sdk";

function loadDotEnv(): void {
  try {
    const text = readFileSync(resolve(process.cwd(), ".env"), "utf8");
    for (const line of text.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) {
        continue;
      }
      const eq = trimmed.indexOf("=");
      if (eq <= 0) {
        continue;
      }
      const key = trimmed.slice(0, eq).trim();
      let value = trimmed.slice(eq + 1).trim();
      if (
        (value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'"))
      ) {
        value = value.slice(1, -1);
      }
      if (process.env[key] === undefined) {
        process.env[key] = value;
      }
    }
  } catch {
    // No .env file is fine; CURSOR_API_KEY can still come from the shell.
  }
}

loadDotEnv();

const prompt = process.argv.slice(2).join(" ").trim();
if (!prompt) {
  console.error('Usage: npm run ask-repo -- "<question about this repo>"');
  process.exit(1);
}

const apiKey = process.env.CURSOR_API_KEY?.trim();
if (!apiKey) {
  console.error(
    "Missing CURSOR_API_KEY. Mint one at https://cursor.com/dashboard/cloud-agents, then export it or put it in .env.",
  );
  process.exit(1);
}

try {
  const result = await Agent.prompt(prompt, {
    apiKey,
    model: { id: "composer-2" },
    local: { cwd: process.cwd() },
  });

  switch (result.status) {
    case "finished":
      console.log(result.result ?? "(no output)");
      process.exit(0);
      break;
    case "error":
      console.error(`run failed: ${result.id}`);
      process.exit(2);
      break;
    case "cancelled":
      console.error(`run cancelled: ${result.id}`);
      process.exit(2);
      break;
    default: {
      const _exhaustive: never = result.status;
      console.error(`unexpected status: ${_exhaustive}`);
      process.exit(2);
    }
  }
} catch (err) {
  if (err instanceof CursorAgentError) {
    console.error(`startup failed: ${err.message}, retryable=${err.isRetryable}`);
    process.exit(1);
  }
  throw err;
}
