#!/usr/bin/env node
// Browser Agent Extension - Test Runner
// Run all browser agent tests and report results

import { fileURLToPath } from 'node:url';
import { dirname, join, resolve, relative } from 'node:path';
import { access, readdir } from 'node:fs/promises';
import { execSync } from 'node:child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ============================================================================
// Configuration
// ============================================================================

const TEST_DIR = __dirname;
const SKIP_PATTERNS = [
  /^conftest\.mjs$/, // Skip fixture files
  /^run_all\.mjs$/,   // Skip this runner
  /^TEST_SUITE_PLAN\.md$/, // Skip plan document
];

const INTEGRATION_DIR = join(TEST_DIR, 'integration');
const E2E_DIR = join(TEST_DIR, 'e2e');
const BACKENDS_DIR = join(TEST_DIR, 'test_backends');

// ============================================================================
// Test Discovery
// ============================================================================

async function discoverTests(directory, recursive = false) {
  const tests = [];
  
  try {
    const entries = await readdir(directory, { withFileTypes: true });
    
    for (const entry of entries) {
      const fullPath = join(directory, entry.name);
      
      if (entry.isDirectory()) {
        if (recursive) {
          const nestedTests = await discoverTests(fullPath, true);
          tests.push(...nestedTests);
        }
      } else if (entry.isFile()) {
        // Only include .mjs test files
        if (entry.name.endsWith('.mjs') || entry.name.endsWith('.spec.mjs')) {
          // Skip patterns
          if (SKIP_PATTERNS.some(p => p.test(entry.name))) {
            continue;
          }
          tests.push(fullPath);
        }
      }
    }
  } catch (error) {
    console.warn(`Could not read directory: ${directory}`, error.message);
  }
  
  return tests;
}

// ============================================================================
// Test Execution
// ============================================================================

function runTest(filePath) {
  const relativePath = resolve(filePath).replace(resolve(TEST_DIR), '');
  const testName = relativePath.replace(/^\/[\/]/, '').replace(/\.mjs$/, '');
  
  console.log(`\n🧪 Running ${testName}...`);
  
  try {
    // Use child_process to run each test in isolation
    const options = {
      cwd: TEST_DIR,
      stdio: 'pipe',
      encoding: 'utf8',
      timeout: 30000, // 30 second timeout per test
    };

    const output = execSync(`node "${filePath}"`, options);
    console.log(output.trim());

    // A test file that exits 0 is considered passing. Node's native test
    // runner (node:test) and the suite's own `ok` convention both exit non-zero
    // on failure, so the exit code is the authoritative signal.
    return { path: filePath, name: testName, passed: true, output };
  } catch (error) {
    // execSync throws on a non-zero exit code; surface stdout/stderr.
    const output = (error.stdout || '') + (error.stderr || error.message || '');
    console.log(output.trim());
    return { path: filePath, name: testName, passed: false, output, error: error.message };
  }
}

// ============================================================================
// Result Reporting
// ============================================================================

function printResults(results) {
  const passed = results.filter(r => r.passed).length;
  const failed = results.filter(r => !r.passed).length;
  const total = results.length;
  
  console.log('\n' + '='.repeat(80));
  console.log(`TEST SUMMARY: ${passed}/${total} passed`);
  console.log('='.repeat(80));
  
  if (failed > 0) {
    console.log(`\n❌ FAILED TESTS (${failed}):`);
    console.log('-'.repeat(40));
    for (const result of results.filter(r => !r.passed)) {
      console.log(`  ✗ ${result.name}`);
      if (result.error) {
        console.log(`    Error: ${result.error}`);
      }
    }
  }
  
  if (passed > 0) {
    console.log(`\n✅ PASSED TESTS (${passed}):`);
    console.log('-'.repeat(40));
    for (const result of results.filter(r => r.passed)) {
      console.log(`  ✓ ${result.name}`);
    }
  }
  
  console.log('\n' + '='.repeat(80));
  
  return { passed, failed, total };
}

// ============================================================================
// Main Execution
// ============================================================================

async function main() {
  const includeE2E = process.argv.includes('--e2e');

  console.log('🔍 Discovering browser agent tests...');

  // Root-level unit tests
  const rootTests = await discoverTests(TEST_DIR, false);

  // Backend tests
  let backendTests = [];
  try {
    await access(BACKENDS_DIR);
    backendTests = await discoverTests(BACKENDS_DIR, false);
  } catch (e) {
    // Directory doesn't exist, skip it
  }

  // Integration tests (Node-powered; skip Playwright e2e unless --e2e)
  let integrationTests = [];
  try {
    const all = await discoverTests(INTEGRATION_DIR, false);
    integrationTests = includeE2E ? all : all.filter((p) => !p.endsWith('.spec.mjs'));
  } catch (e) {
    // Directory doesn't exist, skip it
  }

  // E2E tests require Playwright + a browser; only when --e2e is passed
  let e2eTests = [];
  if (includeE2E) {
    try {
      e2eTests = await discoverTests(E2E_DIR, false);
    } catch (e) {
      // Directory doesn't exist, skip it
    }
  }

  const testsToRun = [...rootTests, ...backendTests, ...integrationTests, ...e2eTests];

  if (testsToRun.length === 0) {
    console.log('⚠️  No tests found!');
    console.log(`Looking in: ${TEST_DIR}`);
    return;
  }

  console.log(`📋 Found ${testsToRun.length} test files:`);
  for (const test of testsToRun) {
    console.log(`   - ${relative(TEST_DIR, test)}`);
  }

  // Run tests sequentially
  const results = [];
  for (const testPath of testsToRun) {
    const result = runTest(testPath);
    results.push(result);
  }

  // Print summary
  const { passed, failed } = printResults(results);

  // Exit with appropriate code
  if (failed > 0) {
    console.log(`\n❌ ${failed} test(s) failed. Exiting with code 1.`);
    process.exit(1);
  } else {
    console.log(`\n✅ All ${passed} tests passed!`);
    process.exit(0);
  }
}

// Run main with error handling
main().catch(error => {
  console.error('❌ Test runner failed:', error);
  process.exit(1);
});
