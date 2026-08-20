#!/usr/bin/env node
// wiki/ の機械的な整合性チェック。
//
//   node scripts/wiki-lint.js
//
// package.json がある場合は "wiki:lint": "node scripts/wiki-lint.js" を scripts に足すと
// `npm run wiki:lint` で走る。Node 18 以降の標準ライブラリだけで動き、外部依存は無い。
//
// 【何を見て、何を見ないか】
// エージェントに任せる Lint(矛盾するページ・コードと乖離した記述・言及されるのにページが
// 無い概念)は意味の理解が要るのでここでは扱わない。ここが見るのは「1 ページを読むだけでは
// 分からないが、突き合わせれば機械的に判定できる」もの — つまり大域的な一貫性だけ。
//
// エージェントは 1 ページ内で完結する局所ルール(frontmatter を書く、テンプレートに従う)は
// よく守る一方、Wiki 全体にまたがる整合性は自然には保てない。そこが壊れる。

import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('..', import.meta.url));
const WIKI = join(ROOT, 'wiki');

// frontmatter を持たない例外。カタログとログでありページではないため(CLAUDE.md の取り決め)。
const NO_FRONTMATTER = new Set(['index', 'log']);
// 孤立ページの検査から外すページ。overview は index から辿る入口で、
// 他ページから参照されていなくてよい(ページが 1 枚も無い初期状態でも成立させる)。
const ORPHAN_EXEMPT = new Set(['index', 'log', 'overview']);
// ディレクトリと type の対応。ここに無い場所(wiki 直下)は type の値域だけ見る。
const DIR_TYPE = { components: 'component', concepts: 'concept', operations: 'operation' };
const TYPES = new Set(Object.values(DIR_TYPE));
// index.md でこの見出し以降に並ぶリンクは「まだ書かれていないページ」の控えなので、
// 参照先が無くてよい(CLAUDE.md / docs/wiki_workflow.md の取り決め)。
const PLANNED_HEADING = /^#{1,6}\s*未作成ページ/m;

// コード参照 `path/to/file.js:123 記号名` の記法。記号名は任意だが、付いていれば検証する。
// 行番号だけのアンカーは編集のたびに黙ってずれるので、記号名を主・行番号を補助として扱う。
const CODE_REF = /^([\w./-]+\/[\w.-]+\.[a-zA-Z0-9]{1,5}):(\d+)(?:\s+(.+))?$/;
// 記号名が指す行と、書かれている行番号のズレをどこまで許すか。
const ANCHOR_SLACK = 5;

// index.md のカタログは各ページの frontmatter の summary から生成する。
// 手書きの一行要約はページ本文と別々に育ち、必ず片方が古くなるため。
const INDEX_START = '<!-- wiki-index:start -->';
const INDEX_END = '<!-- wiki-index:end -->';
const INDEX_SECTIONS = [
  { dir: null, heading: '## 全体像' },
  { dir: 'components', heading: '## Components — モジュール・機能単位 (`components/`)' },
  { dir: 'concepts', heading: '## Concepts — 横断的な仕組み・設計判断 (`concepts/`)' },
  { dir: 'operations', heading: '## Operations — 運用手順・障害対応 (`operations/`)' },
];
const WRITE_INDEX = process.argv.includes('--write-index');

const errors = [];
const notes = [];
const fail = (file, message) => errors.push({ file, message });

/** wiki/ 配下の .md を再帰的に集める(テンプレートは検査対象外)。 */
function collect(dir) {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...collect(full));
    else if (entry.name.endsWith('.md') && entry.name !== '_template.md') found.push(full);
  }
  return found;
}

/**
 * リンク抽出の前に、リンクとして数えてはいけない部分を落とす。
 * - コードブロック / コードスパン: 記法そのものを説明している箇所(「`[[...]]` で参照する」)
 * - HTML コメント: index.md の記入例が `<!-- 例: - [[auth-session]] ... -->` の形で入っている
 */
function stripNonLinks(text) {
  return text
    .replace(/```[\s\S]*?```/g, '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/`[^`\n]*`/g, '');
}

/**
 * コードスパン(`...`)の中身を集める。フェンス付きコードブロックは記法の説明・入力例が
 * 入るので対象外(そこに書かれたパスは実在しなくてよい)。
 */
function codeSpans(text) {
  return [...text.replace(/```[\s\S]*?```/g, '').matchAll(/`([^`\n]+)`/g)].map((m) => m[1].trim());
}

/** `<YYYY-MM-DD>` のような未記入プレースホルダか。テンプレート初期状態を落とさないため。 */
const isPlaceholder = (value) => typeof value === 'string' && /^<.*>$/.test(value.trim());

/** frontmatter を最小限に解釈する。YAML パーサは入れない(依存を増やさないため)。 */
function parseFrontmatter(text) {
  if (!text.startsWith('---')) return null;
  const end = text.indexOf('\n---', 3);
  if (end === -1) return null;
  const fields = {};
  for (const line of text.slice(3, end).split('\n')) {
    const match = /^([a-z]+):\s*(.*)$/.exec(line.trim());
    if (!match) continue;
    const [, key, rawValue] = match;
    const value = rawValue.trim();
    fields[key] = value.startsWith('[')
      ? value.replace(/^\[|\]$/g, '').split(',').map((v) => v.trim()).filter(Boolean)
      : value;
  }
  return fields;
}

/** そのファイルを最後に触ったコミットの日付。取れなければ null(git 管理外・浅いクローン)。 */
function lastCommitDate(file) {
  try {
    const out = execFileSync('git', ['log', '-1', '--format=%ad', '--date=short', '--', file], {
      cwd: ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    return out.trim() || null;
  } catch {
    return null;
  }
}

if (!existsSync(WIKI)) {
  console.error('wiki/ が見つかりません。リポジトリ直下で実行してください。');
  process.exit(1);
}

/**
 * その行の周辺が最後に変わったコミットの日付。ファイル全体ではなく行を見るのは、
 * 同じファイルの無関係な編集で毎回警告を出さないため。取れなければ null
 * (浅いクローン・行数超過・リネーム直後など。既存の日付検査と同じく黙って飛ばす)。
 */
function lastCommitDateForLine(file, line) {
  try {
    const out = execFileSync(
      'git',
      ['log', '-1', '--format=%ad', '--date=short', '-s', `-L${line},${line}:${file}`],
      { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
    );
    return out.trim() || null;
  } catch {
    return null;
  }
}

const pages = new Map();
for (const file of collect(WIKI)) {
  const rel = relative(ROOT, file).split(sep).join('/');
  const parts = rel.split('/');
  const name = parts[parts.length - 1].slice(0, -3);
  const text = readFileSync(file, 'utf8');
  pages.set(name, {
    rel,
    name,
    dir: parts.length > 2 ? parts[1] : null,
    text,
    frontmatter: parseFrontmatter(text),
    links: [...stripNonLinks(text).matchAll(/\[\[([^\]]+)\]\]/g)].map((m) => m[1].trim()),
  });
}

// index.md の「未作成ページ」節に控えられているリンク(= 意図的なリンク切れ)。
// 各行は `- [[まだ無いページ]] — [[参照元]] から参照されている` の形なので、
// **行の最初のリンクだけ**が「未作成ページ」。2 つ目以降は実在する参照元なので数えない。
const planned = new Set();
const index = pages.get('index');
if (index) {
  const match = PLANNED_HEADING.exec(index.text);
  if (match) {
    for (const line of stripNonLinks(index.text.slice(match.index)).split('\n')) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('-')) continue;
      const first = /\[\[([^\]]+)\]\]/.exec(trimmed);
      if (first) planned.add(first[1].trim());
    }
  }
}

// --- 1. frontmatter ---------------------------------------------------------
for (const page of pages.values()) {
  if (NO_FRONTMATTER.has(page.name)) {
    if (page.frontmatter) fail(page.rel, 'frontmatter を持たない取り決めのページに frontmatter がある');
    continue;
  }
  if (!page.frontmatter) {
    fail(page.rel, 'frontmatter が無い');
    continue;
  }
  const { type, updated, related } = page.frontmatter;

  if (!type) fail(page.rel, 'frontmatter に type が無い');
  else if (isPlaceholder(type)) notes.push(`${page.rel}: type が未記入(${type})`);
  else if (!TYPES.has(type)) fail(page.rel, `type の値が不正: ${type} (${[...TYPES].join(' | ')})`);
  else if (page.dir && DIR_TYPE[page.dir] && DIR_TYPE[page.dir] !== type) {
    fail(page.rel, `type が置き場所と食い違う: ${page.dir}/ なら ${DIR_TYPE[page.dir]} のはずが ${type}`);
  }

  if (!updated) fail(page.rel, 'frontmatter に updated が無い');
  else if (isPlaceholder(updated)) notes.push(`${page.rel}: updated が未記入(${updated})`);
  else if (!/^\d{4}-\d{2}-\d{2}$/.test(updated)) fail(page.rel, `updated の形式が不正: ${updated}`);
  else {
    const committed = lastCommitDate(join(ROOT, page.rel));
    if (committed && committed !== updated) {
      fail(page.rel, `updated (${updated}) が最終コミット日 (${committed}) と食い違う`);
    }
  }

  const summary = page.frontmatter.summary;
  if (!summary) fail(page.rel, 'frontmatter に summary が無い(index.md のカタログはここから生成される)');
  else if (isPlaceholder(summary)) notes.push(`${page.rel}: summary が未記入(${summary})`);

  if (!Array.isArray(related)) fail(page.rel, 'frontmatter の related が配列でない');
}

// --- 2. リンクの解決 --------------------------------------------------------
// 参照先が無いリンクは、index.md の「未作成ページ」に控えられていれば意図的とみなす。
for (const page of pages.values()) {
  for (const link of new Set(page.links)) {
    if (pages.has(link) || planned.has(link)) continue;
    fail(page.rel, `[[${link}]] の参照先が存在せず、index.md の「未作成ページ」にも無い` +
      (link.includes('/') || link.includes('.')
        ? '(コードのパスは wikilink ではなくバッククォートで書く)' : ''));
  }
  for (const target of page.frontmatter?.related ?? []) {
    if (!pages.has(target)) fail(page.rel, `related の ${target} が存在しない`);
  }
}
for (const link of planned) {
  if (pages.has(link)) fail('wiki/index.md', `「未作成ページ」の ${link} は既に存在する(節から消す)`);
}

// --- 3. related の対称性 ----------------------------------------------------
for (const page of pages.values()) {
  for (const target of page.frontmatter?.related ?? []) {
    const other = pages.get(target);
    if (other?.frontmatter && !(other.frontmatter.related ?? []).includes(page.name)) {
      fail(other.rel, `related が片方向: ${page.name} は ${target} を挙げているが逆が無い`);
    }
  }
}

// --- 4. 孤立ページ ----------------------------------------------------------
// index.md(カタログ)と log.md(履歴)からの被リンクは数えない。そこからは必ず張られるため、
// 数えると「本文のどこからも参照されていないページ」を検出できなくなる。
const inbound = new Map([...pages.keys()].map((name) => [name, 0]));
for (const page of pages.values()) {
  if (NO_FRONTMATTER.has(page.name)) continue;
  for (const link of new Set(page.links)) {
    if (link !== page.name && inbound.has(link)) inbound.set(link, inbound.get(link) + 1);
  }
}
for (const [name, count] of inbound) {
  if (!ORPHAN_EXEMPT.has(name) && count === 0) {
    fail(pages.get(name).rel, '孤立ページ(index.md / log.md 以外のどこからも [[リンク]] されていない)');
  }
}

// --- 5. index.md の網羅性 ---------------------------------------------------
if (!index) fail('wiki/index.md', 'index.md が無い');
else {
  const listed = new Set(index.links);
  for (const page of pages.values()) {
    if (NO_FRONTMATTER.has(page.name)) continue;
    if (!listed.has(page.name)) fail('wiki/index.md', `${page.rel} が index.md に載っていない`);
  }
}

// --- 6. コード参照 ----------------------------------------------------------
// 行番号だけのアンカーは編集で黙ってずれる(実運用では 13 件中 3 件がずれ、うち 1 件は関数の閉じ括弧を指していた)。
// ファイルの実在・行の範囲・記号名の所在を突き合わせ、ズレていれば正しい行を示す。
const fileCache = new Map();
const readLines = (rel) => {
  if (!fileCache.has(rel)) {
    try {
      fileCache.set(rel, readFileSync(join(ROOT, rel), 'utf8').split('\n'));
    } catch {
      fileCache.set(rel, null);
    }
  }
  return fileCache.get(rel);
};

for (const page of pages.values()) {
  for (const span of codeSpans(page.text)) {
    const match = CODE_REF.exec(span);
    if (!match) continue;
    const [, path, lineText, anchor] = match;
    // 記入例のプレースホルダ(<path/to/file.js:123>)は対象外。
    if (path.startsWith('<')) continue;
    const lines = readLines(path);
    if (lines === null) {
      fail(page.rel, `コード参照 ${span} のファイルが存在しない`);
      continue;
    }
    const line = Number(lineText);
    if (line < 1 || line > lines.length) {
      fail(page.rel, `コード参照 ${span} の行番号がファイルの行数 (${lines.length}) を超えている`);
      continue;
    }
    if (!anchor) {
      notes.push(`${page.rel}: コード参照 ${span} に記号名が無い(行番号だけのアンカーはずれても気づけない)`);
      continue;
    }
    // 末尾の () は呼び出し表記なので落として探す。
    const needle = anchor.replace(/\(\)$/, '');
    const hits = lines.flatMap((text, i) => (text.includes(needle) ? [i + 1] : []));
    if (hits.length === 0) {
      fail(page.rel, `コード参照 ${span} の記号名が ${path} に見つからない`);
    } else if (!hits.some((hit) => Math.abs(hit - line) <= ANCHOR_SLACK)) {
      fail(page.rel, `コード参照 ${span} の行番号がずれている(${needle} は ${hits.join(' / ')} 行目)`);
      continue;
    }

    // 陳腐化検知: ページを最後に触った日より後にその行が変わっていれば、記述が古い可能性がある。
    // **注意止まりにしてエラーにしない。** コードが動くたびに CI が赤くなると、CI そのものが
    // 「無視するもの」に変わる。直すかどうかは読んで判断する必要があり、機械には断定できない。
    const updated = page.frontmatter?.updated;
    if (typeof updated !== 'string' || isPlaceholder(updated)) continue;
    const changed = lastCommitDateForLine(path, line);
    if (changed && changed > updated) {
      notes.push(
        `${page.rel}: ${span} の周辺が ${changed} に変更されている(このページの updated は ${updated})`,
      );
    }
  }
}

// --- 7. index.md の生成領域 -------------------------------------------------
// 掲載漏れ(検査 5)は「載っているか」だけを見る。ここが見るのは「要約が今の summary と同じか」。
function buildIndexBlock() {
  const lines = [
    '<!-- ここから下は scripts/wiki-lint.js が各ページの frontmatter の summary から生成する。',
    '     手で書き換えても次の再生成で消える。要約を直すときはページ側の summary を直し、',
    '     `node scripts/wiki-lint.js --write-index` を走らせる。 -->',
  ];
  for (const section of INDEX_SECTIONS) {
    const listed = [...pages.values()]
      .filter((page) => !NO_FRONTMATTER.has(page.name) && page.dir === section.dir)
      .sort((a, b) => a.name.localeCompare(b.name));
    if (listed.length === 0) continue;
    lines.push('', section.heading, '');
    for (const page of listed) {
      lines.push(`- [[${page.name}]] — ${page.frontmatter?.summary ?? ''}`);
    }
  }
  return lines.join('\n');
}

if (index) {
  const text = index.text;
  const from = text.indexOf(INDEX_START);
  const to = text.indexOf(INDEX_END);
  if (from === -1 || to === -1 || to < from) {
    fail('wiki/index.md', `生成領域のマーカー(${INDEX_START} / ${INDEX_END})が無い`);
  } else {
    // 作業ツリーの改行は core.autocrlf に左右される(Windows では CRLF)。生成側は常に LF なので、
    // 正規化せずに比較すると **ローカルでだけ必ず落ちる**(CI は LF なので緑のまま)。
    const eol = text.includes('\r\n') ? '\r\n' : '\n';
    const normalize = (value) => value.replace(/\r\n/g, '\n');
    const current = normalize(text.slice(from + INDEX_START.length, to)).trim();
    const expected = buildIndexBlock();
    if (WRITE_INDEX) {
      if (current === expected) {
        console.log('wiki-index: 変更なし');
      } else {
        // 書き戻すときは元ファイルの改行を保つ。CRLF のファイルに LF を混ぜると
        // 作業ツリーに無意味な差分が出る。
        const block = expected.split('\n').join(eol);
        const next = `${text.slice(0, from + INDEX_START.length)}${eol}${block}${eol}${text.slice(to)}`;
        writeFileSync(join(ROOT, 'wiki/index.md'), next);
        console.log('wiki-index: wiki/index.md を再生成した');
      }
      process.exit(0);
    }
    if (current !== expected) {
      fail('wiki/index.md', '生成領域が各ページの summary と食い違う(node scripts/wiki-lint.js --write-index で再生成する)');
    }
  }
}

// --- 出力 -------------------------------------------------------------------
for (const note of notes) console.log(`注意: ${note}`);

if (errors.length === 0) {
  console.log(`wiki-lint: OK (${pages.size} ページ)`);
  process.exit(0);
}

const byFile = new Map();
for (const { file, message } of errors) {
  if (!byFile.has(file)) byFile.set(file, []);
  byFile.get(file).push(message);
}
for (const [file, messages] of [...byFile].sort()) {
  console.error(`\n${file}`);
  for (const message of messages) console.error(`  - ${message}`);
}
console.error(`\nwiki-lint: ${errors.length} 件の問題 (${pages.size} ページ)`);
process.exit(1);
