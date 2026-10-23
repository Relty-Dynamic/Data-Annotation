/** @type {import('@stryker-mutator/api/core').PartialStrykerOptions} */
export default {
  mutate: [
    'src/accountRoles.ts',
    'src/tasks/permissions.ts',
    'src/tasks/accountFilters.ts',
  ],
  testRunner: 'command',
  commandRunner: {command: 'npm run test:roles'},
  // The existing node:test suite runs in full for each mutant. The command
  // runner cannot collect per-test coverage; this changes scheduling only.
  coverageAnalysis: 'off',
  concurrency: 2,
  reporters: ['clear-text', 'html', 'json'],
  htmlReporter: {fileName: '.local/stryker-roles/mutation.html'},
  jsonReporter: {fileName: '.local/stryker-roles/mutation.json'},
  tempDirName: '.tmp/stryker-roles',
  ignorePatterns: ['dist/**', 'public/**', '.local/**', '.tmp/**', '.env', '.env.*', 'test/layout/**'],
  thresholds: {high: 100, low: 90, break: 100},
};
