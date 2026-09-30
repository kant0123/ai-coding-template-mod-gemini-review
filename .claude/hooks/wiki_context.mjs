#!/usr/bin/env node
// [オプション] 作業途中でも関連する Wiki ページが読まれるようにする PreToolUse hook。
//
// Edit / Write / MultiEdit / Read で触ろうとしているファイルを、Wiki ページ中のコード参照
// (`path/to/file.py:123 記号名()`)から逆引きし、そのファイルに触れているページの名前・summary・
// パスを additionalContext としてモデルに差し込む。ページ本文は入れない(読むかどうかは
// エージェントが決める。本文まで入れるとトークンを食い、読まれないまま流れる)。
//
// 【なぜ要るか】「着手前に wiki/index.md を読む」は着手時の 1 回きりで、作業途中に別領域の
// ファイルへ踏み込んでも関連ページは読み直されない。読むきっかけを「着手時」から
// 「そのファイルに触る瞬間」へ移す。
//
// 【挙動】
// - 同じページは 1 セッションにつき 1 回だけ差し込む(session_id ごとの状態を OS の一時
//   ディレクトリに置く)。何度も出ると読み飛ばされるようになる。
// - 触ったファイルから親へ辿って `wiki/index.md` と `.git` を持つディレクトリを探し、その
//   wiki/ を使う。worktree 内のファイルなら、その worktree(= そのブランチ)の Wiki が使われる。
// - **失敗したら何も出さず exit 0**(フェイルオープン)。これは助言であってガードではない。
//   他の hook(check_*.sh)がフェイルクローズなのは、素通しすると規約違反が黙って通るため。
//   こちらは素通ししても失うのは助言 1 回だけで、作業を止める方が害が大きい。
//
// 【計測】差し込みと、その後の Wiki ページの Read を `.claude/logs/wiki-context.jsonl` に残す
// (git 管理外)。効いているかは次で見る:
//
//   node .claude/hooks/wiki_context.mjs --stats
//
// .claude/settings.json への登録例は .claude/settings.example.json を参照。
// 使わない場合はこのファイルと該当 hook 設定を削除してよい。

import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

// 1 回に差し込むページの上限。これを超えるファイルはページの分け方の方を見直す合図。
const MAX_PAGES = 5;
// セッション状態ファイルを掃除するまでの日数。
const STATE_TTL_DAYS = 7;
const STATE_PREFIX = 'claude-wiki-context-';

const logFile = (base) => join(base, '.claude', 'logs', 'wiki-context.jsonl');

function log(base, entry) {
  try {
    mkdirSync(dirname(logFile(base)), { recursive: true });
    appendFileSync(logFile(base), `${JSON.stringify({ ts: new Date().toISOString(), ...entry })}\n`);
  } catch {
    // 計測の失敗で作業を止めない。
  }
}

/** ファイルの置き場所から親へ辿り、wiki/ を持つリポジトリ(worktree を含む)の根を探す。 */
function findRoot(start) {
  let dir = start;
  for (;;) {
    if (existsSync(join(dir, 'wiki', 'index.md')) && existsSync(join(dir, '.git'))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function stateFile(sessionId) {
  return join(tmpdir(), `${STATE_PREFIX}${String(sessionId).replace(/[^\w-]/g, '_')}.json`);
}

function readState(file) {
  try {
    return new Set(JSON.parse(readFileSync(file, 'utf8')));
  } catch {
    return null;
  }
}

/** 古いセッションの状態ファイルを消す。新しいセッションの初回だけ走らせる。 */
function sweepStates() {
  const limit = Date.now() - STATE_TTL_DAYS * 24 * 60 * 60 * 1000;
  try {
    for (const name of readdirSync(tmpdir())) {
      if (!name.startsWith(STATE_PREFIX)) continue;
      const full = join(tmpdir(), name);
      try {
        if (statSync(full).mtimeMs < limit) unlinkSync(full);
      } catch {
        // 1 ファイルが別プロセスに掴まれていても、残りの掃除は続ける。
      }
    }
  } catch {
    // 掃除できなくても困らない。
  }
}

/** Windows はパスの大文字小文字を区別しないので、表のキーと突き合わせるときに揃える。 */
function lookup(byFile, rel) {
  if (byFile.has(rel)) return byFile.get(rel);
  if (process.platform !== 'win32') return [];
  const lower = rel.toLowerCase();
  for (const [key, pages] of byFile) if (key.toLowerCase() === lower) return pages;
  return [];
}

function stats(base) {
  let raw;
  try {
    raw = readFileSync(logFile(base), 'utf8');
  } catch {
    console.log(`記録がありません: ${logFile(base)}`);
    return;
  }
  // 行ごとに読む。複数セッションの同時追記や強制終了で壊れた行が 1 行混ざっても、
  // 残りの記録ごと捨てない。
  const lines = [];
  let broken = 0;
  for (const line of raw.split('\n').filter((l) => l.trim())) {
    try {
      lines.push(JSON.parse(line));
    } catch {
      broken += 1;
    }
  }
  if (broken) console.log(`(読めない行を ${broken} 行飛ばした)`);
  // (セッション, ページ) ごとに「差し込まれた後に Read されたか」を見る。
  const injected = new Map();
  for (const entry of lines) {
    if (entry.event === 'inject' && Array.isArray(entry.pages)) {
      for (const page of entry.pages) {
        const key = `${entry.session}\t${page}`;
        if (!injected.has(key)) injected.set(key, { page, read: false });
      }
    } else if (entry.event === 'read') {
      const hit = injected.get(`${entry.session}\t${entry.page}`);
      if (hit) hit.read = true;
    }
  }
  const all = [...injected.values()];
  const read = all.filter((item) => item.read).length;
  const rate = all.length ? Math.round((read / all.length) * 100) : 0;
  console.log(`差し込み: ${all.length} 件(セッション × ページ)/ その後 Read された: ${read} 件 (${rate}%)`);
  const perPage = new Map();
  for (const { page, read: wasRead } of all) {
    const count = perPage.get(page) ?? { shown: 0, read: 0 };
    count.shown += 1;
    if (wasRead) count.read += 1;
    perPage.set(page, count);
  }
  for (const [page, count] of [...perPage].sort((a, b) => b[1].shown - a[1].shown)) {
    console.log(`  ${page}: ${count.read}/${count.shown}`);
  }
}

async function main() {
  const base = process.env.CLAUDE_PROJECT_DIR ?? process.cwd();
  if (process.argv.includes('--stats')) {
    stats(base);
    return;
  }

  const input = JSON.parse(readFileSync(0, 'utf8'));
  const target = input.tool_input?.file_path ?? input.tool_input?.notebook_path;
  if (typeof target !== 'string' || !target) return;
  const abs = resolve(input.cwd ?? process.cwd(), target);
  const root = findRoot(dirname(abs));
  if (!root) return;
  const rel = relative(root, abs).split(sep).join('/');
  // 記録はセッションのプロジェクト(メインツリー)に寄せる。worktree 側に置くと削除で消える。
  const logBase = process.env.CLAUDE_PROJECT_DIR ?? root;
  const session = input.session_id ?? 'unknown';

  if (rel.startsWith('wiki/')) {
    if (input.tool_name === 'Read' && rel.endsWith('.md')) {
      log(logBase, { event: 'read', session, page: rel.split('/').pop().slice(0, -3) });
    }
    return;
  }

  // 逆引きはそのリポジトリ自身の解析部品で行う(ブランチで記法が変わっていても追従する)。
  const refsModule = join(root, 'scripts', 'wiki-refs.js');
  if (!existsSync(refsModule)) return;
  const { pagesByCodeFile } = await import(pathToFileURL(refsModule).href);
  const pages = lookup(pagesByCodeFile(root), rel);
  if (pages.length === 0) return;

  const file = stateFile(session);
  const shown = readState(file);
  if (shown === null) sweepStates();
  const seen = shown ?? new Set();
  const fresh = pages.filter((page) => !seen.has(page.name)).slice(0, MAX_PAGES);
  if (fresh.length === 0) return;
  for (const page of fresh) seen.add(page.name);
  writeFileSync(file, JSON.stringify([...seen]));
  log(logBase, { event: 'inject', session, tool: input.tool_name, file: rel, pages: fresh.map((p) => p.name) });

  const lines = fresh.map((page) => `- ${join(root, page.rel)} — ${page.summary || '(summary 未記入)'}`);
  const context = [
    `[wiki] \`${rel}\` は次の Wiki ページに記述がある。過去の経緯・落とし穴・設計判断が`,
    '書かれているので、このファイルを変える判断の前に読むこと(このセッションでは再表示しない):',
    ...lines,
  ].join('\n');
  process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', additionalContext: context } }));
}

// フェイルオープン(冒頭のコメント参照)。process.exit() は使わない — パイプへの書き込みが
// 終わる前にプロセスが落ち、差し込みが途中で切れることがある。
main().catch(() => {});
process.exitCode = 0;
