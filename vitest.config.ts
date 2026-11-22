import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: [".sandcastle/**/*.test.ts"],
  },
});
