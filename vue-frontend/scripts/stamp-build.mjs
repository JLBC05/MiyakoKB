#!/usr/bin/env node
// Stamp the committed web bundle with a fingerprint of the frontend sources.
//
// Why: the old release gate rebuilt the bundle in CI and required
// `git status --porcelain -- src/backend_v2/static/vue` to be empty. That only
// works when the build is byte-for-byte reproducible across machines, and it is
// not: Vue's scoped-style ids derive from the absolute file path, so a CI
// checkout always produces different hashes than a developer machine.
//
// Instead we record a hash of the *sources* next to the built assets. Committed
// assets are considered fresh when this fingerprint matches the checked-in one,
// which is path independent and catches the real failure mode (sources edited
// without rebuilding + committing the bundle).

import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '..')
const repoRoot = resolve(frontendRoot, '..')
const stampPath = join(repoRoot, 'src', 'backend_v2', 'static', 'vue', 'build-info.json')

// Files outside src/ that change the bundle content.
const EXTRA_FILES = [
  'package.json',
  'package-lock.json',
  'vite.config.ts',
  'index.html',
  'tsconfig.json',
  'tsconfig.app.json',
  'tsconfig.node.json',
]

const IGNORED_DIRS = new Set(['node_modules', 'dist', '.vite-tmp', '.cache'])

function walk(dir, acc) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (IGNORED_DIRS.has(entry.name)) continue
      walk(join(dir, entry.name), acc)
    } else if (entry.isFile()) {
      acc.push(join(dir, entry.name))
    } else {
      // Skip symlinks and other special entries: they are not bundle inputs.
    }
  }
  return acc
}

function collect() {
  const files = walk(join(frontendRoot, 'src'), [])
  for (const extra of EXTRA_FILES) {
    const full = join(frontendRoot, extra)
    if (existsSync(full) && statSync(full).isFile()) files.push(full)
  }
  return files
    .map((full) => ({ full, rel: relative(frontendRoot, full).split(sep).join('/') }))
    .sort((a, b) => (a.rel < b.rel ? -1 : a.rel > b.rel ? 1 : 0))
}

function fingerprint() {
  const hash = createHash('sha256')
  for (const { full, rel } of collect()) {
    const bytes = readFileSync(full)
    hash.update(rel, 'utf8')
    hash.update('\0', 'utf8')
    hash.update(String(bytes.length), 'utf8')
    hash.update('\0', 'utf8')
    hash.update(bytes)
    hash.update('\0', 'utf8')
  }
  return hash.digest('hex')
}

const check = process.argv.includes('--check')
const value = fingerprint()

if (check) {
  if (!existsSync(stampPath)) {
    console.error(`缺少 ${relative(repoRoot, stampPath)}：请先构建前端并提交产物`)
    process.exit(1)
  }
  const recorded = JSON.parse(readFileSync(stampPath, 'utf8')).sourceHash
  if (recorded !== value) {
    console.error('前端产物已过期：源码指纹与提交产物不一致')
    console.error(`  产物记录: ${recorded}`)
    console.error(`  当前源码: ${value}`)
    console.error('  请运行 npm run build 并提交 src/backend_v2/static/vue')
    process.exit(1)
  }
  console.log(`前端产物与源码一致（${value.slice(0, 16)}）`)
} else {
  writeFileSync(
    stampPath,
    JSON.stringify({ sourceHash: value }, null, 2) + '\n',
    'utf8',
  )
  console.log(`已写入 ${relative(repoRoot, stampPath)}（${value.slice(0, 16)}）`)
}
