// wiki/ のページを読み解く共通部品。scripts/wiki-lint.js と .claude/hooks/wiki_context.js が使う。
//
// 【なぜ切り出したか】
// hook は「このファイルに触れている Wiki ページ」を、lint は「コード参照が実在するか」を、
// どちらもページ中のコード参照 `path/to/file.js:123 記号名` から判定する。記法の解釈を
// 2 か所に書くと片方だけ直されて食い違い、lint が通るのに hook が拾わない参照が生まれる。
//
// 読み込んだだけでは何も実行しない(副作用なし)。

import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

// コード参照 `path/to/file.js:123 記号名` の記法。記号名は任意だが、付いていれば検証する。
// 行番号だけのアンカーは編集のたびに黙ってずれるので、記号名を主・行番号を補助として扱う。
export const CODE_REF = /^([\w./-]+\/[\w.-]+\.[a-zA-Z0-9]{1,5}):(\d+)(?:\s+(.+))?$/;

/** wiki/ 配下の .md を再帰的に集める(テンプレートは検査対象外)。 */
export function collect(dir) {
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
export function stripNonLinks(text) {
  return text
    .replace(/```[\s\S]*?```/g, '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/`[^`\n]*`/g, '');
}

/**
 * コードスパン(`...`)の中身を集める。フェンス付きコードブロックは記法の説明・入力例が
 * 入るので対象外(そこに書かれたパスは実在しなくてよい)。
 */
export function codeSpans(text) {
  return [...text.replace(/```[\s\S]*?```/g, '').matchAll(/`([^`\n]+)`/g)].map((m) => m[1].trim());
}

/** `<YYYY-MM-DD>` のような未記入プレースホルダか。テンプレート初期状態を落とさないため。 */
export const isPlaceholder = (value) => typeof value === 'string' && /^<.*>$/.test(value.trim());

/** frontmatter を最小限に解釈する。YAML パーサは入れない(依存を増やさないため)。 */
export function parseFrontmatter(text) {
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

/**
 * コード参照の逆引き表を作る: リポジトリ相対パス → そのファイルを参照しているページの一覧。
 * 記入例のプレースホルダ(`<path/to/file.js:123>`)は数えない。
 */
export function pagesByCodeFile(root) {
  const byFile = new Map();
  for (const file of collect(join(root, 'wiki'))) {
    const text = readFileSync(file, 'utf8');
    const rel = relative(root, file).split(sep).join('/');
    const name = rel.split('/').pop().slice(0, -3);
    const summary = parseFrontmatter(text)?.summary ?? '';
    for (const span of codeSpans(text)) {
      const match = CODE_REF.exec(span);
      if (!match || match[1].startsWith('<')) continue;
      const pages = byFile.get(match[1]) ?? [];
      if (!pages.some((page) => page.name === name)) pages.push({ name, rel, summary });
      byFile.set(match[1], pages);
    }
  }
  return byFile;
}
