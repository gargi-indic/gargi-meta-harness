import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'fs';
import { join } from 'path';

describe('Design Rules', () => {
  const root = join(__dirname, '../');

  function getFiles(dir: string, pattern: RegExp): string[] {
    let results: string[] = [];
    const list = readdirSync(dir);
    list.forEach(file => {
      file = join(dir, file);
      if (file.includes('/generated/') || file.includes('/styles/tokens.css')) {
        return;
      }
      const stat = statSync(file);
      if (stat && stat.isDirectory()) {
        results = results.concat(getFiles(file, pattern));
      } else if (pattern.test(file)) {
        if (!file.endsWith('.test.tsx') && !file.endsWith('.test.ts')) {
          results.push(file);
        }
      }
    });
    return results;
  }

  function stripCssComments(content: string) {
    return content.replace(/\/\*[\s\S]*?\*\//g, '');
  }

  it('no hex color literals exist outside tokens.css', () => {
    const files = getFiles(root, /\.(ts|tsx|css)$/);
    const hexRegex = /#[0-9a-fA-F]{3,8}\b/;

    for (const file of files) {
      let content = readFileSync(file, 'utf-8');
      if (file.endsWith('.css')) {
        content = stripCssComments(content);
      }
      const lines = content.split('\n');
      lines.forEach((line, i) => {
        if (hexRegex.test(line)) {
          throw new Error(`Hex color found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
      });
    }
  });

  it('border-radius is never set to a non-zero value and no box-shadow/gradients in CSS', () => {
    const cssFiles = getFiles(root, /\.css$/);

    for (const file of cssFiles) {
      const content = stripCssComments(readFileSync(file, 'utf-8'));
      const lines = content.split('\n');

      lines.forEach((line, i) => {
        const matches = line.matchAll(/(?:^|[\s;{])(?:border-[a-z-]*)?radius\s*:\s*([^;}]+)/g);
        for (const match of matches) {
          const val = match[1].trim();
          if (!/^0(px)?$/.test(val)) {
            throw new Error(`Non-zero border-radius in ${file}:${i + 1}\n  ${line.trim()}`);
          }
        }

        if (/(box|text)-shadow\s*:/.test(line)) {
          throw new Error(`shadow found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
        if (/drop-shadow\(/.test(line)) {
          throw new Error(`drop-shadow found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
        if (/-gradient\s*\(/.test(line)) {
          throw new Error(`Gradient found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
      });
    }
  });

  it('no border-radius, box-shadow or gradients in TS/TSX', () => {
    const tsFiles = getFiles(root, /\.(ts|tsx)$/);
    for (const file of tsFiles) {
      const content = readFileSync(file, 'utf-8');
      const lines = content.split('\n');

      lines.forEach((line, i) => {
        if (line.includes('borderRadius')) {
          throw new Error(`borderRadius found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
        if (line.includes('boxShadow')) {
          throw new Error(`boxShadow found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
        if (line.includes('gradient(')) {
          throw new Error(`gradient found in ${file}:${i + 1}\n  ${line.trim()}`);
        }
      });
    }
  });

  it('tokens.css matches docs/design/tokens.css byte for byte', () => {
    const originalTokensPath = join(__dirname, '../../../docs/design/tokens.css');
    const localTokensPath = join(root, 'styles/tokens.css');

    const originalTokens = readFileSync(originalTokensPath, 'utf-8');
    const localTokens = readFileSync(localTokensPath, 'utf-8');

    expect(localTokens).toBe(originalTokens);
  });

  it('no link tags in index.html', () => {
    const indexPath = join(root, '../index.html');
    const indexContent = readFileSync(indexPath, 'utf-8');

    if (/<link/i.test(indexContent)) {
      throw new Error(`Found <link> tag in index.html: it should only use web fonts bundled via fontsource.`);
    }
  });
});
