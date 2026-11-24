// Lets a task's prompt.md reuse part of a shared skill word for word, so the
// two can't drift apart:
//
//   {{include .agents/skills/to-tickets/SKILL.md <issue-template>}}
//   {{include .agents/skills/to-tickets/SKILL.md ### 3. Draft vertical slices}}
//
// The path is from the factory repo's root. `<tag>` takes what's between
// <tag> and </tag>; a heading takes everything under it up to the next
// heading of the same or a higher level. A part that isn't there is an error,
// so a renamed skill section breaks the tests, not a run's prompt.

import { readFileSync } from "node:fs";
import { join } from "node:path";

const part = (text: string, selector: string, file: string): string => {
  const tag = selector.match(/^<([\w-]+)>$/);
  if (tag) {
    const found = text.match(new RegExp(`<${tag[1]}>\\n([\\s\\S]*?)\\n</${tag[1]}>`));
    if (!found) throw new Error(`no ${selector} in ${file}`);
    return found[1]!.trim();
  }
  const heading = selector.match(/^(#+) /);
  if (!heading) throw new Error(`can't include "${selector}": expected <tag> or a heading`);
  const lines = text.split("\n");
  const start = lines.findIndex((line) => line.trim() === selector);
  if (start < 0) throw new Error(`no "${selector}" in ${file}`);
  const level = heading[1]!.length;
  const end = lines.findIndex(
    (line, i) => i > start && /^#+ /.test(line) && line.match(/^#+/)![0].length <= level,
  );
  return lines.slice(start + 1, end < 0 ? undefined : end).join("\n").trim();
};

export const expandIncludes = (prompt: string, root: string): string =>
  prompt.replace(/\{\{include (\S+) (.+?)\}\}/g, (_, file: string, selector: string) =>
    part(readFileSync(join(root, file), "utf8"), selector.trim(), file),
  );
