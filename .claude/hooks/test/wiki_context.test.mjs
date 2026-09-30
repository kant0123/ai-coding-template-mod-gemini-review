// .claude/hooks/wiki_context.mjs の回帰テスト。
//
//   node --test .claude/hooks/test/wiki_context.test.mjs
//
// 一時ディレクトリに「wiki/ とコードを持つリポジトリ」を組み立て、hook を実プロセスとして
// 起動して標準出力を見る(Claude Code が実際に呼ぶのと同じ形)。

import { spawnSync } from 'node:child_process';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { after, test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const HOOK = fileURLToPath(new URL('../wiki_context.mjs', import.meta.url));
const REFS = fileURLToPath(new URL('../../../scripts/wiki-refs.js', import.meta.url));

const sandbox = mkdtempSync(join(tmpdir(), 'wiki-context-test-'));
after(() => rmSync(sandbox, { recursive: true, force: true }));

/** wiki/ と scripts/wiki-refs.js と、ページから参照されるコードを持つリポジトリを作る。 */
function makeRepo(name, { page, summary, codeFile }) {
  const root = join(sandbox, name);
  mkdirSync(join(root, '.git'), { recursive: true });
  mkdirSync(join(root, 'wiki', 'components'), { recursive: true });
  mkdirSync(join(root, 'scripts'), { recursive: true });
  mkdirSync(join(root, 'app'), { recursive: true });
  copyFileSync(REFS, join(root, 'scripts', 'wiki-refs.js'));
  writeFileSync(join(root, 'wiki', 'index.md'), '# Index\n');
  writeFileSync(
    join(root, 'wiki', 'components', `${page}.md`),
    `---\ntype: component\nsummary: ${summary}\nupdated: 2026-09-30\nrelated: []\n---\n\n` +
      `入口は \`${codeFile}:1 login()\`。\n\n` +
      // フェンス内の記入例は参照として数えない。
      '```\n`app/example.py:1 sample()`\n```\n',
  );
  writeFileSync(join(root, codeFile), 'def login():\n    pass\n');
  return root;
}

const main = makeRepo('main', { page: 'auth-session', summary: 'Cookie の有効期限の落とし穴', codeFile: 'app/auth.py' });
const worktree = makeRepo('main-feature', { page: 'billing', summary: '請求の締め処理', codeFile: 'app/billing.py' });
const stateDir = join(sandbox, 'state');
mkdirSync(stateDir);

function runHook(input, args = []) {
  const result = spawnSync(process.execPath, [HOOK, ...args], {
    input: typeof input === 'string' ? input : JSON.stringify(input),
    encoding: 'utf8',
    // セッション状態は OS の一時ディレクトリに置かれるので、テスト用に差し替える。
    env: { ...process.env, CLAUDE_PROJECT_DIR: main, TMPDIR: stateDir, TEMP: stateDir, TMP: stateDir },
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

const call = (session, tool, file) => ({ session_id: session, cwd: main, tool_name: tool, tool_input: { file_path: file } });
const context = (stdout) => JSON.parse(stdout).hookSpecificOutput.additionalContext;

test('関連ページのあるファイルを編集すると、ページ名と summary を差し込む', () => {
  const out = runHook(call('s1', 'Edit', join(main, 'app', 'auth.py')));
  assert.equal(JSON.parse(out).hookSpecificOutput.hookEventName, 'PreToolUse');
  assert.match(context(out), /auth-session\.md — Cookie の有効期限の落とし穴/);
  assert.match(context(out), /`app\/auth\.py`/);
});

test('同じセッションでは同じページを二度出さない', () => {
  assert.equal(runHook(call('s1', 'Read', join(main, 'app', 'auth.py'))), '');
});

test('別のセッションでは改めて出す', () => {
  assert.match(context(runHook(call('s2', 'Read', join(main, 'app', 'auth.py')))), /auth-session/);
});

test('相対パスは cwd から解決する', () => {
  assert.match(context(runHook(call('s3', 'Edit', 'app/auth.py'))), /auth-session/);
});

test('関連ページの無いファイル・フェンス内の記入例だけが指すファイルでは何も出さない', () => {
  writeFileSync(join(main, 'app', 'example.py'), '');
  assert.equal(runHook(call('s4', 'Edit', join(main, 'app', 'other.py'))), '');
  assert.equal(runHook(call('s4', 'Edit', join(main, 'app', 'example.py'))), '');
});

test('wiki/ 自体の編集では何も出さない', () => {
  assert.equal(runHook(call('s5', 'Edit', join(main, 'wiki', 'components', 'auth-session.md'))), '');
});

test('worktree 内のファイルには、その worktree の wiki/ を使う', () => {
  const out = runHook(call('s6', 'Edit', join(worktree, 'app', 'billing.py')));
  assert.match(context(out), /billing\.md — 請求の締め処理/);
  assert.equal(runHook(call('s6', 'Edit', join(worktree, 'app', 'auth.py'))), '');
});

test('壊れた入力・パスの無い入力では何も出さず exit 0(フェイルオープン)', () => {
  assert.equal(runHook('not json'), '');
  assert.equal(runHook({ session_id: 's7', tool_name: 'Bash', tool_input: { command: 'ls' } }), '');
});

test('差し込みと、その後の Wiki ページの Read を記録し、--stats で追読率を出す', () => {
  runHook(call('s8', 'Edit', join(main, 'app', 'auth.py')));
  runHook(call('s8', 'Read', join(main, 'wiki', 'components', 'auth-session.md')));
  const log = readFileSync(join(main, '.claude', 'logs', 'wiki-context.jsonl'), 'utf8');
  assert.match(log, /"event":"inject".*"session":"s8"/);
  assert.match(log, /"event":"read","session":"s8","page":"auth-session"/);
  // s1 / s2 / s3 / s6 / s8 の 5 件が差し込まれ、Read されたのは s8 だけ。
  assert.match(runHook('', ['--stats']), /差し込み: 5 件.*Read された: 1 件 \(20%\)/);
});
